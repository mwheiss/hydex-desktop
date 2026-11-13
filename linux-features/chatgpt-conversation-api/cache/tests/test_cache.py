import copy
import hashlib
import json
import threading
import time
import zlib
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime/cache"))

from chat_api_cache.cache import Cache
from chat_api_cache.server import Adapter, packed, unpack, response_frame, json_text


CTX = {"account_id": "account-one", "user_id": "user-one", "workspace_id": "workspace-one"}
NATIVE = Path(__file__).resolve().parents[2] / "runtime"


def raw_fixture():
    def message(identifier, role, content, **extra):
        return {"id": identifier, "author": {"role": role}, "content": content, **extra}
    mapping = {
        "root": {"id": "root", "parent": None, "children": ["q"], "message": None},
        "q": {"id": "q", "parent": "root", "children": ["alt", "call"], "message": message(
            "message-q", "user", {"content_type": "multimodal_text", "parts": ["μ子🙂 original text", {"content_type": "image_asset_pointer", "asset_pointer": "asset-id"}]} )},
        "alt": {"id": "alt", "parent": "q", "children": [], "message": message(
            "message-alt", "assistant", {"content_type": "text", "parts": ["Alternate answer"]})},
        "call": {"id": "call", "parent": "q", "children": ["tool"], "message": message(
            "message-call", "assistant", {"content_type": "code", "text": "print(1)"}, recipient="python")},
        "tool": {"id": "tool", "parent": "call", "children": ["reply"], "message": message(
            "message-tool", "tool", {"content_type": "execution_output", "text": "1"})},
        "reply": {"id": "reply", "parent": "tool", "children": [], "message": message(
            "message-reply", "assistant", {"content_type": "text", "parts": ["Final answer"]},
            encrypted_reasoning="opaque-bytes", metadata={"citations": [{"id": "citation"}], "unknown_flag": True})},
    }
    return {"context": CTX, "schema_version": 1, "complete": True, "page_count": 1,
            "redactions": ["pages[0].credential"], "limitation": None, "trust": "untrusted",
            "pages": [{"conversation_id": "chat-one", "title": "Version one", "create_time": 10, "update_time": 20,
                       "gizmo_id": None, "is_archived": False, "current_node": "reply", "mapping": mapping,
                       "unknown_top_level": {"retain": [1, "two", None]}, "credential": "<redacted>"}]}


