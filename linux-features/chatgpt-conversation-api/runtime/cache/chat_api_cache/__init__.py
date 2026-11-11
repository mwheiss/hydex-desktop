"""Prefer a replayed native runtime that is at least as new as this fallback."""
import json
import os
from pathlib import Path


def _runtime_revision(directory):
    try:
        value = json.loads((directory / "runtime-version.json").read_text())["revision"]
        return value if type(value) is int and value >= 0 else 0
    except (OSError, ValueError, KeyError, TypeError):
        return 0


_local = Path(__file__).resolve().parent
_installed = Path("/opt/hydex-desktop/.codex-linux/features/chatgpt-conversation-api/cache/chat_api_cache")
if (not os.environ.get("HYDEX_API_CACHE_DEV") and _installed.is_dir() and _installed.resolve() != _local
        and _runtime_revision(_installed) >= _runtime_revision(_local)):
    __path__.insert(0, str(_installed))
