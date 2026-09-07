#!/usr/bin/python3
"""Install/ensure the per-user Remote trust-race watcher service."""

import argparse
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

FEATURE = "remote-trust-race-workaround"
UNIT = "hydex-remote-trust-race-workaround.service"
MARKER = "# Managed by hydex-desktop remote-trust-race-workaround v1\n"


def absolute(value):
    value = str(value)
    if not value or any(c in value for c in "\x00\r\n") or not Path(value).is_absolute():
        raise ValueError("Expected an absolute, single-line path: " + repr(value))
    return Path(value).resolve()


def owned_file(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise ValueError("Refusing unsafe or foreign file: " + str(path))


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path):
        owned_file(path)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def quote_unit(value, *, argument=False):
    value = str(value)
    if any(c in value for c in "\x00\r\n"):
        raise ValueError("Invalid systemd value")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if argument:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def unit_path():
    config_home = absolute(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return config_home / "systemd/user" / UNIT


def watcher_path(app_dir):
    return app_dir / ".codex-linux/features" / FEATURE / "watcher.py"


def unit_text(app_dir):
    watcher = watcher_path(app_dir)
    command = " ".join(quote_unit(v, argument=True) for v in (sys.executable, watcher))
    return MARKER + f"""[Unit]
Description=Hydex Remote generated-path trust race workaround
Documentation=https://github.com/openai/codex/issues/39678
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
ExecStart={command}
Restart=on-failure
RestartSec=1
UMask=0077
StandardInput=null
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def run(args, *, check=True):
    return subprocess.run(
        args,
        check=check,
        universal_newlines=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def ensure(app_dir):
    if os.getuid() == 0:
        raise ValueError("Run as the ordinary desktop user, not root or sudo")
    app = absolute(app_dir)
    watcher = watcher_path(app)
    if not watcher.is_file() or not os.access(watcher, os.X_OK):
        raise ValueError("Missing packaged watcher: " + str(watcher))

    unit = unit_path()
    desired = unit_text(app)
    changed = False
    if os.path.lexists(unit):
        owned_file(unit)
        current = unit.read_text()
        if not current.startswith(MARKER):
            raise ValueError("Refusing to overwrite an existing user service: " + str(unit))
        if current != desired:
            atomic_write(unit, desired)
            changed = True
    else:
        atomic_write(unit, desired)
        changed = True

    if changed:
        run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", UNIT])


def remove():
    unit = unit_path()
    if not os.path.lexists(unit):
        return
    owned_file(unit)
    if not unit.read_text().startswith(MARKER):
        raise ValueError("Refusing to remove an unrecognized user service")
    run(["systemctl", "--user", "disable", "--now", UNIT], check=False)
    unit.unlink()
    run(["systemctl", "--user", "daemon-reload"], check=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("ensure", "remove"))
    parser.add_argument("--app-dir", default="/opt/hydex-desktop")
    args = parser.parse_args(argv)
    if args.action == "ensure":
        ensure(args.app_dir)
    else:
        remove()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(FEATURE + ": " + str(error), file=sys.stderr)
        sys.exit(78)