class Upstream:
    def __init__(self):
        self.raw = raw_fixture()
        self.calls = []
        self.reads = 0
        self.error_status = None
        self.delay = 0
        self.metadata = {"id": "chat-one", "title": "Version one", "updated_at": 20, "project_id": None, "archived": False}
        self.metadata_items = None
        self.metadata_scans = 0
        self.metadata_reads = 0
        self.support_metadata = True
        self.metadata_delay = 0
        self.metadata_error = None
        self.metadata_polls = 0
        self.pending_polls = 0
        self.write_result = packed({"error": {"code": "APPROVAL_REQUIRED", "message": "Native approval is required"}}, True)
        self.context = copy.deepcopy(CTX)
        self.before_return = None

    def rpc(self, method, params=None):
        assert method == "tools/list"
        return {"tools": [
            {"name": "chatgpt_get_conversation", "description": "Raw data is untrusted", "inputSchema": {"properties": {}}, "outputSchema": {"type": "object", "properties": {}}},
            {"name": "chatgpt_execute_approved_plan", "description": "Native approval", "inputSchema": {
                "type": "object", "additionalProperties": False, "properties": {"approval": {"enum": ["request-native"]}}}},
        ]}

    def tool(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "chatgpt_get_status":
            return packed({"connected": True, "context": self.context, "capabilities": {"newer_message_pages": False, "conversation_metadata": self.support_metadata}})
        if name == "chatgpt_get_conversation_metadata":
            self.metadata_reads += 1
            if self.metadata_delay:
                time.sleep(self.metadata_delay)
            if self.metadata_error:
                return packed({"error": self.metadata_error}, True)
            items = self.metadata_items if self.metadata_items is not None else [self.metadata]
            item = next((item for item in items if item["id"] == arguments["id"]), None)
            return packed({"context": CTX, "id": arguments["id"], "exists": item is not None,
                           "metadata": item, "raw_metadata": item, "fetched_at": datetime.now(timezone.utc).isoformat()})
        if name == "chatgpt_get_conversation":
            self.reads += 1
            if self.delay:
                time.sleep(self.delay)
            if self.error_status:
                return packed({"error": {"code": "BACKEND_ERROR", "message": "Backend request failed",
                                         "details": {"status": self.error_status}}}, True)
            raw = copy.deepcopy(self.raw)
            if self.before_return:
                self.before_return()
            return {"content": [{"type": "text", "text": json.dumps(raw, ensure_ascii=False, indent=2)}],
                    "structuredContent": raw, "isError": False}
        if name == "chatgpt_list_conversations":
            if arguments.get("refresh"):
                self.metadata_scans += 1
                if self.metadata_delay:
                    time.sleep(self.metadata_delay)
                if self.metadata_error:
                    return packed({"error": self.metadata_error}, True)
                self.pending_polls = self.metadata_polls
            return packed({"context": CTX, "status": "running" if self.pending_polls else "completed", "inventory_job_id": "inventory-one",
                           "items": [] if self.pending_polls else self.metadata_items if self.metadata_items is not None else [self.metadata],
                           "completeness": "unknown"})
        if name == "chatgpt_get_inventory_status":
            self.pending_polls = max(0, self.pending_polls - 1)
            return packed({"context": CTX, "id": "inventory-one", "status": "running" if self.pending_polls else "completed"})
        if name == "chatgpt_get_job_status":
            return packed({"context": CTX, "id": "inventory-one", "kind": "inventory", "status": "completed",
                           "finished_at": datetime.now(timezone.utc).isoformat()})
        if name == "chatgpt_execute_approved_plan":
            return copy.deepcopy(self.write_result)
        raise AssertionError(name)


@pytest.fixture
def setup(tmp_path):
    config = {"directory": str(tmp_path / "cache"), "freshness_seconds": 60, "not_found_ttl_seconds": 3600,
              "metadata_poll_seconds": 0.001, "metadata_check_timeout_seconds": 5,
              "upstream": {"command": "/usr/bin/node"}, "normalizer": str(NATIVE / "normalize.js")}
    upstream = Upstream()
    adapter = Adapter(config, upstream, launcher=lambda *_: None)
    yield adapter, upstream, config
    adapter.cache.close()


def read_capabilities(upstream, revision):
    original = upstream.tool
    def tool(name, arguments):
        result = original(name, arguments)
        if name == 'chatgpt_get_status':
            data = unpack(result)
            data['capabilities'].update(conversation_read_context=True, conversation_read_revision=revision)
            return packed(data)
        return result
    upstream.tool = tool


def test_project_reads_require_bridge_capability_and_forward_only_validated_context(setup):
    adapter, upstream, _ = setup
    adapter.cache.observe(CTX, [{**upstream.metadata, 'project_id':'g-p-test', 'owner_id':'user-owner'}], time.time())
    assert unpack(read(adapter, mode='raw'))['error']['code']=='CAPABILITY_REQUIRED'
    assert upstream.reads==0
    read_capabilities(upstream, 2)
    assert not read(adapter, mode='raw')['isError']
    args = next(a for n,a in upstream.calls if n=='chatgpt_get_conversation')
    assert args['read_context']=={'project_id':'g-p-test','owner_id':'user-owner'}
    adapter.cache.observe(CTX, [{**upstream.metadata, 'project_id':'g-p-test', 'owner_id':{'untrusted':'value'}}], time.time())
    assert adapter.cache.read_context(CTX,'chat-one')=={'project_id':'g-p-test'}


def test_404_quarantine_survives_clients_and_expires_on_bridge_revision_or_metadata_change(setup):
    adapter, upstream, config = setup
    read_capabilities(upstream, 2)
    upstream.error_status=404
    assert read(adapter, mode='raw')['isError']
    second=Adapter(config,upstream,launcher=lambda *_:None)
    try:
        assert unpack(read(second,mode='raw'))['error']['details']['quarantined']
        assert upstream.reads==1
        read_capabilities(upstream,3)
        read(second,mode='raw');assert upstream.reads==2
        second.cache.observe(CTX,[{**upstream.metadata,'updated_at':21}],time.time())
        read(second,mode='raw');assert upstream.reads==3
        upstream.error_status=None
        assert not read(second,mode='raw',cache_policy='refresh')['isError']
        assert second.cache.db.execute('SELECT count(*) FROM fetch_failures').fetchone()[0]==0
    finally:second.cache.close()


def test_metadata_equivalent_timestamps_do_not_enqueue_false_updates(setup):
    adapter,upstream,_=setup
    read(adapter,mode='raw')
    before=len(adapter.cache.index_events(CTX))
    adapter.cache.observe(CTX,[{**upstream.metadata,'updated_at':'1970-01-01T00:00:20Z'}],time.time())
    assert len(adapter.cache.index_events(CTX))==before
    assert adapter.cache.lookup(CTX,'chat-one')['metadata_matches']


def test_legacy_inventory_bound_captures_do_not_require_a_refetch_for_body_time_drift(setup):
    adapter,upstream,_=setup
    read(adapter,mode='raw')
    # Cache revision 2 bound heads to the selected inventory hint, whose update
    # time can differ by seconds from the body time on the same backend record.
    item={**upstream.metadata,'updated_at':19}
    adapter.cache.observe(CTX,[item],time.time())
    with adapter.cache.db:
        adapter.cache.db.execute('UPDATE heads SET metadata_version=?',(adapter.cache.version(CTX,'chat-one'),))
    assert adapter.cache.lookup(CTX,'chat-one')['metadata_matches']
    assert unpack(read(adapter,mode='raw'))['cache_info']['hit'] and upstream.reads==1
    adapter.cache.observe(CTX,[{**item,'updated_at':'1970-01-01T00:00:19Z'}],time.time())
    assert adapter.cache.lookup(CTX,'chat-one')['metadata_matches']


def test_recurring_inventory_does_not_refetch_chats_with_different_body_timestamps(setup):
    adapter,upstream,_=setup
    inventory={**upstream.metadata,'updated_at':19}
    adapter.cache.observe(CTX,[inventory],time.time())
    read(adapter,mode='raw')
    assert adapter.cache.metadata_item(CTX,'chat-one')['updated_at']==19
    before=len(adapter.cache.index_events(CTX))
    adapter.cache.observe(CTX,[{**inventory,'updated_at':'1970-01-01T00:00:19Z'}],time.time())
    assert len(adapter.cache.index_events(CTX))==before
    assert unpack(read(adapter,mode='raw'))['cache_info']['hit'] and upstream.reads==1


def read(adapter, **options):
    return adapter.tool("chatgpt_get_conversation", {"context": CTX, "id": "chat-one", **options})


def age_snapshot(adapter, seconds=61):
    with adapter.cache.db:
        adapter.cache.db.execute("UPDATE heads SET fetched_at=fetched_at-?", (seconds,))
        adapter.cache.db.execute("UPDATE metadata SET observed_at=observed_at-?", (seconds,))


def changes(upstream):
    original = upstream.tool
    feed = {"generation": "one", "events": []}
    def tool(name, arguments):
        if name == "chatgpt_get_changes":
            upstream.calls.append((name, copy.deepcopy(arguments)))
            cursor = arguments.get("cursor")
            prefix = feed["generation"] + ":"
            reset = not cursor or not cursor.startswith(prefix)
            after = int(cursor.split(":")[-1]) if not reset else len(feed["events"])
            return packed({"context": upstream.context, "cursor": prefix + str(len(feed["events"])),
                           "reset_required": reset, "has_more": False,
                           "items": [] if reset else feed["events"][after:]})
        result = original(name, arguments)
        if name == "chatgpt_get_status":
            data = unpack(result)
            data["capabilities"]["conversation_changes"] = True
            return packed(data)
        return result
    upstream.tool = tool
    def publish(identifier="chat-one", *, gap=False):
        feed["events"].append({"sequence": len(feed["events"]) + 1, "kind": "gap" if gap else "chat_changed",
                               **({} if gap else {"id": identifier})})
    return feed, publish


def test_verbatim_raw_snapshot_preserves_unknown_fields_and_survives_restart(setup):
    adapter, upstream, config = setup
    response = read(adapter, mode="raw")
    data = unpack(response)
    assert not response["isError"] and not data["cache_info"]["hit"]
    assert data["pages"] == upstream.raw["pages"]
    assert next(args for name, args in upstream.calls if name == "chatgpt_get_conversation") == {"context": CTX, "id": "chat-one", "mode": "raw", "branch": "all"}
    saved = adapter.cache.db.execute("SELECT payload,sha FROM snapshots").fetchone()
    original = json.dumps(upstream.raw, ensure_ascii=False, indent=2).encode()
    assert zlib.decompress(saved["payload"]) == original
    assert saved["sha"] == hashlib.sha256(original).hexdigest()
    restarted = Adapter(config, upstream, launcher=lambda *_: None)
    try:
        again = unpack(read(restarted, mode="raw", cache_policy="cache_only"))
        assert again["cache_info"]["hit"] and upstream.reads == 1
        assert again["pages"] == upstream.raw["pages"]
    finally:
        restarted.cache.close()


@pytest.mark.skipif(not (NATIVE / "normalize.js").exists(), reason="Native normalizer not installed")
def test_one_raw_fetch_serves_active_all_and_raw_without_losing_tool_records(setup):
    adapter, upstream, _ = setup
    active = unpack(read(adapter))["conversation"]
    all_branches = unpack(read(adapter, branch="all"))["conversation"]
    raw = unpack(read(adapter, mode="raw"))
    assert upstream.reads == 1
    assert [m["id"] for m in active["messages"]] == ["message-q", "message-call", "message-tool", "message-reply"]
    assert {m["id"] for m in all_branches["messages"]} == {"message-q", "message-call", "message-tool", "message-reply", "message-alt"}
    assert any(m["kind"] == "tool_result" for m in all_branches["messages"])
    assert raw["pages"][0]["mapping"]["reply"]["message"]["encrypted_reasoning"] == "opaque-bytes"


def test_metadata_only_update_check_invalidates_changed_bodies(setup):
    adapter, upstream, _ = setup
    adapter.tool("chatgpt_list_conversations", {"context": CTX})
    read(adapter, mode="raw")
    adapter.tool("chatgpt_list_conversations", {"context": CTX})
    assert unpack(read(adapter, mode="raw"))["cache_info"]["hit"]
    assert upstream.reads == 1
    upstream.metadata = {**upstream.metadata, "title": "Version two", "updated_at": 21}
    upstream.raw["pages"][0].update(title="Version two", update_time=21)
    adapter.tool("chatgpt_list_conversations", {"context": CTX})
    response = unpack(read(adapter, mode="raw"))
    assert upstream.reads == 2 and response["pages"][0]["title"] == "Version two"
    assert adapter.cache.status(CTX)["retained_versions"] == 2


def test_explicit_refresh_and_cache_only_are_distinct(setup):
    adapter, upstream, _ = setup
    assert read(adapter, mode="raw", cache_policy="cache_only")["isError"]
    assert upstream.reads == 0
    read(adapter, mode="raw")
    read(adapter, mode="raw", cache_policy="refresh")
    assert upstream.reads == 2


def test_account_switch_cannot_expose_cached_data(setup):
    adapter, upstream, _ = setup
    read(adapter, mode="raw")
    result = adapter.tool("chatgpt_get_conversation", {"context": {**CTX, "user_id": "someone-else"},
                                                     "id": "chat-one", "mode": "raw", "cache_policy": "cache_only"})
    assert result["isError"] and unpack(result)["error"]["code"] == "CONTEXT_CHANGED"
    assert upstream.reads == 1 and "pages" not in unpack(result)


@pytest.mark.skipif(not (NATIVE / "normalize.js").exists(), reason="Native normalizer not installed")
def test_incomplete_snapshots_are_saved_before_normalization_fails(setup):
    adapter, upstream, _ = setup
    upstream.raw["complete"] = False
    upstream.raw["limitation"] = "Missing forward page"
    normalized = read(adapter)
    assert normalized["isError"]
    raw = unpack(read(adapter, mode="raw", cache_policy="cache_only"))
    assert raw["complete"] is False and raw["pages"] == upstream.raw["pages"]
    assert upstream.reads == 1 and adapter.cache.status(CTX)["complete_chats"] == 0


def test_known_404_is_temporarily_cached_and_does_not_erase_old_raw_copy(setup):
    adapter, upstream, _ = setup
    adapter.tool("chatgpt_list_conversations", {"context": CTX})
    read(adapter, mode="raw")
    upstream.error_status = 404
    assert read(adapter, mode="raw", cache_policy="refresh")["isError"]
    negative = read(adapter, mode="raw")
    assert negative["isError"] and unpack(negative)["cache_info"]["negative"]
    assert upstream.reads == 2
    old = unpack(read(adapter, mode="raw", cache_policy="cache_only"))
    assert old["pages"] == upstream.raw["pages"] and old["cache_info"]["stale"]


def test_write_arguments_and_native_approval_contract_pass_through_exactly(setup):
    adapter, upstream, _ = setup
    before = upstream.rpc("tools/list")["tools"][1]
    after = adapter.catalog()["tools"][1]
    assert before == after
    arguments = {"context": CTX, "approval": "request-native", "plan_id": "plan-one"}
    result = adapter.tool("chatgpt_execute_approved_plan", arguments)
    assert result == upstream.write_result
    assert upstream.calls[-1] == ("chatgpt_execute_approved_plan", arguments)
    assert adapter.cache.status(CTX)["current_chats"] == 0


def test_two_clients_share_one_upstream_read(setup):
    adapter, upstream, config = setup
    upstream.delay = 0.25
    def worker():
        client = Adapter(config, upstream, launcher=lambda *_: None)
        try:
            return read(client, mode="raw")
        finally:
            client.cache.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: worker(), range(2)))
    assert all(not response["isError"] for response in responses)
    assert upstream.reads == 1


