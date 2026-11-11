"""Validate one requested conversation through the native metadata-only batch read."""
from __future__ import annotations

import math
import time
import uuid
from datetime import datetime


class MetadataCheckError(RuntimeError):
    def __init__(self, error):
        self.error = error
        super().__init__(error.get("message", "Metadata validation failed"))


def data_from(response):
    if "structuredContent" in response:
        return response["structuredContent"]
    import json
    return json.loads(next(c["text"] for c in response.get("content", []) if c.get("type") == "text"))


def has_update_marker(item):
    value = item.get("updated_at") if item else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return math.isfinite(value)
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            pass
    return False


class MetadataValidator:
    def __init__(self, config, upstream, cache):
        self.config, self.upstream, self.cache = config, upstream, cache

    def validate(self, context, conversation_id, request_started):
        timeout = self.config.get("metadata_check_timeout_seconds", 45)
        started_at = time.time()
        check_id = str(uuid.uuid4())
        try:
            # Coalesce only requests for this chat; never scan the account.
            with self.cache.metadata_lock(context, timeout, conversation_id):
                previous = self.cache.targeted_check(context, conversation_id)
                if previous and previous["checked_at"] >= request_started:
                    if previous["status"] == "failed":
                        raise MetadataCheckError(previous["error"])
                    return previous
                if previous and previous["status"] == "failed":
                    retry = ((previous["error"] or {}).get("details") or {}).get("retry_at_ms")
                    if isinstance(retry, (int, float)) and retry > time.time() * 1000:
                        raise MetadataCheckError(previous["error"])
                try:
                    response = self.upstream.tool("chatgpt_get_conversation_metadata", {"context": context, "id": conversation_id})
                    data = data_from(response)
                    if not response.get("isError") and "error" not in data and data.get("context") != context:
                        raise MetadataCheckError({"code": "CONTEXT_CHANGED", "message": "The targeted metadata response has a different account/workspace"})
                    self.cache.record_metadata(context, conversation_id, response)
                    if response.get("isError") or "error" in data:
                        raise MetadataCheckError(data.get("error") or {"code": "METADATA_CHECK_FAILED", "message": "Targeted metadata read failed"})
                    if time.time() - started_at > timeout:
                        raise MetadataCheckError({"code": "METADATA_CHECK_TIMEOUT", "message": "The requested chat metadata read timed out"})
                    metadata = data.get("metadata") if data["exists"] else None
                    if metadata is not None:
                        observed_at = datetime.fromisoformat(data["fetched_at"].replace("Z", "+00:00")).timestamp() if data.get("fetched_at") else time.time()
                        self.cache.observe(context, [metadata], observed_at)
                    return self.cache.save_targeted_check(context, conversation_id, check_id, started_at, metadata=metadata)
                except MetadataCheckError as error:
                    self.cache.save_targeted_check(context, conversation_id, check_id, started_at, error=error.error)
                    raise
                except Exception as error:
                    detail = {"code": "METADATA_CHECK_FAILED", "message": f"Targeted metadata validation failed ({type(error).__name__})"}
                    self.cache.save_targeted_check(context, conversation_id, check_id, started_at, error=detail)
                    raise MetadataCheckError(detail) from None
        except TimeoutError as error:
            raise MetadataCheckError({"code": "METADATA_CHECK_TIMEOUT", "message": str(error)}) from None
