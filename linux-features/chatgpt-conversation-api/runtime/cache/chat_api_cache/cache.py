from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
import zlib
from contextlib import contextmanager
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def scope(context):
    if not isinstance(context, dict) or set(context) != {"account_id", "user_id", "workspace_id"}:
        raise ValueError("An exact authenticated account/workspace context is required")
    if not all(isinstance(value, str) and value for value in context.values()):
        raise ValueError("Invalid account/workspace context")
    return digest(context)


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
        ''')
        self.db.execute("PRAGMA user_version=1")

    def close(self):
        self.db.close()

    def version(self, context, conversation_id):
        row = self.db.execute("SELECT version FROM metadata WHERE scope=? AND id=?",
                              (scope(context), conversation_id)).fetchone()
        return row[0] if row else None

    def observe(self, context, items, observed_at):
        with self.db:
            for item in items:
                version = digest({key: item.get(key) for key in ("updated_at", "title", "project_id", "archived")})
                self.db.execute('''INSERT INTO metadata VALUES (?,?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET
                    version=excluded.version,data=excluded.data,observed_at=excluded.observed_at
                    WHERE excluded.observed_at>=metadata.observed_at''',
                                (scope(context), item["id"], version, json.dumps(item, ensure_ascii=False), observed_at))

    @contextmanager
    def fetch_lock(self, context, conversation_id):
        directory = self.directory / "locks"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / digest([scope(context), conversation_id])
        with path.open("a+") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
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

    def record(self, context, conversation_id, response):
        """Retain the native API's exact JSON text, including unknown fields and errors."""
        encoded = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode()
        data = response.get("structuredContent")
        if data is None:
            data = json.loads(next(c["text"] for c in response["content"] if c.get("type") == "text"))
        error = data.get("error") or {}
        version = self.version(context, conversation_id)
        timestamp = time.time()
        with self.db:
            self.db.execute("INSERT INTO attempts(scope,id,at,response,error_code,http_status,metadata_version) VALUES (?,?,?,?,?,?,?)",
                            (scope(context), conversation_id, timestamp, zlib.compress(encoded), error.get("code"),
                             (error.get("details") or {}).get("status"), version))
            if not response.get("isError") and "error" not in data:
                text = next((c["text"] for c in response.get("content", []) if c.get("type") == "text"),
                            json.dumps(data, ensure_ascii=False))
                raw = text.encode()
                if json.loads(raw) != data or data.get("context") != context or not isinstance(data.get("pages"), list) or not data["pages"]:
                    raise ValueError("Native raw response has an inconsistent payload or account context")
                sha = hashlib.sha256(raw).hexdigest()
                self.db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?)",
                                (scope(context), conversation_id, sha, zlib.compress(raw), int(data.get("complete") is True), timestamp))
                self.db.execute("INSERT OR REPLACE INTO heads VALUES (?,?,?,?,?,0)",
                                (scope(context), conversation_id, sha, timestamp, version))
            elif (error.get("details") or {}).get("status") in (401, 403, 404):
                self.db.execute("UPDATE heads SET invalidated=1 WHERE scope=? AND id=?", (scope(context), conversation_id))

    def lookup(self, context, conversation_id):
        row = self.db.execute('''SELECT h.*,s.payload,s.complete,m.version AS current_version,m.observed_at
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
        # A body fetched before the first inventory can itself prove that its
        # public metadata matches the subsequently observed inventory version.
        header = data["pages"][-1]
        if not matched and all(key in header for key in ("title", "update_time", "is_archived")) and any(
                key in header for key in ("gizmo_id", "conversation_template_id", "project_id")):
            membership = next((header[key] for key in ("gizmo_id", "conversation_template_id", "project_id")
                               if header.get(key) is not None), None)
            body_version = digest({"title": header["title"], "updated_at": header["update_time"],
                                   "archived": header["is_archived"], "project_id": membership})
            matched = body_version == row["current_version"]
        validated_at = max(row["fetched_at"], row["observed_at"] or 0) if matched else row["fetched_at"]
        pending = self.db.execute("SELECT 1 FROM mutation_jobs WHERE scope=? AND status='pending' LIMIT 1", (scope(context),)).fetchone()
        return {"data": data, "sha": row["sha"], "fetched_at": row["fetched_at"],
                "validated_at": validated_at, "metadata_matches": matched,
                "metadata_observed": row["current_version"] is not None,
                "invalidated": bool(row["invalidated"] or pending)}

    def negative(self, context, conversation_id, ttl):
        row = self.db.execute('''SELECT * FROM attempts WHERE scope=? AND id=?
            ORDER BY sequence DESC LIMIT 1''', (scope(context), conversation_id)).fetchone()
        invalidated = self.db.execute("SELECT value FROM events WHERE key=?", ("epoch:" + scope(context),)).fetchone()
        if row and row["http_status"] == 404 and time.time() - row["at"] < ttl and row["metadata_version"] == self.version(context, conversation_id) and (not invalidated or row["at"] >= float(invalidated[0])):
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

    def invalidate(self, context, event):
        key = digest([scope(context), event])
        with self.db:
            if self.db.execute("SELECT 1 FROM events WHERE key=?", (key,)).fetchone():
                return
            self.db.execute("UPDATE heads SET invalidated=1 WHERE scope=?", (scope(context),))
            self.db.execute("INSERT INTO events VALUES (?,?)", (key, str(time.time())))
            self.db.execute("INSERT OR REPLACE INTO events VALUES (?,?)", ("epoch:" + scope(context), str(time.time())))

    def mutation_job(self, context, job_id, status):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO mutation_jobs VALUES (?,?,?)", (scope(context), job_id, status))

    def pending_jobs(self, context):
        return [r[0] for r in self.db.execute("SELECT id FROM mutation_jobs WHERE scope=? AND status='pending'", (scope(context),))]

    def status(self, context):
        bound = scope(context)
        current, complete = self.db.execute('''SELECT count(*),coalesce(sum(s.complete),0) FROM heads h
            JOIN snapshots s ON s.scope=h.scope AND s.id=h.id AND s.sha=h.sha WHERE h.scope=?''', (bound,)).fetchone()
        latest = self.db.execute("SELECT id FROM targeted_checks WHERE scope=? ORDER BY checked_at DESC LIMIT 1", (bound,)).fetchone()
        return {"directory": str(self.directory), "current_chats": current, "complete_chats": complete,
                "retained_versions": self.db.execute("SELECT count(*) FROM snapshots WHERE scope=?", (bound,)).fetchone()[0],
                "metadata_records": self.db.execute("SELECT count(*) FROM metadata WHERE scope=?", (bound,)).fetchone()[0],
                "recorded_fetch_attempts": self.db.execute("SELECT count(*) FROM attempts WHERE scope=?", (bound,)).fetchone()[0],
                "default_policy": "prefer_cache", "automatic_metadata_check": True, "metadata_check_scope": "requested_chat",
                "last_metadata_check": self.targeted_check(context, latest[0]) if latest else self.metadata_check(context),
                "targeted_metadata_reads": self.db.execute("SELECT count(*) FROM metadata_reads WHERE scope=?", (bound,)).fetchone()[0],
                "storage": "verbatim_native_raw_json_compressed"}
