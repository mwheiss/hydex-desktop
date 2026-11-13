"""Launch the optional shared cache against the original native Node bridge."""
import argparse
import os
import shutil
import sys
import json
import re
from pathlib import Path

from .server import load_config, serve, native_transport_config
from .transport import StdioClient


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--native-runtime", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--pause-reads", action="store_true")
    action.add_argument("--read-status", action="store_true")
    action.add_argument("--probe-read")
    parser.add_argument("--resume-after-probe", action="store_true")
    args = parser.parse_args(argv)
    if args.resume_after_probe and not args.probe_read:
        parser.error("--resume-after-probe requires --probe-read")
    if args.probe_read and not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", args.probe_read):
        parser.error("--probe-read requires a native conversation ID")
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
        config = {"directory": str(state / "hydex-desktop/conversation-cache"), "freshness_seconds": 60,
                  "inventory_refresh_seconds": 1800,
                  "metadata_check_timeout_seconds": 45, "not_found_ttl_seconds": 3600,
                  "normalizer": str(runtime / "normalize.js"),
                  "upstream": {"command": shutil.which("node") or "node", "args": [str(runtime / "mcp.js")],
                               "env": {"XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")}}}
    native_transport_config(config)
    if args.pause_reads or args.read_status:
        from .cache import Cache
        cache=Cache(config['directory'])
        try:
            if args.pause_reads:cache.suspend_reads()
            print(json.dumps({'native_reads':cache.read_control(),'circuits':[dict(r) for r in cache.db.execute('SELECT state,error,at FROM read_controls')]}))
        finally:cache.close()
        return
    if args.probe_read:
        from .recovery import probe
        result=probe(config,args.probe_read,resume=args.resume_after_probe)
        print(json.dumps(result))
        if not result['complete']:raise SystemExit(2)
        return
    serve(config, StdioClient(config["upstream"]), sys.stdin.buffer, sys.stdout)
