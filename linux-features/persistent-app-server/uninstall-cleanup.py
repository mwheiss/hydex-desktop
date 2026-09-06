#!/usr/bin/python3
"""Remove only recognized persistent-service state and its exact VS Code override."""
import argparse
import json
import os
from pathlib import Path
import pwd
import stat
import tempfile


FEATURE = "persistent-app-server"
UNIT = "codex-remote-control.service"
UNIT_MARKER = "# Managed by codex-desktop-linux persistent-app-server v1\n"
SETTINGS_KEYS = ("hydex.cliExecutable", "chatgpt.cliExecutable")
SETTINGS_ROOTS = ("Code", "Code - OSS", "VSCodium")


def warn(message):
    print("persistent-app-server cleanup: " + message)


def owned_regular(path, uid, *, private=False):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(value.st_mode) or value.st_uid != uid or value.st_mode & 0o022:
        warn("preserving unsafe or foreign file: " + str(path))
        return False
    if private and value.st_mode & 0o077:
        warn("preserving non-private file: " + str(path))
        return False
    return True


def atomic_json_write(path, value, uid, gid, mode):
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(mode))
            os.fchown(stream.fileno(), uid, gid)
            json.dump(value, stream, indent=4, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def remove_service_state(home, uid, app_dir):
    config = home / ".config/codex-desktop/persistent-app-server.json"
    if not owned_regular(config, uid, private=True):
        return
    try:
        value = json.loads(config.read_text())
    except (OSError, json.JSONDecodeError) as error:
        warn(f"preserving unrecognized configuration {config}: {error}")
        return
    unit = home / ".config/systemd/user" / UNIT
    expected = {
        "version": 1,
        "feature": FEATURE,
        "app_dir": str(app_dir),
        "unit_path": str(unit),
    }
    if not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items()):
        warn("preserving configuration for another installation: " + str(config))
        return
    if unit.exists() or unit.is_symlink():
        if not owned_regular(unit, uid) or not unit.read_text().startswith(UNIT_MARKER):
            warn("preserving unrecognized user service: " + str(unit))
            return
    wants = unit.parent / "default.target.wants" / UNIT
    if wants.is_symlink() and wants.lstat().st_uid == uid:
        target = Path(os.path.abspath(wants.parent / os.readlink(wants)))
        if target == unit:
            wants.unlink()
            warn("removed " + str(wants))
    if unit.exists():
        unit.unlink()
        warn("removed " + str(unit))
    config.unlink()
    warn("removed " + str(config))


def settings_paths(home):
    for root_name in SETTINGS_ROOTS:
        user_root = home / ".config" / root_name / "User"
        yield user_root / "settings.json"
        profiles = user_root / "profiles"
        if profiles.is_dir() and not profiles.is_symlink():
            yield from profiles.glob("*/settings.json")


def remove_editor_override(home, uid, adapter):
    for path in settings_paths(home):
        if not owned_regular(path, uid):
            continue
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            warn(f"preserving non-JSON editor settings {path}: {error}")
            continue
        if not isinstance(value, dict):
            warn("preserving non-object editor settings: " + str(path))
            continue
        changed = False
        for key in SETTINGS_KEYS:
            if value.get(key) == str(adapter):
                del value[key]
                changed = True
        if changed:
            file_stat = path.stat()
            atomic_json_write(path, value, uid, file_stat.st_gid, file_stat.st_mode)
            warn("removed packaged CLI override from " + str(path))


def normal_users():
    uid_min = 1000
    try:
        for line in Path("/etc/login.defs").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "UID_MIN":
                uid_min = int(parts[1])
                break
    except (OSError, ValueError):
        pass
    seen = set()
    for account in pwd.getpwall():
        home = Path(account.pw_dir)
        if account.pw_uid < uid_min or home == Path("/") or home in seen or not home.is_dir():
            continue
        seen.add(home)
        yield account, home


def cleanup(app_dir):
    adapter = app_dir / ".codex-linux/features/persistent-app-server/codex-vscode-proxy"
    for account, home in normal_users():
        remove_service_state(home, account.pw_uid, app_dir)
        remove_editor_override(home, account.pw_uid, adapter)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.getuid() != 0:
        raise SystemExit("persistent-app-server cleanup must run from a package removal hook")
    app_dir = Path(os.path.abspath(args.app_dir))
    if app_dir.parent != Path("/opt") or app_dir.name not in ("codex-desktop", "chatgpt-community"):
        raise SystemExit("refusing unexpected package app directory: " + str(app_dir))
    cleanup(app_dir)


if __name__ == "__main__":
    main()
