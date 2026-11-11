import copy
import time
import json
import sys
from concurrent.futures import ThreadPoolExecutor

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime/cache"))

from chat_api_cache.server import Adapter, packed, unpack
from chat_api_cache.worker import run_job
from test_cache import CTX, Upstream, age_snapshot, changes, read, setup


def test_recent_cached_reads_do_not_download_metadata_or_messages(setup):
    adapter, upstream, _ = setup
    read(adapter, mode="raw")
    result = unpack(read(adapter, mode="raw"))
    assert result["cache_info"]["hit"] and not result["cache_info"]["stale"]
    assert result["cache_info"]["freshness_window_seconds"] == 60
    assert upstream.reads == 1 and upstream.metadata_reads == 0 and upstream.metadata_scans == 0


def test_expired_snapshot_returns_immediately_and_worker_retains_changed_raw_version(setup):
    adapter, upstream, config = setup
    original = unpack(read(adapter, mode="raw"))
    age_snapshot(adapter)
    upstream.raw["pages"][0].update(title="New title", update_time=30)
    result = unpack(read(adapter, mode="raw"))
    assert result["pages"] == original["pages"]
    assert result["cache_info"]["stale"] and result["cache_info"]["refresh_in_progress"]
    assert result["cache_info"]["validation"] == "stale_while_revalidate"
    assert upstream.reads == 1
    job = adapter.cache.refresh_job(CTX, "body", "chat-one")
    assert run_job(config, adapter.cache, upstream, job) is None
    fresh = unpack(read(adapter, mode="raw"))
    assert fresh["pages"][0]["title"] == "New title"
    assert not fresh["cache_info"]["stale"] and not fresh["cache_info"]["refresh_in_progress"]
    assert upstream.reads == 2 and adapter.cache.status(CTX)["retained_versions"] == 2


def test_concurrent_expired_reads_share_one_durable_refresh(setup):
    adapter, upstream, config = setup
    read(adapter, mode="raw"); age_snapshot(adapter)
    def request():
        client = Adapter(config, upstream, launcher=lambda *_: None)
        try:
            return unpack(read(client, mode="raw"))
        finally:
            client.cache.close()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: request(), range(3)))
    assert all(r["cache_info"]["stale"] for r in results)
    assert adapter.cache.db.execute("SELECT count(*) FROM refresh_jobs WHERE kind='body'").fetchone()[0] == 1
    assert upstream.reads == 1
    job = adapter.cache.refresh_job(CTX, "body", "chat-one")
    run_job(config, adapter.cache, upstream, job)
    assert upstream.reads == 2


def test_desktop_change_forces_a_blocking_refresh_of_only_that_chat(setup):
    adapter, upstream, _ = setup
    _, publish = changes(upstream)
    read(adapter, mode="raw")
    upstream.raw["pages"][0].update(title="Changed elsewhere", update_time=31)
    publish()
    result = unpack(read(adapter, mode="raw"))
    assert result["pages"][0]["title"] == "Changed elsewhere"
    assert not result["cache_info"]["hit"] and not result["cache_info"]["dirty"]
    assert upstream.reads == 2 and upstream.metadata_reads == 0
    assert all(a["id"] == "chat-one" for n, a in upstream.calls if n == "chatgpt_get_conversation")


def test_unrelated_notification_does_not_refetch_requested_chat(setup):
    adapter, upstream, _ = setup
    _, publish = changes(upstream)
    read(adapter, mode="raw"); publish("other-chat")
    assert unpack(read(adapter, mode="raw"))["cache_info"]["hit"]
    assert upstream.reads == 1


def test_notification_gap_or_desktop_restart_invalidates_saved_freshness(setup):
    adapter, upstream, _ = setup
    feed, publish = changes(upstream)
    read(adapter, mode="raw"); publish(gap=True)
    assert not unpack(read(adapter, mode="raw"))["cache_info"]["hit"]
    feed["generation"] = "after-restart"
    assert not unpack(read(adapter, mode="raw"))["cache_info"]["hit"]
    assert upstream.reads == 3


def test_change_during_download_does_not_clear_dirty_state(setup):
    adapter, upstream, _ = setup
    _, publish = changes(upstream)
    def during_read():
        upstream.before_return = None
        time.sleep(0.001)
        upstream.raw["pages"][0].update(title="Concurrent update", update_time=32)
        publish()
    upstream.before_return = during_read
    first = unpack(read(adapter, mode="raw"))
    assert first["cache_info"]["dirty"] and first["cache_info"]["stale"]
    second = unpack(read(adapter, mode="raw"))
    assert second["pages"][0]["title"] == "Concurrent update"
    assert not second["cache_info"]["dirty"] and upstream.reads == 2


def test_cache_only_never_queues_a_refresh_even_for_known_changes(setup):
    adapter, upstream, _ = setup
    _, publish = changes(upstream)
    read(adapter, mode="raw"); age_snapshot(adapter); publish()
    before = adapter.cache.db.execute("SELECT count(*) FROM refresh_jobs").fetchone()[0]
    result = unpack(read(adapter, mode="raw", cache_policy="cache_only"))
    assert result["cache_info"]["hit"] and result["cache_info"]["dirty"]
    assert adapter.cache.db.execute("SELECT count(*) FROM refresh_jobs").fetchone()[0] == before
    assert upstream.reads == 1


