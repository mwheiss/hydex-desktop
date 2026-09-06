#!/usr/bin/python3
"""Native install wrapper and user-service manager. No Electron/IPC patches."""
import argparse
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile

FEATURE = "persistent-app-server"
UNIT = "codex-remote-control.service"
MARKER = "# Managed by codex-desktop-linux persistent-app-server v1\n"
INCOMPATIBLE = {"shared-app-server-socket"}


def run(args, *, check=True, capture=False, timeout=30, **kwargs):
    return subprocess.run(args, check=check, text=True, capture_output=capture,
                          timeout=timeout, **kwargs)


def absolute(value):
    value = str(value)
    if not value or any(c in value for c in "\x00\r\n") or not Path(value).is_absolute():
        raise ValueError("Expected an absolute, single-line path: " + repr(value))
    return Path(value).resolve()


def config_path():
    root = absolute(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "codex-desktop" / (FEATURE + ".json")


def owned_file(path, *, private=False):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise ValueError("Refusing unsafe or foreign file: " + str(path))
    if private and st.st_mode & 0o077:
        raise ValueError("Configuration must have mode 0600: " + str(path))


def atomic_write(path, data, mode=0o600):
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


def read_config(path):
    if not path.exists():
        raise ValueError("Service is not configured. Run: python3 " + str(Path(__file__)) + " setup")
    owned_file(path, private=True)
    config = json.loads(path.read_text())
    if config.get("version") != 1 or config.get("feature") != FEATURE:
        raise ValueError("Unrecognized persistent app-server configuration")
    for name in ("home", "codex_home", "app_dir", "unit_path"):
        config[name] = str(absolute(config[name]))
    if not isinstance(config.get("path"), str) or any(c in config["path"] for c in "\x00\r\n"):
        raise ValueError("Invalid saved PATH")
    return config


def socket_path(config):
    return Path(config["codex_home"]) / "app-server-control/app-server-control.sock"


def cli_path(config):
    # hydex-offload replaces this same executable when selected.
    return Path(config["app_dir"]) / "resources/codex"


def helper_path(config):
    return Path(config["app_dir"]) / ".codex-linux/features/persistent-app-server/manage.py"


def quote_unit(value, *, argument=False):
    value = str(value)
    if any(c in value for c in "\x00\r\n"):
        raise ValueError("Invalid systemd value")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if argument:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def unit_text(config, path):
    command = " ".join(quote_unit(v, argument=True) for v in
                       ("/usr/bin/python3", helper_path(config), "serve", "--config", path))
    return MARKER + f"""[Unit]
Description=Persistent Codex app-server with Remote Control
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
WorkingDirectory=%h
ExecStart={command}
Restart=on-failure
RestartPreventExitStatus=78
RestartSec=3
TimeoutStopSec=90
UMask=0077
StandardInput=null
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def enable_selection(repo):
    path = repo / "linux-features/features.json"
    config = json.loads(path.read_text()) if path.exists() else {"enabled": []}
    enabled = config.get("enabled", [])
    if not isinstance(enabled, list) or not all(isinstance(v, str) for v in enabled):
        raise ValueError("features.json enabled must be a string array")
    config["enabled"] = list(dict.fromkeys(
        [v for v in enabled if v not in INCOMPATIBLE] + ["remote-mobile-control", FEATURE]))
    backup = path.with_name("features.json.before-persistent-app-server")
    if path.exists() and not os.path.lexists(backup):
        atomic_write(backup, path.read_text())
    atomic_write(path, json.dumps(config, indent=2) + "\n")


def check_foreign_owner(config, unit):
    run(["systemctl", "--user", "show-environment"], capture=True)
    fragment = run(["systemctl", "--user", "show", UNIT, "--property=FragmentPath", "--value"],
                   check=False, capture=True).stdout.strip()
    if fragment and Path(fragment).resolve() != unit.resolve():
        raise ValueError("Another service already owns " + UNIT + ": " + fragment)
    if os.path.lexists(unit):
        owned_file(unit)
        if not unit.read_text().startswith(MARKER):
            raise ValueError("Refusing to overwrite an existing user service: " + str(unit))
    active = run(["systemctl", "--user", "is-active", "--quiet", UNIT],
                 check=False, capture=True).returncode == 0
    if active and not unit.exists():
        raise ValueError("A service is already running without our unit file")
    if not active and os.path.lexists(socket_path(config)):
        raise ValueError("Existing socket: " + str(socket_path(config)) +
                         ". Finish work and stop its owner first; no socket was removed.")
    # A detached updater could later reclaim the socket, even if it is absent now.
    for name in ("app-server.pid", "app-server-updater.pid"):
        path = Path(config["codex_home"]) / "app-server-daemon" / name
        if path.exists():
            metadata = json.loads(path.read_text())
            pid = metadata.get("pid") if isinstance(metadata, dict) else None
            if not isinstance(pid, int) or pid <= 0:
                raise ValueError("Inspect unrecognized daemon metadata: " + str(path))
            try:
                os.kill(pid, 0)  # Probe only. Never terminate a competing daemon.
            except ProcessLookupError:
                continue
            raise ValueError("A managed daemon/updater may still be running; stop it before setup: " + str(path))
    return active


def setup(app_dir, *, linger=True):
    path = config_path()
    app_dir = absolute(app_dir)
    proposed = {
        "version": 1, "feature": FEATURE, "home": str(Path.home().resolve()),
        "app_dir": str(app_dir),
        "codex_home": str(absolute(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))),
        "unit_path": str(path.parent.parent / "systemd/user" / UNIT),
        "path": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    # Reinstalling/updating preserves the established home/PATH instead of
    # silently moving an active server to a different profile.
    config = read_config(path) if path.exists() else proposed
    if config["app_dir"] != str(app_dir):
        raise ValueError("Configured for another app directory; remove the old service explicitly first")
    cli = cli_path(config)
    if not helper_path(config).is_file() or not os.access(cli, os.X_OK):
        raise ValueError("Install the package with this feature enabled before service setup")
    # Written by mobile's stage hook only after its launch transform is found.
    # The historical marker label is not the runtime owner in proxy mode.
    mobile_marker = app_dir / ".codex-linux/desktop-app-server-remote-control-enabled"
    if mobile_marker.is_symlink() or not mobile_marker.is_file() or mobile_marker.read_text() != "version=1\nowner=desktop\n":
        raise ValueError("Packaged mobile launch transform is not verified; rebuild with remote-mobile-control")
    unit = Path(config["unit_path"])
    active = check_foreign_owner(config, unit)
    if active and not path.exists():
        raise ValueError("An active service has lost its configuration; stop it explicitly before setup")
    environment = dict(os.environ, CODEX_HOME=config["codex_home"])
    run([str(cli), "app-server", "--remote-control", "--listen", "unix://", "--help"],
        capture=True, env=environment)
    run([str(cli), "app-server", "proxy", "--help"], capture=True, env=environment)
    if linger:
        user = pwd.getpwuid(os.getuid()).pw_name
        current = run(["loginctl", "show-user", user, "--property=Linger", "--value"],
                      check=False, capture=True)
        if current.stdout.strip() != "yes":
            result = run(["loginctl", "enable-linger", user], check=False)
            if result.returncode:
                run(["sudo", "loginctl", "enable-linger", user], timeout=None)
            current = run(["loginctl", "show-user", user, "--property=Linger", "--value"], capture=True)
            if current.stdout.strip() != "yes":
                raise ValueError("Lingering was not enabled; use --no-linger for login-only startup")
    atomic_write(path, json.dumps(config, indent=2) + "\n")
    atomic_write(unit, unit_text(config, path), 0o644)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", UNIT])
    run(["systemctl", "--user", "is-active", "--quiet", UNIT])
    print("Configured " + str(unit))
    print("Server " + ("was already active; NOT restarted." if active else "started; check its journal for readiness."))
    print("Pairing, authentication and Remote Control availability still need a live check.")


def emit_environment(config):
    app_dir = os.environ.get("CODEX_LINUX_APP_DIR")
    if app_dir and absolute(app_dir) != Path(config["app_dir"]):
        raise ValueError("This Desktop is not the installation configured for the persistent server")
    values = {
        "CODEX_HOME": config["codex_home"],
        "CODEX_REMOTE_CONTROL_APP_SERVER_MODE": "proxy",
        "CODEX_REMOTE_CONTROL_APP_SERVER_PROXY_SOCKET": str(socket_path(config)),
        "CODEX_REMOTE_CONTROL_DAEMON_AUTOSTART_DISABLED": "1",
    }
    for key, value in values.items():
        print("env " + key + "=" + value)


def serve(config):
    environment = dict(os.environ, CODEX_HOME=config["codex_home"],
                       PATH=str(Path(config["app_dir"]) / "resources") + ":" + config["path"])
    binary = str(cli_path(config))
    os.chdir(config["home"])
    # exec, not a detached 'remote-control start': systemd tracks the real server.
    os.execve(binary, [binary, "-c", "features.code_mode_host=true", "app-server",
                      "--remote-control", "--listen", "unix://"], environment)


def remove(path):
    config = read_config(path)
    unit = Path(config["unit_path"])
    owned_file(unit)
    if not unit.read_text().startswith(MARKER):
        raise ValueError("Refusing to remove an unrecognized service")
    run(["systemctl", "--user", "disable", "--now", UNIT])
    unit.unlink()
    path.unlink()
    run(["systemctl", "--user", "daemon-reload"])
    print("Removed our service/config only. Codex data and user lingering were preserved.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "setup", "env", "serve", "remove"))
    parser.add_argument("--app-dir", default="/opt/codex-desktop")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--no-linger", action="store_true", help="Start at login rather than enabling boot/logout persistence")
    args = parser.parse_args(argv)
    if os.getuid() == 0:
        raise ValueError("Run as your ordinary desktop user, not root or sudo")
    if args.config is not None and args.action in ("install", "setup"):
        raise ValueError("Use XDG_CONFIG_HOME to select the install/setup configuration location")
    path = args.config or config_path()
    if args.action == "install":
        repo = Path(__file__).resolve().parents[2]
        if not (repo / "Makefile").is_file() or not (repo / "linux-features/remote-mobile-control/feature.json").is_file():
            raise ValueError("The install command must run from the source checkout")
        if absolute(args.app_dir) != Path("/opt/codex-desktop"):
            raise ValueError("Native install uses /opt/codex-desktop; use setup for another existing installation")
        # Fail before configuration/build changes if the actual mobile feature
        # can no longer operate independently in proxy mode.
        run(["node", str(Path(__file__).with_name("check-mobile.cjs")), str(repo)], timeout=60)
        enable_selection(repo)
        run(["make", "install-native"], cwd=repo, timeout=None)
        setup(args.app_dir, linger=not args.no_linger)
    elif args.action == "setup":
        setup(args.app_dir, linger=not args.no_linger)
    elif args.action == "env":
        emit_environment(read_config(path))
    elif args.action == "serve":
        serve(read_config(path))
    else:
        remove(path)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print("persistent-app-server: " + str(error), file=sys.stderr)
        sys.exit(78)