def test_previous_cache_schema_gains_queues_without_changing_retained_raw_bytes(setup):
    adapter, upstream, config = setup
    original = unpack(read(adapter, mode="raw"))
    sha = original["cache_info"]["snapshot_sha256"]
    for table in ("dirty", "change_cursors", "plan_targets", "job_targets", "refresh_jobs"):
        adapter.cache.db.execute("DROP TABLE " + table)
    adapter.cache.db.commit()
    adapter.cache.close()
    adapter.cache = Cache(config["directory"])
    result = unpack(read(adapter, mode="raw", cache_policy="cache_only"))
    assert result["cache_info"]["snapshot_sha256"] == sha
    assert result["pages"] == original["pages"] and upstream.reads == 1
    assert adapter.cache.status(CTX)["background_refresh_jobs"] == 0


def test_snapshot_integrity_is_checked(setup):
    adapter, _, _ = setup
    read(adapter, mode="raw")
    with adapter.cache.db:
        adapter.cache.db.execute("UPDATE snapshots SET payload=?", (zlib.compress(b'{}'),))
    result=unpack(read(adapter, mode="raw"))
    assert result['error']['code']=='READ_CIRCUIT_OPEN'
    assert result['error']['details']['read_control']['error']['code']=='READ_PIPELINE_FAILED'