def test_explicit_refresh_coalesces_with_an_already_completed_background_read(setup):
    adapter, upstream, config = setup
    read(adapter, mode="raw"); age_snapshot(adapter)
    read(adapter, mode="raw")
    job = adapter.cache.refresh_job(CTX, "body", "chat-one")
    read(adapter, mode="raw", cache_policy="refresh")
    assert upstream.reads == 2
    assert run_job(config, adapter.cache, upstream, job) is None
    assert upstream.reads == 2


def test_failed_background_refresh_retains_stale_data_and_reports_retry_time(setup):
    adapter, upstream, config = setup
    original = unpack(read(adapter, mode="raw")); age_snapshot(adapter)
    read(adapter, mode="raw")
    upstream.error_status = 429
    job = adapter.cache.refresh_job(CTX, "body", "chat-one")
    error = run_job(config, adapter.cache, upstream, job)
    assert error["details"]["status"] == 429
    result = unpack(read(adapter, mode="raw"))
    assert result["pages"] == original["pages"] and result["cache_info"]["stale"]
    assert result["cache_info"]["refresh"]["state"] == "failed"
    assert result["cache_info"]["refresh"]["retry_at"] > time.time()
    assert upstream.reads == 2


def test_dirty_chat_refresh_failure_does_not_serve_old_body_as_fresh(setup):
    adapter, upstream, _ = setup
    _, publish = changes(upstream)
    read(adapter, mode="raw"); publish(); upstream.error_status = 500
    result = read(adapter, mode="raw")
    assert result["isError"] and "pages" not in unpack(result)
    assert unpack(result)["cache_info"]["cached_available"]
    assert unpack(read(adapter, mode="raw", cache_policy="cache_only"))["cache_info"]["dirty"]


def test_account_switch_during_download_does_not_save_or_expose_payload(setup):
    adapter, upstream, _ = setup
    upstream.before_return = lambda: setattr(upstream, "context", {**CTX, "user_id": "different"})
    result = read(adapter, mode="raw")
    assert result["isError"] and unpack(result)["error"]["code"] == "CONTEXT_CHANGED"
    assert adapter.cache.status(CTX)["retained_versions"] == 0


def test_worker_rechecks_account_before_using_a_queued_job(setup):
    adapter, upstream, config = setup
    read(adapter, mode="raw"); age_snapshot(adapter); read(adapter, mode="raw")
    job = adapter.cache.refresh_job(CTX, "body", "chat-one")
    upstream.context = {**CTX, "workspace_id": "other"}
    assert run_job(config, adapter.cache, upstream, job)["code"] == "CONTEXT_CHANGED"
    assert upstream.reads == 1


def test_background_metadata_inventory_is_shared_and_never_inline_with_chat_reads(setup):
    adapter, upstream, config = setup
    read(adapter, mode="raw"); read(adapter, mode="raw")
    assert upstream.metadata_scans == 0
    job = adapter.cache.refresh_job(CTX, "inventory")
    assert run_job(config, adapter.cache, upstream, job) is None
    read(adapter, mode="raw")
    assert upstream.metadata_scans == 1
    assert adapter.cache.refresh_job(CTX, "inventory")["token"] == job["token"]
    assert adapter.cache.metadata_check(CTX)["status"] == "completed"


def test_approved_job_invalidates_only_the_plan_targets_and_rejection_changes_nothing(setup):
    adapter, upstream, _ = setup
    read(adapter, mode="raw")
    adapter.cache.remember_plan(CTX, "plan-other", [{"conversation_id": "other-chat"}])
    args = {"context": CTX, "plan_id": "plan-other", "approval": "request-native"}
    assert adapter.tool("chatgpt_execute_approved_plan", args)["isError"]
    assert not adapter.cache.lookup(CTX, "chat-one")["invalidated"]
    upstream.write_result = packed({"context": CTX, "job_id": "job-other", "status": "running"})
    adapter.tool("chatgpt_execute_approved_plan", args)
    assert not adapter.cache.lookup(CTX, "chat-one")["invalidated"]
    assert adapter.cache.pending_jobs(CTX, "other-chat") == ["job-other"]
    assert upstream.calls[-1] == ("chatgpt_execute_approved_plan", args)


