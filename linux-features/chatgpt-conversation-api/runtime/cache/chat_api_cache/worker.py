"""Read-only native refresh worker. No backend mutation tool can be dispatched."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chat_api_cache.cache import Cache
from chat_api_cache.server import Adapter, unpack
from chat_api_cache.transport import StdioClient
from chat_api_cache.transport import StdioProtocolError


class ReadFailure(RuntimeError):
    def __init__(self, error):
        self.error = error


def native_read(upstream, name, arguments, context):
    allowed = {"chatgpt_list_conversations", "chatgpt_get_inventory_status", "chatgpt_get_job_status"}
    if name not in allowed:
        raise ValueError("Not a background inventory read")
    result = upstream.tool(name, arguments)
    data = unpack(result)
    if result.get("isError") or "error" in data:
        raise ReadFailure(data.get("error") or {"code": "INVENTORY_FAILED"})
    if data.get("context") != context:
        raise ReadFailure({"code": "CONTEXT_CHANGED", "message": "Inventory account/workspace changed"})
    return data


def refresh_inventory(config, cache, upstream, context):
    adapter = Adapter(config, upstream, cache=cache)
    if adapter.identity(context) is None:
        raise ReadFailure({"code": "CONTEXT_CHANGED", "message": "The authenticated account/workspace changed"})
    started = time.time()
    page = native_read(upstream, "chatgpt_list_conversations", {"context": context, "refresh": True, "limit": 1000}, context)
    job_id = page["inventory_job_id"]
    completed = False
    try:
        _collect_inventory(config, cache, upstream, context, adapter, page, job_id, started)
        completed = True
    finally:
        if not completed:
            # Cancel only this account-bound read job. Never dispatch a
            # conversation mutation or leave an unattended scan after failure.
            control = None
            try:
                control = StdioClient(config["upstream"], timeout=8)
                control.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "hydex-stop-inventory", "version": "5"}})
                control.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                control.tool("chatgpt_cancel_job", {"context": context, "job_id": job_id})
            except Exception:
                cache.suspend_reads(context, code="INVENTORY_CANCEL_UNCONFIRMED",
                                    message="Read-job cancellation could not be confirmed; verify native job state before resuming")
            finally:
                if control:control.close()


def _collect_inventory(config, cache, upstream, context, adapter, page, job_id, started):
    while page.get("status") in ("running", "cancelling"):
        if cache.read_control(context):
            raise ReadFailure({"code": "READS_PAUSED", "message": "Native reads were suspended"})
        if time.time() - started > config.get("inventory_timeout_seconds", 1800):
            raise ReadFailure({"code": "INVENTORY_TIMEOUT", "message": "Shared metadata synchronization is still running"})
        status = native_read(upstream, "chatgpt_get_inventory_status", {"context": context}, context)
        if status.get("status") not in ("running", "cancelling"):
            page = native_read(upstream, "chatgpt_list_conversations", {"context": context, "limit": 1000}, context)
            break
        time.sleep(1)
    if page.get("status") != "completed":
        stopped = native_read(upstream, "chatgpt_get_job_status", {"context": context, "job_id": job_id}, context)
        detail = stopped.get("error") or {"code": "INCOMPLETE_INVENTORY", "message": "Native inventory did not complete"}
        raise ReadFailure(detail)
    items = list(page.get("items", []))
    cursors = set()
    while page.get("next_cursor"):
        cursor = page["next_cursor"]
        if cursor in cursors:
            raise ReadFailure({"code": "INVALID_CURSOR", "message": "Inventory pagination did not advance"})
        cursors.add(cursor)
        page = native_read(upstream, "chatgpt_list_conversations", {"context": context, "limit": 1000, "cursor": cursor}, context)
        if page.get("inventory_job_id") != job_id or page.get("status") != "completed":
            raise ReadFailure({"code": "CONTEXT_CHANGED", "message": "Inventory snapshot changed during pagination"})
        items.extend(page.get("items", []))
    job = native_read(upstream, "chatgpt_get_job_status", {"context": context, "job_id": job_id}, context)
    if job.get("status") != "completed" or adapter.identity(context) is None:
        raise ReadFailure({"code": "CONTEXT_CHANGED", "message": "Inventory changed before saving metadata"})
    observed = datetime.fromisoformat(job["finished_at"].replace("Z", "+00:00")).timestamp()
    cache.finish_metadata_check(context, items, job_id, started, observed, page.get("completeness", "unknown"))


def run_job(config, cache, upstream, job):
    if cache.read_control(job["context"]):
        error = {"code": "READS_PAUSED", "message": "Native reads are suspended"}
        cache.pause_refresh(job, error)
        return error
    cache.start_refresh(job)
    error = None
    try:
        if job["kind"] == "body":
            adapter = Adapter(config, upstream, cache=cache)
            result = adapter.fetch_body(job["context"], job["id"], job["requested_at"])
            if result is not None:
                raise ReadFailure(unpack(result).get("error") or {"code": "REFRESH_FAILED"})
        elif job["kind"] == "inventory":
            refresh_inventory(config, cache, upstream, job["context"])
        else:
            raise ValueError("Not a background read job")
    except ReadFailure as failure:
        error = failure.error
    except (StdioProtocolError, TimeoutError) as failure:
        error = unpack(Adapter(config, upstream, cache=cache).trip(job["context"], failure))["error"]
    except Exception as failure:
        error = {"code": "REFRESH_FAILED", "message": type(failure).__name__}
    temporary = error and (error.get("code") in ("PACING_WAIT", "RATE_LIMITED", "CAPABILITY_REQUIRED", "DESKTOP_DISCONNECTED", "SESSION_UNAVAILABLE")
                           or (error.get("details") or {}).get("status") in (429, 503))
    if (error and error.get("code") not in ("PACING_WAIT", "RATE_LIMITED", "CAPABILITY_REQUIRED", "CONTEXT_CHANGED", "SESSION_UNAVAILABLE", "READS_PAUSED", "READ_CIRCUIT_OPEN", "RESOURCE_SCOPE_MISMATCH")
            and (error.get("details") or {}).get("status") not in (404, 429)):
        error = unpack(Adapter(config, upstream, cache=cache).trip(job["context"], StdioProtocolError(error.get("code", "REFRESH_FAILED"), "Native refresh stopped")))["error"]
    if error and error.get("code") in ("READS_PAUSED", "READ_CIRCUIT_OPEN"):
        cache.pause_refresh(job, error)
    elif temporary:
        cache.defer_refresh(job, error)
    else:
        cache.finish_refresh(job, error, retry_delay=config.get("inventory_refresh_seconds", 1800) if job["kind"] == "inventory" else 30)
    return error


def main():
    kind, descriptor = sys.argv[1], int(sys.argv[2])
    if kind not in ("body", "inventory"):
        raise ValueError("Invalid refresh worker kind")
    config = json.load(sys.stdin)
    cache = Cache(config["directory"])
    upstream = None
    try:
        cache.recover_refreshes(kind)  # exclusive inherited worker flock
        upstream = StdioClient(config["upstream"], timeout=480)
        upstream.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "hydex-chat-cache-worker", "version": "2"}})
        upstream.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        while True:
            with cache.queue_lock():
                job = cache.next_refresh(kind)
                if job is None:
                    deadline = cache.next_refresh_deadline(kind)
                    if deadline is not None:
                        job = False
                    else:
                    # Release under the queue mutex, so an enqueue cannot miss
                    # the departing worker and leave its request stranded.
                        os.close(descriptor)
                        descriptor = None
                        break
            if job is False:
                time.sleep(min(1, max(.05, deadline - time.time())))
                continue
            error = run_job(config, cache, upstream, job)
    except Exception as failure:
        # Startup/protocol failures must not leave a permanently "queued" job
        # after this worker has disappeared. Do not expose subprocess stderr.
        with cache.queue_lock():
            while (job := cache.next_refresh(kind)) is not None:
                cache.finish_refresh(job, {"code": "REFRESH_WORKER_FAILED", "message": type(failure).__name__},
                                     retry_delay=config.get("inventory_refresh_seconds", 1800) if kind == "inventory" else 30)
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
    finally:
        if upstream:
            upstream.close()
        cache.close()
        if descriptor is not None:
            os.close(descriptor)


if __name__ == "__main__":
    main()
