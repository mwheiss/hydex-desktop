"""Shared durable read queues; one detached worker per kind, guarded by flock."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
from pathlib import Path


def launch_worker(config, kind, descriptor):
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("worker.py")), kind, str(descriptor)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        pass_fds=(descriptor,), start_new_session=True)
    try:
        process.stdin.write(json.dumps(config).encode())
        process.stdin.close()
    except BaseException:
        process.terminate()
        threading.Thread(target=process.wait, daemon=True).start()
        raise
    threading.Thread(target=process.wait, daemon=True).start()
    return process


def schedule(config, cache, context, kind, identifier="", *, interval=0, force=False, launcher=None):
    if kind not in ("body", "inventory"):
        raise ValueError("Only native read jobs can run in the background")
    control = cache.read_control(context)
    if control:
        return {"state": "paused", "retry_at": None, "error": {"code": "READS_PAUSED", "message": "Native reads are suspended"}}
    with cache.queue_lock():
        job = cache.queue_refresh(context, kind, identifier, interval=interval, force=force)
        if job["state"] not in ("queued", "running"):
            return job
        descriptor = os.open(cache.directory / ("worker-" + kind + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return job
            # The child inherits this open file description. Closing the parent
            # descriptor does not release the child's flock; do not LOCK_UN.
            (launcher or launch_worker)(config, kind, descriptor)
        except Exception as error:
            cache.finish_refresh(job, {"code": "REFRESH_START_FAILED", "message": type(error).__name__},
                                 retry_delay=config.get("inventory_refresh_seconds", 1800) if kind == "inventory" else 30)
        finally:
            os.close(descriptor)
    return cache.refresh_job(context, kind, identifier)