@pytest.mark.skipif(not (NATIVE / "normalize.js").exists(), reason="Native normalizer not installed")
def test_older_pages_use_original_final_header_for_normalization(setup):
    adapter, upstream, _ = setup
    def msg(id, text):
        return {"id": id, "author": {"role": "user"}, "content": {"content_type": "text", "parts": [text]}}
    older = {"messages": [msg("old-message", "Old text")], "page_info": {"has_previous_page": False, "has_next_page": True}}
    newest = {"conversation_id": "chat-one", "title": "Newest header", "is_archived": False, "gizmo_id": None,
              "messages": [msg("new-message", "New text")], "page_info": {"has_previous_page": True, "has_next_page": False}}
    upstream.raw.update(pages=[older, newest], page_count=2)
    result = unpack(read(adapter))["conversation"]
    assert result["title"] == "Newest header"
    assert [m["id"] for m in result["messages"]] == ["old-message", "new-message"]
    assert read(adapter, branch="all", cache_policy="cache_only")["isError"]
    assert upstream.reads == 1


def test_old_metadata_observation_cannot_roll_back_newer_version(setup):
    adapter, _, _ = setup
    new = {"id": "chat-one", "updated_at": 30, "title": "New"}
    old = {"id": "chat-one", "updated_at": 20, "title": "Old"}
    adapter.cache.observe(CTX, [new], 200)
    version = adapter.cache.version(CTX, "chat-one")
    adapter.cache.observe(CTX, [old], 100)
    assert adapter.cache.version(CTX, "chat-one") == version


