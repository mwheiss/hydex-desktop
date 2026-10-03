"""Bounded, read-only activity checks against the private app-server socket."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import stat
import time


class DeadlineConnection:
    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Activity check timed out")
        self.connection.settimeout(remaining)

    def recv(self, size):
        self.remaining()
        return self.connection.recv(size)

    def sendall(self, data):
        self.remaining()
        return self.connection.sendall(data)

    def close(self):
        self.connection.close()


def probe(config, *, timeout=15):
    """Unknown status, unsupported APIs, pagination drift and errors block updates."""
    path = Path(config["codex_home"]) / "app-server-control/app-server-control.sock"
    if path.lstat().st_uid != os.getuid():
        raise ValueError("Foreign app-server socket link")
    target = path.resolve(strict=True)
    info, parent = target.lstat(), target.parent.stat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise ValueError("App-server socket must be owned by this user in a private directory")
    adapter = Path(config["app_dir"]) / ".codex-linux/features/persistent-app-server/codex-vscode-proxy"
    loader = importlib.machinery.SourceFileLoader("hydex_activity_adapter", str(adapter))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    client = importlib.util.module_from_spec(spec)
    loader.exec_module(client)
    client.MAX_MESSAGE_BYTES = 1024 * 1024
    deadline = time.monotonic() + timeout
    websocket = client.connect_unix_websocket(target, timeout=timeout)
    websocket.connection = DeadlineConnection(websocket.connection, deadline)
    request_id = 0

    def request(method, params):
        nonlocal request_id
        request_id += 1
        websocket.send_frame(1, json.dumps({"id": request_id, "method": method, "params": params}).encode())
        for _ in range(2048):
            raw = websocket.receive_text()
            if raw is None:
                raise EOFError("App-server closed the activity check")
            value = json.loads(raw)
            if value.get("id") != request_id:
                continue
            if "error" in value:
                raise ValueError(method + " is unavailable; activity is unknown")
            return value["result"]
        raise ValueError("Activity response limit exceeded")

    def loaded():
        threads, cursor = [], None
        for _ in range(11):
            result = request("thread/loaded/list", {"cursor": cursor, "limit": 100})
            threads.extend(result["data"])
            if len(threads) > 1000 or len(set(threads)) != len(threads):
                raise ValueError("Loaded-thread limit or pagination consistency check failed")
            cursor = result["nextCursor"]
            if cursor is None:
                return threads
        raise ValueError("Loaded-thread pagination limit exceeded")

    try:
        request("initialize", {
            "clientInfo": {"name": "hydex_service_update", "version": "1.0.0"},
            "capabilities": {"experimentalApi": True},
        })
        websocket.send_frame(1, b'{"method":"initialized"}')
        threads = loaded()
        for thread_id in threads:
            thread = request("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
            if thread["status"]["type"] != "idle":
                return {"idle": False, "reason": "A turn or pending request is active, or its status is unknown"}
            result = request("thread/backgroundTerminals/list", {
                "threadId": thread_id, "cursor": None, "limit": 1,
            })
            if result["data"] or result["nextCursor"] is not None:
                return {"idle": False, "reason": "A background command is still running"}
        if set(loaded()) != set(threads):
            return {"idle": False, "reason": "Loaded threads changed during the activity check"}
        return {"idle": True, "reason": "All loaded threads and background commands are idle"}
    finally:
        websocket.close()
