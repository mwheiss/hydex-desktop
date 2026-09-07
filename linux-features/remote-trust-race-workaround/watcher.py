#!/usr/bin/python3
"""Narrow inotify workaround for Codex Remote exact-path trust races on Linux."""

from collections import namedtuple
import ctypes
import ctypes.util
from datetime import datetime
import errno
import json
import os
from pathlib import Path
import re
import select
import stat
import struct
import sys
import time

try:
    import tomllib
except ImportError:
    import tomli as tomllib

FEATURE = "remote-trust-race-workaround"
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_ONLYDIR = 0x01000000
IN_ISDIR = 0x40000000
WATCH_MASK = IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE | IN_DELETE_SELF | IN_MOVE_SELF
EVENT = struct.Struct("iIII")
DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}(?:-|$)")


Paths = namedtuple(
    "Paths",
    ("home", "codex_home", "config", "state", "projectless_root", "worktrees_root"),
)


def env_path(name, default):
    raw = os.environ.get(name)
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute() or any(c in str(path) for c in "\x00\r\n"):
        raise ValueError(f"{name} must be an absolute single-line path")
    return path


def configured_paths():
    home = Path.home().resolve()
    codex_home = env_path("CODEX_HOME", home / ".codex").resolve()
    projectless = env_path("HYDEX_REMOTE_PROJECTLESS_ROOT", home / "Documents/Codex").resolve()
    worktrees = env_path("HYDEX_REMOTE_WORKTREES_ROOT", codex_home / "worktrees").resolve()
    state_home = env_path("XDG_STATE_HOME", home / ".local/state").resolve()
    state = state_home / "hydex-desktop" / (FEATURE + ".json")
    return Paths(home, codex_home, codex_home / "config.toml", state, projectless, worktrees)


def safe_owned_directory(path):
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(st.st_mode) and not path.is_symlink() and st.st_uid == os.getuid()


def read_config(path):
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            return {}
        with path.open("rb") as stream:
            value = tomllib.load(stream)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, PermissionError, tomllib.TOMLDecodeError, OSError):
        return {}


def project_map(config):
    value = config.get("projects", {})
    return value if isinstance(value, dict) else {}


def explicitly_trusted(config, path):
    value = project_map(config).get(str(path))
    return isinstance(value, dict) and value.get("trust_level") == "trusted"


def explicitly_untrusted(config, path):
    value = project_map(config).get(str(path))
    return isinstance(value, dict) and value.get("trust_level") == "untrusted"


def trusted_git_checkout_names(config):
    names = set()
    for raw_path, value in project_map(config).items():
        if not isinstance(raw_path, str) or not isinstance(value, dict) or value.get("trust_level") != "trusted":
            continue
        path = Path(raw_path)
        if not path.is_absolute() or not safe_owned_directory(path):
            continue
        # Worktrees have a .git file; ordinary checkouts normally have a .git directory.
        if (path / ".git").exists():
            names.add(path.name)
    return names


def same_dayish_projectless_name(name):
    if not DATE_PREFIX.match(name):
        return False
    try:
        prefix = name[:10]
        parsed = datetime.strptime(prefix, "%Y-%m-%d").date()
    except ValueError:
        return False
    # The date prefix is a shape check, not a clock policy; Remote may reconnect
    # across midnight. Direct-child placement, ownership, and explicit root trust
    # are the actual boundary.
    return parsed.isoformat() == prefix


def projectless_candidate(paths, candidate, config):
    try:
        if candidate.parent != paths.projectless_root:
            return False
    except OSError:
        return False
    return (
        explicitly_trusted(config, paths.projectless_root)
        and same_dayish_projectless_name(candidate.name)
        and safe_owned_directory(candidate)
        and not explicitly_untrusted(config, candidate)
    )


def worktree_candidate(paths, candidate, config):
    try:
        relative = candidate.relative_to(paths.worktrees_root)
    except ValueError:
        return False
    if len(relative.parts) != 2 or not safe_owned_directory(candidate):
        return False
    generated_parent = candidate.parent
    if not safe_owned_directory(generated_parent):
        return False
    if explicitly_untrusted(config, candidate):
        return False
    return candidate.name in trusted_git_checkout_names(config)


def toml_table(path):
    # JSON string escaping is a valid TOML basic-string escaping subset.
    key = json.dumps(str(path), ensure_ascii=True)
    return f'\n[projects.{key}]\ntrust_level = "trusted"\n'.encode("utf-8")


def add_exact_trust(config_path, candidate):
    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(config_path, flags)
    except (FileNotFoundError, PermissionError):
        return False
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022:
            return False
        try:
            with os.fdopen(os.dup(fd), "rb") as stream:
                config = tomllib.load(stream)
        except tomllib.TOMLDecodeError:
            return False

        # Bind validation and append to the same inode. If Codex atomically
        # replaced config.toml, writing this old descriptor is harmless and a
        # later reconciliation retries against the replacement.
        try:
            current_path = config_path.lstat()
        except FileNotFoundError:
            return False
        after = os.fstat(fd)
        if (
            not stat.S_ISREG(current_path.st_mode)
            or (current_path.st_dev, current_path.st_ino) != (after.st_dev, after.st_ino)
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        ):
            return False

        current = project_map(config).get(str(candidate))
        if isinstance(current, dict):
            # Never override an explicit user decision, including untrusted.
            return current.get("trust_level") == "trusted"
        os.write(fd, toml_table(candidate))
        os.fsync(fd)
    finally:
        os.close(fd)
    return explicitly_trusted(read_config(config_path), candidate)