def test_rejected_approval_cannot_invalidate_or_modify_cached_snapshot(setup):
    adapter, upstream, _ = setup
    read(adapter, mode="raw")
    sha = adapter.cache.lookup(CTX, "chat-one")["sha"]
    result = adapter.tool("chatgpt_execute_approved_plan", {"context": CTX, "approval": "request-native", "plan_id": "plan-one"})
    assert result["isError"]
    cached = unpack(read(adapter, mode="raw"))
    assert cached["cache_info"]["hit"] and cached["cache_info"]["snapshot_sha256"] == sha
    assert upstream.reads == 1


def test_first_metadata_scan_reuses_matching_previously_fetched_body(setup):
    adapter, upstream, _ = setup
    read(adapter, mode="raw")
    adapter.tool("chatgpt_list_conversations", {"context": CTX})
    assert unpack(read(adapter, mode="raw"))["cache_info"]["hit"]
    assert upstream.reads == 1








def test_explicit_snapshot_and_cache_only_do_not_start_metadata_checks(setup):
    adapter, upstream, _ = setup
    read(adapter, mode="raw")
    upstream.metadata_error = {"code": "UNAVAILABLE", "message": "No metadata"}
    assert unpack(read(adapter, mode="raw", cache_policy="cache_only"))["cache_info"]["hit"]
    snapshot = unpack(read(adapter, mode="raw", cache_policy="inventory_snapshot"))
    assert snapshot["cache_info"]["hit"] and snapshot["cache_info"]["validation"] == "inventory_snapshot"
    assert upstream.metadata_reads == 0 and upstream.metadata_scans == 0 and upstream.reads == 1
























