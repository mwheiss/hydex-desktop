"""Launch the optional shared cache against the original native Node bridge."""
import argparse
import os
import shutil
import sys
from pathlib import Path

from .server import load_config, serve
from .transport import StdioClient


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--native-runtime", type=Path)
    args = parser.parse_args(argv)
    config_path = args.config or Path.cwd() / "api-cache-config.json"
    if config_path.exists():
        config = load_config(config_path)
    else:
        here = Path(__file__).resolve().parent
        candidates = [args.native_runtime] if args.native_runtime else [
            here.parent.parent, here.parent.parent.parent / "runtime",
            Path("/opt/hydex-desktop/.codex-linux/features/chatgpt-conversation-api")]
        runtime = next((p for p in candidates if p and (p / "mcp.js").is_file() and (p / "normalize.js").is_file()), None)
        if runtime is None:
            raise RuntimeError("Supply --config or --native-runtime pointing to the native conversation API")
        state = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        config = {"directory": str(state / "hydex-desktop/conversation-cache"), "max_age_seconds": 300,
                  "metadata_check_timeout_seconds": 45, "not_found_ttl_seconds": 3600,
                  "normalizer": str(runtime / "normalize.js"),
                  "upstream": {"command": shutil.which("node") or "node", "args": [str(runtime / "mcp.js")],
                               "env": {"XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")}}}
    serve(config, StdioClient(config["upstream"]), sys.stdin.buffer, sys.stdout)
