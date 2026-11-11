from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
import tomllib
from pathlib import Path

ROOT = Path.cwd()


class NativeError(RuntimeError):
    def __init__(self, message, *, code=None, status=None, retry_at_ms=None, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_at_ms = retry_at_ms
        self.details = details or {}


READ_TOOLS = frozenset({"chatgpt_get_status", "chatgpt_list_conversations", "chatgpt_get_conversation",
                        "chatgpt_get_inventory_status", "chatgpt_get_job_status", "chatgpt_get_conversation_metadata", "chatgpt_get_changes"})


class NativeClient:
    """Call only read tools through the existing native MCP server, never raw backend routes."""

    def __init__(self, workspace: Path = ROOT, timeout: float = 480, *, conversation_cache_policy: str | None = None):
        settings = tomllib.loads((workspace / ".codex/config.toml").read_text())["mcp_servers"]["chatgpt_conversations"]
        if not settings.get("enabled", True):
            raise NativeError("The native conversation API is disabled in this workspace")
        self.timeout = timeout
        self.workspace = Path(workspace)
        self.conversation_cache_policy = conversation_cache_policy
        self.sequence = 0
        self.buffer = bytearray()
        self.process = subprocess.Popen([settings["command"], *settings.get("args", [])],
                                        cwd=settings.get("cwd", str(workspace)),
                                        env={**os.environ, **settings.get("env", {})},
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "chatgpt-local-search", "version": "0.1.0"}})
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.selector.close()
        if self.process.stdin:
            self.process.stdin.close()
        if self.process.stdout:
            self.process.stdout.close()

    def send(self, message: dict):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def rpc(self, method: str, params: dict) -> dict:
        self.sequence += 1
        request_id = self.sequence
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            while b"\n" in self.buffer:
                line, _, self.buffer = self.buffer.partition(b"\n")
                if len(line) > 64 * 1024 * 1024:
                    self.buffer.clear(); self.close()
                    raise NativeError("Native response exceeds the 64 MiB limit", code="FRAME_TOO_LARGE")
                try:
                    response = json.loads(line)
                except ValueError:
                    raise NativeError("Invalid native MCP response") from None
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    raise NativeError(f"Native MCP error {response['error'].get('code')}")
                return response["result"]
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise NativeError("Native conversation API timed out")
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data:
                raise NativeError("Native conversation API disconnected")
            self.buffer.extend(data)
            if b"\n" not in self.buffer and len(self.buffer) > 64 * 1024 * 1024:
                self.buffer.clear(); self.close()
                raise NativeError("Native response exceeds the 64 MiB limit", code="FRAME_TOO_LARGE")

    def call(self, name: str, arguments: dict | None = None) -> dict:
        if name not in READ_TOOLS:
            raise NativeError("Local search can only call allow-listed native read tools")
        for attempt in range(4):
            result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
            data = result.get("structuredContent")
            if data is None:
                data = json.loads(next(c["text"] for c in result.get("content", []) if c.get("type") == "text"))
            if not result.get("isError") and "error" not in data:
                return data
            error = data.get("error", {})
            code = error.get("code", "UNKNOWN_ERROR")
            details = error.get("details") or {}
            status = details.get("status")
            status = status if isinstance(status, int) else None
            message = f"Native conversation API: {code}" + (f" (HTTP {status})" if status else "")
            failure = NativeError(message, code=code, status=status, retry_at_ms=details.get("retry_at_ms"), details=details)
            if code in ("RATE_LIMITED", "PACING_WAIT", "CAPABILITY_REQUIRED") or status == 429:
                # The importer owns cooldown/resumption; search callers receive a clear temporary error.
                raise failure
            if attempt == 3 or status not in (408, 429, 500, 502, 503, 504):
                raise failure
            retry_at = failure.retry_at_ms
            delay = max(2 ** attempt, retry_at / 1000 - time.time()) if isinstance(retry_at, (int, float)) else 2 ** attempt
            # Long server backoffs pause the import rather than retrying earlier than requested.
            if delay > 60:
                raise failure
            time.sleep(max(0, delay))
        raise failure

    def identity(self) -> dict:
        status = self.call("chatgpt_get_status")
        if not status.get("connected") or not status.get("context"):
            raise NativeError("Hydex Desktop must be running and signed in to ChatGPT")
        return status["context"]

    def inventory(self, context: dict, *, refresh: bool = True, progress=None, timeout: float = 900):
        page = self.call("chatgpt_list_conversations", {"context": context, "refresh": refresh, "limit": 1000})
        deadline = time.monotonic() + timeout
        while page.get("status") in ("running", "cancelling"):
            if time.monotonic() > deadline:
                raise NativeError("Inventory is still running; rerun sync to resume")
            status = self.call("chatgpt_get_inventory_status", {"context": context})
            if progress:
                progress({"phase": "inventory", "status": status.get("status"),
                          "conversations": status.get("unique_conversations", 0)})
            if status.get("status") not in ("running", "cancelling"):
                page = self.call("chatgpt_list_conversations", {"context": context, "limit": 1000})
                break
            time.sleep(2)
        if page.get("status") != "completed":
            raise NativeError("Native inventory did not complete; existing local data is retained")
        items = {item["id"]: item for item in page.get("items", [])}
        completeness = page.get("completeness", "unknown")
        while page.get("next_cursor"):
            page = self.call("chatgpt_list_conversations", {"context": context, "limit": 1000,
                                                          "cursor": page["next_cursor"]})
            items.update((item["id"], item) for item in page.get("items", []))
        return list(items.values()), completeness

    def conversation(self, context: dict, conversation_id: str) -> dict:
        arguments = {"context": context, "id": conversation_id, "mode": "normalized", "branch": "active"}
        if self.conversation_cache_policy is not None:
            arguments["cache_policy"] = self.conversation_cache_policy
        result = self.call("chatgpt_get_conversation", arguments)
        conversation = result.get("conversation")
        if not result.get("complete") or not conversation or not conversation.get("complete"):
            raise NativeError("Native API could not establish complete active-branch coverage")
        if conversation.get("id") != conversation_id or conversation.get("branch") != "active":
            raise NativeError("Native API returned a different conversation or branch")
        conversation["source_snapshot_sha256"] = (result.get("cache_info") or {}).get("snapshot_sha256")
        return conversation

    def indexing_conversation(self, context: dict, conversation_id: str) -> dict:
        """Read an explicit visible-text projection if a full view exceeds the wire limit."""
        try:
            return NativeClient.conversation(self, context, conversation_id)
        except NativeError as error:
            if error.code != "RESPONSE_TOO_LARGE" or not error.details.get("snapshot_sha256"):
                raise
            if self.identity() != context:
                raise NativeError("Account changed before reading the indexing projection", code="CONTEXT_CHANGED")
            from .cache import Cache
            from .server import load_config
            settings = load_config(self.workspace / "api-cache-config.json")
            cache = Cache(settings["directory"])
            try:
                sha = error.details["snapshot_sha256"]
                raw = cache.snapshot(context, conversation_id, sha)
            finally:
                cache.close()
            result = subprocess.run([settings["upstream"]["command"], str(Path(__file__).with_name("normalize_snapshot.cjs")),
                                     settings["normalizer"]], input=json.dumps({"raw": raw, "branch": "active",
                                     "projection": "index_text", "snapshot_sha256": sha}).encode(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if result.returncode:
                raise NativeError("Local indexing projection failed")
            projected = json.loads(result.stdout)
            if "error" in projected:
                raise NativeError("Local indexing projection is unavailable: " + projected["error"]["code"])
            conversation = projected["conversation"]
            conversation["source_snapshot_sha256"] = sha
            if conversation.get("id") != conversation_id or not conversation.get("complete"):
                raise NativeError("Local indexing projection has incomplete or different source data")
            return conversation
