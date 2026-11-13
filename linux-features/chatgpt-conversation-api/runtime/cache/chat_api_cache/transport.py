from __future__ import annotations

import json
import os
import re
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
        self.closed = False
        self.buffer = bytearray()
        self.process = subprocess.Popen([settings["command"], *settings.get("args", [])],
                                        cwd=settings.get("cwd"), env={**os.environ, **settings.get("env", {})},
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def send(self, data):
        if self.closed:
            raise StdioProtocolError("UPSTREAM_CLOSED", "The failed native connection has been discarded")
        try:
            self.process.stdin.write((json.dumps(data, ensure_ascii=False) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            self.close()
            raise StdioProtocolError("UPSTREAM_DISCONNECTED", "The native connection closed") from None

    def broken(self, code, message):
        self.close()
        raise StdioProtocolError(code, message)

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
                try:
                    response = json.loads(line)
                except (ValueError, UnicodeError):
                    self.broken("UPSTREAM_PROTOCOL_ERROR", "The native connection returned invalid JSON")
                if not isinstance(response, dict):
                    self.broken("UPSTREAM_PROTOCOL_ERROR", "The native connection returned an invalid response")
                if response.get("id") is None and "error" in response:
                    self.broken("UPSTREAM_PROTOCOL_ERROR", "The native MCP input/output stream failed")
                if response.get("id") != request_id:
                    if "id" not in response and isinstance(response.get("method"), str):
                        continue
                    self.broken("UPSTREAM_PROTOCOL_ERROR", "The native connection returned an unexpected response ID")
                if "error" in response:
                    self.broken("UPSTREAM_PROTOCOL_ERROR", "The native MCP request failed")
                result = response["result"]
                if isinstance(result, dict) and result.get("_meta", {}).get("hydex/structured-json-text") is True:
                    # Preserve the exact native structured JSON, including its
                    # number/Unicode serialization, without duplicating it on
                    # the MCP wire. The outer native key precedes its payload.
                    text = line.decode("utf-8")
                    match = re.search(r'(?<!\\)"structuredContent"\s*:\s*', text)
                    if match is None:
                        self.broken("UPSTREAM_PROTOCOL_ERROR", "The native structured payload is missing")
                    try:
                        value, end = json.JSONDecoder().raw_decode(text, match.end())
                    except ValueError:
                        self.broken("UPSTREAM_PROTOCOL_ERROR", "The native structured payload is invalid")
                    if value != result.get("structuredContent"):
                        self.broken("UPSTREAM_PROTOCOL_ERROR", "The native structured payload is inconsistent")
                    result["_native_json_text"] = text[match.end():end]
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                self.broken("NATIVE_READ_TIMEOUT", "The native read timed out; the connection was discarded")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                self.broken("UPSTREAM_DISCONNECTED", "The native connection closed before replying")
            self.buffer.extend(chunk)
            if b"\n" not in self.buffer and len(self.buffer) > 64 * 1024 * 1024:
                self.buffer.clear(); self.close()
                raise StdioProtocolError("UPSTREAM_FRAME_TOO_LARGE", "Native upstream frame exceeds 64 MiB")

    def tool(self, name, arguments):
        return self.rpc("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.selector.close()
        for stream in (self.process.stdin, self.process.stdout):
            try:stream.close()
            except OSError:pass
