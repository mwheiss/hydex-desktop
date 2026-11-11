from __future__ import annotations

import json
import os
import selectors
import subprocess
import time


class StdioProtocolError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class StdioClient:
    """Dependency-free JSON-lines MCP transport to the existing native server."""

    def __init__(self, settings, timeout=120):
        self.timeout, self.sequence = timeout, 0
        self.buffer = bytearray()
        self.process = subprocess.Popen([settings["command"], *settings.get("args", [])],
                                        cwd=settings.get("cwd"), env={**os.environ, **settings.get("env", {})},
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def send(self, data):
        self.process.stdin.write((json.dumps(data, ensure_ascii=False) + "\n").encode())
        self.process.stdin.flush()

    def rpc(self, method, params=None):
        self.sequence += 1
        request_id = self.sequence
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            while b"\n" in self.buffer:
                line, _, self.buffer = self.buffer.partition(b"\n")
                if len(line) > 64 * 1024 * 1024:
                    self.buffer.clear(); self.close()
                    raise StdioProtocolError("UPSTREAM_FRAME_TOO_LARGE", "Native upstream frame exceeds 64 MiB")
                response = json.loads(line)
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    raise RuntimeError(f"Native MCP error {response['error'].get('code')}")
                return response["result"]
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("Native conversation API timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("Native conversation API disconnected")
            self.buffer.extend(chunk)
            if b"\n" not in self.buffer and len(self.buffer) > 64 * 1024 * 1024:
                self.buffer.clear(); self.close()
                raise StdioProtocolError("UPSTREAM_FRAME_TOO_LARGE", "Native upstream frame exceeds 64 MiB")

    def tool(self, name, arguments):
        return self.rpc("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.selector.close()
        self.process.stdin.close()
        self.process.stdout.close()
