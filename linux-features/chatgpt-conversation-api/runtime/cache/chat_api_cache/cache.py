from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
import zlib
from contextlib import contextmanager, nullcontext
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def scope(context):
    if not isinstance(context, dict) or set(context) != {"account_id", "user_id", "workspace_id"}:
        raise ValueError("An exact authenticated account/workspace context is required")
    if not all(isinstance(value, str) and value for value in context.values()):
        raise ValueError("Invalid account/workspace context")
    return digest(context)


def metadata_fingerprint(item):
    value = item.get("updated_at")
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            value = round(parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp() * 1000000)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            value = round(value * 1000000)
    except (ValueError, OverflowError):
        pass
    return digest({"updated_at": value, **{key: item.get(key) for key in ("title", "project_id", "archived")}})


class Cache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.directory.stat().st_uid != os.getuid():
            raise ValueError("Unsafe API cache directory")
        self.directory.chmod(0o700)
        path = self.directory / "cache.sqlite3"
        if path.is_symlink():
            raise ValueError("Unsafe API cache database")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("Unsupported API cache schema version")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata(scope TEXT,id TEXT,version TEXT,data TEXT,observed_at REAL,
                PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS snapshots(scope TEXT,id TEXT,sha TEXT,payload BLOB,
                complete INTEGER,recorded_at REAL,PRIMARY KEY(scope,id,sha));
            CREATE TABLE IF NOT EXISTS heads(scope TEXT,id TEXT,sha TEXT,fetched_at REAL,
                metadata_version TEXT,invalidated INTEGER DEFAULT 0,PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS attempts(sequence INTEGER PRIMARY KEY,scope TEXT,id TEXT,
                at REAL,response BLOB,error_code TEXT,http_status INTEGER,metadata_version TEXT);
            CREATE INDEX IF NOT EXISTS attempts_chat ON attempts(scope,id,sequence);
            CREATE TABLE IF NOT EXISTS events(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS mutation_jobs(scope TEXT,id TEXT,status TEXT,PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS metadata_checks(scope TEXT PRIMARY KEY,job_id TEXT,started_at REAL,
                checked_at REAL,status TEXT,completeness TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS checked_metadata(scope TEXT,id TEXT,job_id TEXT,PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS targeted_checks(scope TEXT,id TEXT,check_id TEXT,started_at REAL,
                checked_at REAL,status TEXT,metadata TEXT,error TEXT,PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS metadata_reads(sequence INTEGER PRIMARY KEY,scope TEXT,id TEXT,
                at REAL,response BLOB);
            CREATE TABLE IF NOT EXISTS dirty(scope TEXT,id TEXT,at REAL,reason TEXT,PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS change_cursors(scope TEXT PRIMARY KEY,cursor TEXT,checked_at REAL);
            CREATE TABLE IF NOT EXISTS plan_targets(scope TEXT,id TEXT,targets TEXT,PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS job_targets(scope TEXT,id TEXT,targets TEXT,PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS refresh_jobs(scope TEXT,kind TEXT,id TEXT,token TEXT UNIQUE,context TEXT,
                state TEXT,requested_at REAL,started_at REAL,finished_at REAL,retry_at REAL,error TEXT,
                PRIMARY KEY(scope,kind,id));
            CREATE TABLE IF NOT EXISTS index_events(sequence INTEGER PRIMARY KEY,scope TEXT,id TEXT,
                kind TEXT,sha TEXT,at REAL);
            CREATE INDEX IF NOT EXISTS index_events_scope ON index_events(scope,sequence);
            CREATE INDEX IF NOT EXISTS index_events_chat ON index_events(scope,id,sequence);
            CREATE TABLE IF NOT EXISTS fetch_failures(scope TEXT,id TEXT,signature TEXT,error TEXT,
                at REAL,retry_at REAL,count INTEGER,PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS read_controls(scope TEXT PRIMARY KEY,state TEXT,error TEXT,at REAL);
        ''')
        self.db.execute("PRAGMA user_version=1")

    def close(self):
        self.db.close()

    def read_control(self, context=None):
        keys = ["global"]
        if context is not None:
            keys.append(scope(context))
        for key in keys:
            row = self.db.execute("SELECT state,error,at FROM read_controls WHERE scope=?", (key,)).fetchone()
            if row:
                return {"state": row["state"], "error": json.loads(row["error"]), "at": row["at"]}
        return None

    def suspend_reads(self, context=None, *, code="MAINTENANCE", message="Native reads are paused"):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO read_controls VALUES (?,?,?,?)",
                            (scope(context) if context is not None else "global", "paused" if code == "MAINTENANCE" else "open",
                             json.dumps({"code": code, "message": message}), time.time()))

    def resume_reads(self, context=None):
        # Only a local operator after a bounded successful probe calls this.
        # No tool argument or expiry can clear the circuit automatically.
        with self.db:
            self.db.execute("DELETE FROM read_controls WHERE scope=?", (scope(context) if context is not None else "global",))
            self.db.execute("INSERT OR REPLACE INTO events VALUES ('read_recovery_epoch',?)", (str(uuid.uuid4()),))

    def read_epoch(self):
        row = self.db.execute("SELECT value FROM events WHERE key='read_recovery_epoch'").fetchone()
        return row[0] if row else None

    def pause_refresh(self, job, error):
        with self.db:
            self.db.execute("UPDATE refresh_jobs SET state='paused',finished_at=?,retry_at=NULL,error=? WHERE token=?",
                            (time.time(), json.dumps(error), job["token"]))

    def version(self, context, conversation_id):
        row = self.db.execute("SELECT version FROM metadata WHERE scope=? AND id=?",
                              (scope(context), conversation_id)).fetchone()
        return row[0] if row else None

    def emit_index_event(self, context, identifier, kind, sha=None):
        self.db.execute("INSERT INTO index_events(scope,id,kind,sha,at) VALUES (?,?,?,?,?)",
                        (scope(context), identifier, kind, sha, time.time()))

    def index_events(self, context, after=0, limit=500):
        return [dict(row) for row in self.db.execute("SELECT * FROM index_events WHERE scope=? AND sequence>? ORDER BY sequence LIMIT ?",
                                                    (scope(context), after, limit))]

    def metadata_item(self, context, identifier):
        row = self.db.execute("SELECT data FROM metadata WHERE scope=? AND id=?", (scope(context), identifier)).fetchone()
        return json.loads(row[0]) if row else None

    def read_context(self, context, identifier):
        item = self.metadata_item(context, identifier) or {}
        project, owner = item.get("project_id"), item.get("owner_id")
        if not isinstance(project, str) or not re.fullmatch(r"g-p-[A-Za-z0-9_-]{1,196}", project):
            return {}
        result = {"project_id": project}
        if isinstance(owner, str) and re.fullmatch(r"[A-Za-z0-9@._:-]{1,200}", owner):
            result["owner_id"] = owner
        return result

    def failure_signature(self, context, identifier, revision):
        return digest([metadata_fingerprint(self.metadata_item(context, identifier) or {}), self.read_context(context, identifier), revision])

    def failure(self, context, identifier, signature):
        row = self.db.execute("SELECT * FROM fetch_failures WHERE scope=? AND id=?", (scope(context), identifier)).fetchone()
        if row and row["signature"] == signature and row["retry_at"] > time.time():
            error = json.loads(row["error"])
            error["details"] = {**error.get("details", {}), "quarantined": True, "retry_at_ms": int(row["retry_at"] * 1000)}
            return error
        return None

    def observe(self, context, items, observed_at, *, emit=True):
        with (nullcontext() if self.db.in_transaction else self.db):
            for item in items:
                old = self.db.execute("SELECT data,observed_at,version FROM metadata WHERE scope=? AND id=?", (scope(context), item["id"])).fetchone()
                if old and old["observed_at"] > observed_at:
                    continue
                version = metadata_fingerprint(item)
                changed = old is None or metadata_fingerprint(json.loads(old["data"])) != version
                if old and not changed and old["version"] != version:
                    self.db.execute("UPDATE heads SET metadata_version=? WHERE scope=? AND id=? AND metadata_version=?",
                                    (version, scope(context), item["id"], old["version"]))
                self.db.execute('''INSERT INTO metadata VALUES (?,?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET
                    version=excluded.version,data=excluded.data,observed_at=excluded.observed_at
                    WHERE excluded.observed_at>=metadata.observed_at''',
                                (scope(context), item["id"], version, json.dumps(item, ensure_ascii=False), observed_at))
                if emit and changed:
                    self.emit_index_event(context, item["id"], "refresh")

    @contextmanager
    def fetch_lock(self, context, conversation_id, *, blocking=True):
        directory = self.directory / "locks"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / digest([scope(context), conversation_id])
        with path.open("a+") as lock:
            os.fchmod(lock.fileno(), 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def metadata_lock(self, context, timeout, conversation_id=None):
        directory = self.directory / "locks"
        directory.mkdir(mode=0o700, exist_ok=True)
        deadline = time.monotonic() + timeout
        suffix = "-" + digest(conversation_id) if conversation_id is not None else ""
        with (directory / ("metadata-" + scope(context) + suffix)).open("a+") as lock:
            os.fchmod(lock.fileno(), 0o600)
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting for the shared metadata check") from None
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def metadata_check(self, context):
        row = self.db.execute("SELECT * FROM metadata_checks WHERE scope=?", (scope(context),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["error"] = json.loads(result["error"]) if result["error"] else None
        return result

    def start_metadata_check(self, context, job_id, started_at):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata_checks VALUES (?,?,?,NULL,'running',NULL,NULL)",
                            (scope(context), job_id, started_at))

    def finish_metadata_check(self, context, items, job_id, started_at, observed_at, completeness):
        previous = {r[0] for r in self.db.execute("SELECT id FROM checked_metadata WHERE scope=?", (scope(context),))}
        missing = previous - {item["id"] for item in items}
        if missing:
            # Absence is a reason to recheck a requested body, never to erase it.
            self.invalidate(context, ["inventory_absence", job_id], missing)
        self.observe(context, items, observed_at)
        with self.db:
            self.db.execute("DELETE FROM checked_metadata WHERE scope=?", (scope(context),))
            self.db.executemany("INSERT INTO checked_metadata VALUES (?,?,?)",
                                [(scope(context), item["id"], job_id) for item in items])
            self.db.execute("INSERT OR REPLACE INTO metadata_checks VALUES (?,?,?,?,'completed',?,NULL)",
                            (scope(context), job_id, started_at, time.time(), completeness))
        return self.metadata_check(context)

    def fail_metadata_check(self, context, job_id, started_at, error):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata_checks VALUES (?,?,?,?,'failed',NULL,?)",
                            (scope(context), job_id, started_at, time.time(), json.dumps(error)))

    def checked_item(self, context, conversation_id, job_id):
        row = self.db.execute('''SELECT m.data FROM metadata m JOIN checked_metadata v ON v.scope=m.scope AND v.id=m.id
            WHERE m.scope=? AND m.id=? AND v.job_id=?''', (scope(context), conversation_id, job_id)).fetchone()
        return json.loads(row[0]) if row else None

    def targeted_check(self, context, conversation_id):
        row = self.db.execute("SELECT * FROM targeted_checks WHERE scope=? AND id=?", (scope(context), conversation_id)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("metadata", "error"):
            result[key] = json.loads(result[key]) if result[key] else None
        result["source"] = "targeted_metadata"
        result["job_id"] = None
        result["completeness"] = "requested_chat_only"
        return result

    def record_metadata(self, context, conversation_id, response):
        data = response.get("structuredContent")
        if data is None:
            data = json.loads(next(c["text"] for c in response["content"] if c.get("type") == "text"))
        if not response.get("isError") and "error" not in data:
            if data.get("context") != context or data.get("id") != conversation_id or not isinstance(data.get("exists"), bool):
                raise ValueError("Targeted metadata response has an inconsistent account or conversation ID")
            item = data.get("metadata")
            if data["exists"] and (not isinstance(item, dict) or item.get("id") != conversation_id):
                raise ValueError("Targeted metadata response has an inconsistent record")
            if not data["exists"] and item is not None:
                raise ValueError("Missing targeted metadata unexpectedly contains a record")
        with self.db:
            self.db.execute("INSERT INTO metadata_reads(scope,id,at,response) VALUES (?,?,?,?)",
                            (scope(context), conversation_id, time.time(), zlib.compress(json.dumps(response, ensure_ascii=True).encode())))

    def save_targeted_check(self, context, conversation_id, check_id, started_at, metadata=None, error=None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO targeted_checks VALUES (?,?,?,?,?,?,?,?)",
                            (scope(context), conversation_id, check_id, started_at, time.time(),
                             "failed" if error else "completed", json.dumps(metadata) if metadata is not None else None,
                             json.dumps(error) if error else None))
        return self.targeted_check(context, conversation_id)

    def record(self, context, conversation_id, response, *, started_at=None, metadata_version=None, failure_signature=None, attempt_only=False):
        """Retain the native API's exact JSON text, including unknown fields and errors."""
        encoded = json.dumps({key:value for key,value in response.items() if key != "_native_json_text"}, ensure_ascii=True, separators=(",", ":")).encode()
        data = response.get("structuredContent")
        if data is None:
            data = json.loads(next(c["text"] for c in response["content"] if c.get("type") == "text"))
        error = data.get("error") or {}
        version = self.version(context, conversation_id) if started_at is None else metadata_version
        timestamp = time.time()
        with self.db:
            self.db.execute("INSERT INTO attempts(scope,id,at,response,error_code,http_status,metadata_version) VALUES (?,?,?,?,?,?,?)",
                            (scope(context), conversation_id, timestamp, zlib.compress(encoded), error.get("code"),
                             (error.get("details") or {}).get("status"), version))
            if attempt_only:
                return
            if not response.get("isError") and "error" not in data:
                text = response.get("_native_json_text") or next((c["text"] for c in response.get("content", []) if c.get("type") == "text"),
                            json.dumps(data, ensure_ascii=False))
                raw = text.encode()
                if json.loads(raw) != data or data.get("context") != context or not isinstance(data.get("pages"), list) or not data["pages"]:
                    raise ValueError("Native raw response has an inconsistent payload or account context")
                sha = hashlib.sha256(raw).hexdigest()
                header = data["pages"][-1]
                if all(key in header for key in ("title", "update_time", "is_archived")) and any(key in header for key in ("gizmo_id", "conversation_template_id", "project_id")):
                    item = {"id": conversation_id, "title": header["title"], "updated_at": header["update_time"],
                            "created_at": header.get("create_time"), "archived": header["is_archived"],
                            "project_id": next((header[k] for k in ("gizmo_id", "conversation_template_id", "project_id") if header.get(k)), None),
                            "owner_id": header["owner"].get("user_id") if isinstance(header.get("owner"), dict) else None}
                    # Inventory and body update_time values can differ for the
                    # same backend revision. Retain the inventory hint selected
                    # before this read; replacing it with the body timestamp
                    # would produce a false change on every later scan.
                    if self.metadata_item(context, conversation_id) is None:
                        self.observe(context, [item], started_at or timestamp, emit=False)
                        version = metadata_fingerprint(item)
                self.db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?)",
                                (scope(context), conversation_id, sha, zlib.compress(raw), int(data.get("complete") is True), timestamp))
                self.db.execute("INSERT OR REPLACE INTO heads VALUES (?,?,?,?,?,0)",
                                (scope(context), conversation_id, sha, timestamp, version))
                dirty = self.db.execute("SELECT at FROM dirty WHERE scope=? AND id=?",
                                        (scope(context), conversation_id)).fetchone()
                epoch = self.db.execute("SELECT value FROM events WHERE key=?", ("epoch:" + scope(context),)).fetchone()
                boundary = started_at if started_at is not None else timestamp
                if (dirty and dirty[0] > boundary) or (epoch and float(epoch[0]) > boundary):
                    self.db.execute("UPDATE heads SET invalidated=1 WHERE scope=? AND id=?", (scope(context), conversation_id))
                else:
                    self.db.execute("DELETE FROM dirty WHERE scope=? AND id=?", (scope(context), conversation_id))
                self.db.execute("DELETE FROM fetch_failures WHERE scope=? AND id=?", (scope(context), conversation_id))
                self.emit_index_event(context, conversation_id, "upsert" if data.get("complete") else "incomplete", sha)
            elif (error.get("details") or {}).get("status") in (401, 403, 404):
                self.db.execute("UPDATE heads SET invalidated=1 WHERE scope=? AND id=?", (scope(context), conversation_id))
                self.emit_index_event(context, conversation_id, "unavailable")
                if (error.get("details") or {}).get("status") == 404 and failure_signature:
                    previous = self.db.execute("SELECT count,signature FROM fetch_failures WHERE scope=? AND id=?", (scope(context), conversation_id)).fetchone()
                    count = previous["count"] + 1 if previous and previous["signature"] == failure_signature else 1
                    self.db.execute("INSERT OR REPLACE INTO fetch_failures VALUES (?,?,?,?,?,?,?)",
                                    (scope(context), conversation_id, failure_signature, json.dumps(error), timestamp,
                                     timestamp + min(86400, 3600 * 2 ** min(count - 1, 5)), count))

    def lookup(self, context, conversation_id):
        row = self.db.execute('''SELECT h.*,s.payload,s.complete,m.version AS current_version,m.observed_at,m.data AS current_metadata
            FROM heads h JOIN snapshots s ON s.scope=h.scope AND s.id=h.id AND s.sha=h.sha
            LEFT JOIN metadata m ON m.scope=h.scope AND m.id=h.id WHERE h.scope=? AND h.id=?''',
                              (scope(context), conversation_id)).fetchone()
        if not row:
            return None
        raw = zlib.decompress(row["payload"])
        if hashlib.sha256(raw).hexdigest() != row["sha"]:
            raise ValueError("API snapshot integrity check failed")
        data = json.loads(raw)
        if data.get("context") != context:
            raise ValueError("Cached API snapshot has a different account context")
        matched = row["current_version"] is None or row["current_version"] == row["metadata_version"]
        # Keep the inventory hint bound to legacy captures. Body timestamps and
        # list timestamps can differ even for the same revision. A body can add
        # positive proof when no matching bound hint exists, never invalidate it.
        header = data["pages"][-1]
        if not matched and row["current_metadata"] and all(key in header for key in ("title", "update_time", "is_archived")) and any(
                key in header for key in ("gizmo_id", "conversation_template_id", "project_id")):
            membership = next((header[key] for key in ("gizmo_id", "conversation_template_id", "project_id")
                               if header.get(key) is not None), None)
            body_version = metadata_fingerprint({"title": header["title"], "updated_at": header["update_time"],
                                   "archived": header["is_archived"], "project_id": membership})
            matched = body_version == metadata_fingerprint(json.loads(row["current_metadata"]))
        validated_at = max(row["fetched_at"], row["observed_at"] or 0) if matched else row["fetched_at"]
        pending = self.pending_jobs(context, conversation_id)
        dirty = self.db.execute("SELECT at,reason FROM dirty WHERE scope=? AND id=?", (scope(context), conversation_id)).fetchone()
        return {"data": data, "sha": row["sha"], "fetched_at": row["fetched_at"],
                "validated_at": validated_at, "metadata_matches": matched,
                "metadata_observed": row["current_version"] is not None,
                "dirty_reason": dirty["reason"] if dirty else "pending_mutation" if pending else
                                "metadata_changed" if not matched else "invalidated" if row["invalidated"] else None,
                "invalidated": bool(row["invalidated"] or dirty or pending)}

    def negative(self, context, conversation_id, ttl):
        row = self.db.execute('''SELECT * FROM attempts WHERE scope=? AND id=?
            ORDER BY sequence DESC LIMIT 1''', (scope(context), conversation_id)).fetchone()
        invalidated = self.db.execute("SELECT value FROM events WHERE key=?", ("epoch:" + scope(context),)).fetchone()
        dirty = self.db.execute("SELECT at FROM dirty WHERE scope=? AND id=?", (scope(context), conversation_id)).fetchone()
        if row and row["http_status"] == 404 and time.time() - row["at"] < ttl and row["metadata_version"] == self.version(context, conversation_id) and (not invalidated or row["at"] >= float(invalidated[0])) and (not dirty or row["at"] >= dirty[0]) and not self.pending_jobs(context, conversation_id):
            return json.loads(zlib.decompress(row["response"]))

    def snapshot(self, context, conversation_id, sha):
        row = self.db.execute("SELECT payload FROM snapshots WHERE scope=? AND id=? AND sha=?",
                              (scope(context), conversation_id, sha)).fetchone()
        if row is None:
            raise ValueError("The referenced raw snapshot is not available")
        raw = zlib.decompress(row[0])
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError("API snapshot integrity check failed")
        data = json.loads(raw)
        if data.get("context") != context:
            raise ValueError("Cached API snapshot has a different account context")
        return data

    def invalidate(self, context, event, targets=None):
        key = digest([scope(context), event])
        with self.db:
            if self.db.execute("SELECT 1 FROM events WHERE key=?", (key,)).fetchone():
                return
            if targets is None:
                for row in self.db.execute("SELECT id,sha FROM heads WHERE scope=?", (scope(context),)).fetchall():
                    self.emit_index_event(context, row["id"], "reconcile", row["sha"])
                self.db.execute("INSERT OR REPLACE INTO dirty SELECT scope,id,?,? FROM heads WHERE scope=?",
                                (time.time(), str(event[0]), scope(context)))
                self.db.execute("UPDATE heads SET invalidated=1 WHERE scope=?", (scope(context),))
                self.db.execute("INSERT OR REPLACE INTO events VALUES (?,?)", ("epoch:" + scope(context), str(time.time())))
            else:
                for identifier in set(targets):
                    self.db.execute("DELETE FROM fetch_failures WHERE scope=? AND id=?", (scope(context), identifier))
                    self.emit_index_event(context, identifier, "refresh")
                    self.db.execute("UPDATE heads SET invalidated=1 WHERE scope=? AND id=?", (scope(context), identifier))
                    self.db.execute("INSERT OR REPLACE INTO dirty VALUES (?,?,?,?)",
                                    (scope(context), identifier, time.time(), str(event[0])))
            self.db.execute("INSERT INTO events VALUES (?,?)", (key, str(time.time())))

    def remember_plan(self, context, plan_id, operations):
        targets = sorted({op["conversation_id"] for op in operations if op.get("conversation_id")})
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO plan_targets VALUES (?,?,?)", (scope(context), plan_id, json.dumps(targets)))

    def targets(self, context, identifier, *, job=False):
        table = "job_targets" if job else "plan_targets"
        row = self.db.execute(f"SELECT targets FROM {table} WHERE scope=? AND id=?", (scope(context), identifier)).fetchone()
        return json.loads(row[0]) if row else None

    def mutation_job(self, context, job_id, status, targets=None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO mutation_jobs VALUES (?,?,?)", (scope(context), job_id, status))
            if targets is not None:
                self.db.execute("INSERT OR REPLACE INTO job_targets VALUES (?,?,?)", (scope(context), job_id, json.dumps(targets)))

    def pending_jobs(self, context, conversation_id=None):
        jobs = [r[0] for r in self.db.execute("SELECT id FROM mutation_jobs WHERE scope=? AND status='pending'", (scope(context),))]
        return [j for j in jobs if conversation_id is None or self.targets(context, j, job=True) is None
                or conversation_id in self.targets(context, j, job=True)]

    def change_cursor(self, context):
        row = self.db.execute("SELECT cursor FROM change_cursors WHERE scope=?", (scope(context),)).fetchone()
        return row[0] if row else None

    def apply_changes(self, context, data):
        if (data.get("context") != context or not isinstance(data.get("cursor"), str) or len(data["cursor"]) > 100
                or not isinstance(data.get("items"), list) or len(data["items"]) > 1000
                or type(data.get("reset_required")) is not bool or type(data.get("has_more")) is not bool):
            raise ValueError("Inconsistent native change feed")
        for event in data["items"]:
            if (not isinstance(event, dict) or set(event) - {"sequence", "kind", "id"}
                    or type(event.get("sequence")) is not int or event["sequence"] < 1
                    or event.get("kind") not in ("chat_changed", "gap")
                    or event["kind"] == "chat_changed" and (not isinstance(event.get("id"), str) or not event["id"])
                    or event["kind"] == "gap" and "id" in event):
                raise ValueError("Invalid native change event")
        if data.get("reset_required"):
            self.invalidate(context, ["notification_gap", data["cursor"]])
        for event in data["items"]:
            if event.get("kind") == "gap":
                self.invalidate(context, ["notification_gap", data["cursor"], event["sequence"]])
            elif event.get("kind") == "chat_changed" and isinstance(event.get("id"), str):
                self.invalidate(context, ["desktop_change", data["cursor"], event["sequence"]], [event["id"]])
            else:
                raise ValueError("Invalid native change event")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO change_cursors VALUES (?,?,?)", (scope(context), data["cursor"], time.time()))

    def refresh_job(self, context, kind, identifier=""):
        row = self.db.execute("SELECT * FROM refresh_jobs WHERE scope=? AND kind=? AND id=?", (scope(context), kind, identifier)).fetchone()
        if not row:
            return None
        data = dict(row)
        for key in ("context", "error"):
            data[key] = json.loads(data[key]) if data[key] else None
        return data

    def queue_refresh(self, context, kind, identifier="", *, interval=0, force=False):
        old = self.refresh_job(context, kind, identifier)
        now = time.time()
        if old:
            if old["state"] in ("queued", "running") or (old["retry_at"] or 0) > now:
                return old
            if not force and old["finished_at"] and now - old["finished_at"] < interval:
                return old
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO refresh_jobs VALUES (?,?,?,?,?,'queued',?,NULL,NULL,NULL,NULL)",
                            (scope(context), kind, identifier, str(uuid.uuid4()), json.dumps(context), now))
        return self.refresh_job(context, kind, identifier)

    def next_refresh(self, kind):
        row = self.db.execute("SELECT * FROM refresh_jobs WHERE kind=? AND state='queued' AND coalesce(retry_at,0)<=? ORDER BY requested_at LIMIT 1", (kind, time.time())).fetchone()
        if not row:
            return None
        return self.refresh_job(json.loads(row["context"]), kind, row["id"])

    def start_refresh(self, job):
        with self.db:
            self.db.execute("UPDATE refresh_jobs SET state='running',started_at=? WHERE token=?", (time.time(), job["token"]))

    def finish_refresh(self, job, error=None, *, retry_delay=30):
        retry = ((error or {}).get("details") or {}).get("retry_at_ms")
        retry_at = max(time.time() + retry_delay, retry / 1000 if isinstance(retry, (int, float)) else 0) if error else None
        with self.db:
            self.db.execute("UPDATE refresh_jobs SET state=?,finished_at=?,retry_at=?,error=? WHERE token=?",
                            ("failed" if error else "completed", time.time(), retry_at, json.dumps(error) if error else None, job["token"]))

    def defer_refresh(self, job, error):
        retry = (error.get("details") or {}).get("retry_at_ms")
        retry_at = max(time.time() + 1, retry / 1000 if isinstance(retry, (int, float)) else time.time() + 30)
        with self.db:
            self.db.execute("UPDATE refresh_jobs SET state='queued',started_at=NULL,retry_at=?,error=? WHERE token=?",
                            (retry_at, json.dumps(error), job["token"]))

    def next_refresh_deadline(self, kind):
        row = self.db.execute("SELECT min(coalesce(retry_at,0)) FROM refresh_jobs WHERE kind=? AND state='queued'", (kind,)).fetchone()
        return row[0] if row and row[0] is not None else None

    def recover_refreshes(self, kind):
        # Caller owns the exclusive worker flock. Only read jobs are replayed.
        with self.db:
            self.db.execute("UPDATE refresh_jobs SET state='queued' WHERE kind=? AND state='running'", (kind,))

    @contextmanager
    def queue_lock(self):
        path = self.directory / "queue.lock"
        with path.open("a+") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def status(self, context):
        bound = scope(context)
        current, complete = self.db.execute('''SELECT count(*),coalesce(sum(s.complete),0) FROM heads h
            JOIN snapshots s ON s.scope=h.scope AND s.id=h.id AND s.sha=h.sha WHERE h.scope=?''', (bound,)).fetchone()
        latest = self.db.execute("SELECT id FROM targeted_checks WHERE scope=? ORDER BY checked_at DESC LIMIT 1", (bound,)).fetchone()
        return {"directory": str(self.directory), "current_chats": current, "complete_chats": complete,
                "retained_versions": self.db.execute("SELECT count(*) FROM snapshots WHERE scope=?", (bound,)).fetchone()[0],
                "metadata_records": self.db.execute("SELECT count(*) FROM metadata WHERE scope=?", (bound,)).fetchone()[0],
                "recorded_fetch_attempts": self.db.execute("SELECT count(*) FROM attempts WHERE scope=?", (bound,)).fetchone()[0],
                "default_policy": "prefer_cache", "automatic_metadata_check": False, "freshness_strategy": "cache_age_and_change_notifications",
                "background_refresh_jobs": self.db.execute("SELECT count(*) FROM refresh_jobs WHERE scope=? AND state IN ('queued','running')", (bound,)).fetchone()[0],
                "read_control": self.read_control(context),
                "last_metadata_check": self.targeted_check(context, latest[0]) if latest else self.metadata_check(context),
                "targeted_metadata_reads": self.db.execute("SELECT count(*) FROM metadata_reads WHERE scope=?", (bound,)).fetchone()[0],
                "storage": "verbatim_native_raw_json_compressed"}