def test_large_response_returns_one_bounded_error_and_preserves_next_frame():
    large = packed({"pages": ["x" * 20000], "cache_info": {"snapshot_sha256": "snapshot-one"}})
    first = response_frame({"jsonrpc": "2.0", "id": 1, "result": large}, limit=2048)
    second = response_frame({"jsonrpc": "2.0", "id": 2, "result": {}})
    frames = [json.loads(line) for line in (first + second).splitlines()]
    assert len(first.encode()) < 2048
    assert frames[0]["result"]["isError"]
    assert frames[0]["result"]["structuredContent"]["error"]["code"] == "RESPONSE_TOO_LARGE"
    assert frames[0]["result"]["structuredContent"]["error"]["details"]["snapshot_sha256"] == "snapshot-one"
    assert frames[1]["id"] == 2


def test_json_frame_preserves_unicode_and_lone_surrogates_without_inflation():
    text = "日本語🙂" * 100 + "\ud800"
    encoded = json_text({"text": text})
    assert json.loads(encoded)["text"] == text
    assert "日本語" in encoded and "\\ud800" in encoded


@pytest.mark.skipif(not (NATIVE / "normalize.js").exists(), reason="Native normalizer not installed")
def test_explicit_index_projection_matches_visible_text_and_keeps_raw_snapshot(setup):
    import subprocess
    adapter, upstream, _ = setup
    raw = unpack(read(adapter, mode="raw"))
    full = unpack(read(adapter, cache_policy="cache_only"))["conversation"]
    sha = raw["cache_info"]["snapshot_sha256"]
    process = subprocess.run(["node", str(NATIVE/'cache/chat_api_cache/normalize_snapshot.cjs'), str(NATIVE/'normalize.js')],
                             input=json.dumps({"raw": upstream.raw, "branch": "active", "projection": "index_text", "snapshot_sha256": sha}).encode(),
                             stdout=subprocess.PIPE, check=True)
    projected = json.loads(process.stdout)["conversation"]
    assert projected["projection"]["kind"] == "visible_message_text"
    assert [(m["id"],m["role"]) for m in projected["messages"]] == [("message-q","user"),("message-reply","assistant")]
    assert all(m["id"] in {x["id"] for x in full["messages"]} for m in projected["messages"])
    assert projected["messages"][0]["content"]["parts"] == ["μ子🙂 original text"]
    assert adapter.cache.snapshot(CTX,"chat-one",sha)["pages"] == upstream.raw["pages"]