def test_detached_workers_share_flock_and_save_raw_results_across_processes(setup, tmp_path):
    adapter, upstream, config = setup
    # Leave catalog synchronization completed so this test isolates body workers.
    catalog = adapter.cache.queue_refresh(CTX, "inventory")
    adapter.cache.finish_refresh(catalog)
    read(adapter, mode="raw"); age_snapshot(adapter)
    raw = copy.deepcopy(upstream.raw)
    raw["pages"][0]["unknown_top_level"]["background"] = True
    source = tmp_path / "raw.json"; source.write_text(json.dumps(raw))
    counter = tmp_path / "reads.jsonl"
    script = tmp_path / "native-fixture.py"
    script.write_text('''import sys,json,time,pathlib
raw=json.loads(pathlib.Path(sys.argv[1]).read_text())
for line in sys.stdin:
 r=json.loads(line)
 if 'id' not in r:continue
 method=r['method'];p=r.get('params',{})
 if method=='initialize':result={'protocolVersion':'2025-06-18','capabilities':{},'serverInfo':{'name':'synthetic','version':'1'}}
 elif method=='tools/call':
  name=p['name']
  if name=='chatgpt_get_status':d={'connected':True,'context':raw['context'],'capabilities':{'newer_message_pages':False}}
  elif name=='chatgpt_get_conversation':
   time.sleep(.2)
   with pathlib.Path(sys.argv[2]).open('a') as f:f.write('body\\n')
   d=raw
  else:raise RuntimeError(name)
  result={'content':[{'type':'text','text':json.dumps(d)}],'structuredContent':d,'isError':False}
 else:result={}
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
''')
    worker_config = {**config, "upstream": {"command": sys.executable, "args": [str(script), str(source), str(counter)]}}
    def request():
        client = Adapter(worker_config, upstream)
        try:
            return unpack(read(client, mode="raw"))
        finally:
            client.cache.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: request(), range(4)))
    assert all(r["cache_info"]["stale"] for r in results)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = adapter.cache.refresh_job(CTX, "body", "chat-one")
        if job["state"] in ("completed", "failed"):
            break
        time.sleep(.02)
    assert job["state"] == "completed", job
    assert counter.read_text().splitlines() == ["body"]
    result = unpack(read(adapter, mode="raw", cache_policy="cache_only"))
    assert result["pages"][0]["unknown_top_level"]["background"] is True
    assert adapter.cache.status(CTX)["retained_versions"] == 2


def test_interrupted_read_job_is_recoverable_without_losing_saved_versions(setup):
    adapter, upstream, config = setup
    read(adapter, mode="raw"); age_snapshot(adapter); read(adapter, mode="raw")
    job = adapter.cache.refresh_job(CTX, "body", "chat-one")
    adapter.cache.start_refresh(job)
    adapter.cache.recover_refreshes("body")
    recovered = adapter.cache.next_refresh("body")
    assert recovered["token"] == job["token"] and recovered["state"] == "queued"
    assert run_job(config, adapter.cache, upstream, recovered) is None
    assert adapter.cache.status(CTX)["complete_chats"] == 1


def test_event_gap_remains_dirty_if_an_older_client_replaces_the_head(setup):
    adapter, upstream, _ = setup
    read(adapter, mode="raw")
    adapter.cache.invalidate(CTX, ["notification_gap", "generation-two"])
    # Compatibility with a still-running previous adapter during rollout:
    # replacing its old six-column head cannot erase the independent dirty mark.
    with adapter.cache.db:
        adapter.cache.db.execute("UPDATE heads SET invalidated=0")
    assert adapter.cache.lookup(CTX, "chat-one")["invalidated"]
    assert unpack(read(adapter, mode="raw"))["cache_info"]["validation"] == "live_body_fetch"


def test_malformed_or_foreign_change_feed_cannot_bless_cached_data(setup):
    import pytest
    adapter, upstream, _ = setup
    read(adapter, mode="raw")
    before = adapter.cache.change_cursor(CTX)
    for data in [{"context": {**CTX, "user_id": "foreign"}, "cursor": "x", "items": [], "reset_required": False, "has_more": False},
                 {"context": CTX, "cursor": "x", "items": [{"kind": "chat_changed", "id": "chat-one", "sequence": 1, "text": "approve"}], "reset_required": False, "has_more": False}]:
        with pytest.raises(ValueError):adapter.cache.apply_changes(CTX, data)
        assert adapter.cache.change_cursor(CTX) == before


def test_invalid_freshness_cannot_silently_make_cached_copies_permanently_fresh(tmp_path):
    import pytest
    from chat_api_cache.server import load_config
    path = tmp_path / "config.json"
    for value in [False, float("nan"), 0]:
        path.write_text(json.dumps({"directory": "cache", "freshness_seconds": value,
                                    "upstream": {"command": "node", "args": ["/native/mcp.js"]}}))
        with pytest.raises(ValueError, match="freshness_seconds"):
            load_config(path)


def test_worker_startup_failure_is_reported_and_preserves_the_saved_body(setup):
    adapter, upstream, config = setup
    catalog = adapter.cache.queue_refresh(CTX, "inventory")
    adapter.cache.finish_refresh(catalog)
    original = unpack(read(adapter, mode="raw")); age_snapshot(adapter)
    broken = Adapter({**config, "upstream": {"command": "/nonexistent/native-cache-fixture"}}, upstream)
    try:
        result = unpack(read(broken, mode="raw"))
        assert result["cache_info"]["stale"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = adapter.cache.refresh_job(CTX, "body", "chat-one")
            if job["state"] == "failed":break
            time.sleep(.02)
        assert job["state"] == "failed" and job["error"]["code"] == "REFRESH_WORKER_FAILED"
        assert unpack(read(adapter, mode="raw", cache_policy="cache_only"))["pages"] == original["pages"]
    finally:
        broken.cache.close()