def load_state(path):
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            return set()
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("version") != 1 or value.get("feature") != FEATURE:
            return set()
        approved = value.get("approved", [])
        if not isinstance(approved, list):
            return set()
        return {Path(item) for item in approved if isinstance(item, str) and Path(item).is_absolute()}
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
        return set()


def save_state(path, approved):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps({
        "version": 1,
        "feature": FEATURE,
        "approved": sorted(str(item) for item in approved),
    }, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def validate_candidate(paths, candidate, config):
    return projectless_candidate(paths, candidate, config) or worktree_candidate(paths, candidate, config)


def remember_and_trust(paths, candidate, config=None):
    config = config or read_config(paths.config)
    if explicitly_trusted(config, candidate):
        return True
    if not validate_candidate(paths, candidate, config):
        return False
    approved = load_state(paths.state)
    approved.add(candidate)
    save_state(paths.state, approved)
    return add_exact_trust(paths.config, candidate)


class Inotify:
    def __init__(self) -> None:
        libc_name = ctypes.util.find_library("c")
        if not libc_name:
            raise OSError("cannot locate libc for inotify")
        self.libc = ctypes.CDLL(libc_name, use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_init1.restype = ctypes.c_int
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int
        flags = os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        self.fd = self.libc.inotify_init1(flags)
        if self.fd < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        self.watches = {}
        self.reverse = {}

    def add(self, path):
        path = path.resolve()
        if path in self.reverse or not safe_owned_directory(path):
            return
        wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), WATCH_MASK | IN_ONLYDIR)
        if wd < 0:
            code = ctypes.get_errno()
            if code not in (errno.ENOENT, errno.ENOTDIR):
                raise OSError(code, os.strerror(code), str(path))
            return
        self.watches[wd] = path
        self.reverse[path] = wd

    def events(self, timeout=1.0):
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return
        data = os.read(self.fd, 65536)
        offset = 0
        while offset + EVENT.size <= len(data):
            wd, mask, _cookie, length = EVENT.unpack_from(data, offset)
            offset += EVENT.size
            raw_name = data[offset:offset + length].split(b"\0", 1)[0]
            offset += length
            base = self.watches.get(wd)
            if base is None:
                continue
            name = os.fsdecode(raw_name) if raw_name else ""
            yield base, name, mask


def reconcile(paths, ino):
    config = read_config(paths.config)
    approved = load_state(paths.state)
    # Watch config parent so atomic config.toml replacement is observed.
    ino.add(paths.codex_home)
    ino.add(paths.projectless_root.parent)
    ino.add(paths.worktrees_root.parent)
    if safe_owned_directory(paths.projectless_root):
        ino.add(paths.projectless_root)
    if safe_owned_directory(paths.worktrees_root):
        ino.add(paths.worktrees_root)
        for generated in paths.worktrees_root.iterdir():
            if safe_owned_directory(generated):
                ino.add(generated)
    # Reconcile only paths that this service previously approved. Startup must
    # not retroactively trust arbitrary pre-existing date-shaped directories.
    retained = set()
    for candidate in approved:
        if not safe_owned_directory(candidate):
            continue
        if validate_candidate(paths, candidate, config):
            retained.add(candidate)
            add_exact_trust(paths.config, candidate)
    if retained != approved:
        save_state(paths.state, retained)


def handle_candidate(paths, candidate):
    config = read_config(paths.config)
    if explicitly_trusted(config, candidate):
        return
    if validate_candidate(paths, candidate, config):
        started = time.monotonic()
        if remember_and_trust(paths, candidate, config):
            elapsed_ms = (time.monotonic() - started) * 1000
            print(f"{FEATURE}: trusted generated path in {elapsed_ms:.3f} ms: {candidate}", file=sys.stderr, flush=True)


def run():
    if os.getuid() == 0:
        raise ValueError("refusing to run as root")
    paths = configured_paths()
    ino = Inotify()
    reconcile(paths, ino)
    last_reconcile = time.monotonic()
    while True:
        for base, name, mask in ino.events(0.25) or ():
            if base == paths.codex_home and name == paths.config.name:
                # Codex commonly replaces config.toml atomically. Reconcile immediately
                # so previously approved generated entries are restored if needed.
                reconcile(paths, ino)
                continue
            if mask & IN_ISDIR and name:
                candidate = base / name
                if candidate == paths.projectless_root or candidate == paths.worktrees_root:
                    ino.add(candidate)
                    continue
                if base == paths.worktrees_root:
                    ino.add(candidate)
                    # The repository child can appear before this watch is installed.
                    # Scan once immediately rather than waiting for periodic repair.
                    try:
                        for child in candidate.iterdir():
                            handle_candidate(paths, child)
                    except (FileNotFoundError, NotADirectoryError, PermissionError):
                        pass
                else:
                    handle_candidate(paths, candidate)
        now = time.monotonic()
        if now - last_reconcile >= 2.0:
            # Low-frequency repair covers roots created after service startup and
            # inotify self-move/delete events without widening the trust policy.
            reconcile(paths, ino)
            last_reconcile = now


if __name__ == "__main__":
    try:
        run()
    except (ValueError, OSError) as error:
        print(FEATURE + ": " + str(error), file=sys.stderr)
        sys.exit(78)
