"""Durable user-service updates; automatic activation never forces a shutdown."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import time

from server_activity import probe

WORKER = "hydex-app-server-update.service"
WORKER_MARKER = "# Managed by hydex-desktop persistent-app-server update v1\n"
STATE_NAMES = ("runtime", "update")


def process_identity(pid):
    path = Path("/proc") / str(pid)
    if pid <= 0 or path.stat().st_uid != os.getuid():
        raise ValueError("Foreign app-server process")
    fields = (path / "stat").read_text().rsplit(") ", 1)[1].split()
    return {"pid": pid, "start": fields[19], "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


class ServiceUpdate:
    def __init__(self, manager, path):
        self.m, self.path = manager, path
        self.lock_path = path.with_suffix(".lock")

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise ValueError("Unsafe service configuration directory")
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            self.m.owned_file(self.lock_path, private=True)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def state_path(self, name):
        return self.path.with_name(self.path.stem + "." + name + ".json")

    def read_state(self, name, config):
        path = self.state_path(name)
        if not os.path.lexists(path):
            return None
        self.m.owned_file(path, private=True)
        value = json.loads(path.read_text())
        if (not isinstance(value, dict) or value.get("version") != 1 or value.get("feature") != self.m.FEATURE
                or value.get("app_dir") != config["app_dir"]):
            raise ValueError("Unrecognized service " + name + " state")
        if name == "update" and (value.get("phase") not in ("waiting", "draining", "starting")
                                  or not isinstance(value.get("force"), bool)):
            raise ValueError("Unrecognized pending update phase or restart request")
        return value

    def write_state(self, name, config, value):
        self.m.atomic_write(self.state_path(name), json.dumps({
            "version": 1, "feature": self.m.FEATURE, "app_dir": config["app_dir"], **value,
        }, indent=2) + "\n")

    def generation(self, config):
        desired = {**config, "unit_path": str(Path(config["unit_path"]).with_name(self.m.UNIT))}
        digest = hashlib.sha256(json.dumps(desired, sort_keys=True).encode())
        digest.update(self.m.unit_text(desired, self.path).encode())
        root = self.m.helper_path(config).parent
        for name in ("manage.py", "service_update.py", "server_activity.py", "codex-vscode-proxy", "codex-cli-wrapper"):
            digest.update(name.encode())
            digest.update((root / name).read_bytes())
        for path in (root.parents[1] / "build-info.json", root.parent / "hydex-offload/build-info"):
            if path.is_file():
                digest.update(path.read_bytes())
        # Native package replacement changes inode/mtime even at the same CLI version.
        # Never rehash the large CLI at every launch or worker poll.
        for name in ("codex", "codex-code-mode-host"):
            info = (Path(config["app_dir"]) / "resources" / name).stat()
            digest.update(str((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)).encode())
        return digest.hexdigest()

    def record_runtime(self, config):
        self.write_state("runtime", config, {
            "generation": self.generation(config), "process": process_identity(os.getpid()),
            "unit": Path(config["unit_path"]).name,
        })

    def owner(self, config):
        unit = Path(config["unit_path"])
        active = self.m.check_foreign_owner(config, unit)
        if not active:
            return None
        dropins = self.m.run(["systemctl", "--user", "show", unit.name,
                             "--property=DropInPaths", "--value"], capture=True).stdout.strip()
        if dropins:
            raise ValueError("Inspect service drop-ins before enabling automatic updates")
        pid = int(self.m.run(["systemctl", "--user", "show", unit.name,
                              "--property=MainPID", "--value"], capture=True).stdout.strip())
        identity = process_identity(pid)
        executable = os.readlink("/proc/" + str(pid) + "/exe")
        if executable.endswith(" (deleted)"):
            executable = executable[:-10]
        if executable != str(self.m.cli_path(config).resolve()):
            raise ValueError("Service MainPID is not the configured app-server executable")
        arguments = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        if b"app-server" not in arguments or b"unix://" not in arguments or b"--remote-control" not in arguments:
            raise ValueError("Service MainPID is not the configured Unix app-server")
        return identity

    def current(self, config, owner, generation):
        runtime = self.read_state("runtime", config)
        unit = Path(config["unit_path"])
        return (owner is not None and unit.name == self.m.UNIT
                and unit.read_text() == self.m.unit_text(config, self.path)
                and runtime is not None and runtime.get("generation") == generation
                and runtime.get("process") == owner and runtime.get("unit") == self.m.UNIT)

    def worker_path(self, config):
        return Path(config["unit_path"]).with_name(WORKER)

    def worker_text(self, config):
        command = " ".join(self.m.quote_unit(v, argument=True) for v in (
            "/usr/bin/python3", self.m.helper_path(config), "update-worker", "--config", self.path))
        return WORKER_MARKER + f"""[Unit]
Description=Apply pending Hydex app-server updates when idle
After=hydex-remote-control.service codex-remote-control.service

[Service]
Type=simple
ExecStart={command}
Restart=on-failure
RestartPreventExitStatus=78
RestartSec=30
UMask=0077
StandardInput=null
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""

    def check_worker(self, config):
        path = self.worker_path(config)
        if os.path.lexists(path):
            self.m.owned_file(path)
            if not path.read_text().startswith(WORKER_MARKER):
                raise ValueError("Refusing to overwrite a foreign update worker")
        fragment = self.m.run(["systemctl", "--user", "show", WORKER,
                              "--property=FragmentPath", "--value"], check=False, capture=True).stdout.strip()
        if fragment and Path(fragment).resolve() != path.resolve():
            raise ValueError("Another service owns " + WORKER)
        if not path.exists() and self.m.run(["systemctl", "--user", "is-active", "--quiet", WORKER],
                                           check=False, capture=True).returncode == 0:
            raise ValueError("An active update worker has no recognized unit file")

    def queue(self, config, generation, *, force=False):
        self.check_worker(config)
        pending = self.read_state("update", config) or {"phase": "waiting"}
        if pending.get("generation") != generation:
            pending.pop("prompted", None)
        pending.update(generation=generation, force=pending.get("force", False) or force)
        self.write_state("update", config, pending)
        worker = self.worker_path(config)
        text = self.worker_text(config)
        if not worker.exists() or worker.read_text() != text:
            self.m.atomic_write(worker, text, 0o644)
            self.m.run(["systemctl", "--user", "daemon-reload"])
        self.m.run(["systemctl", "--user", "enable", "--now", WORKER])

    def ensure(self, app_dir, *, prompt=None):
        notice, idle, offer = False, False, False
        with self.locked():
            if not os.path.lexists(self.path):
                self.m.setup(app_dir, linger=False, path=self.path)
                return self.m.read_config(self.path)
            config = self.m.read_config(self.path)
            if config["app_dir"] != str(self.m.absolute(app_dir)):
                raise ValueError("Configured for another app directory; remove the old service explicitly first")
            active = self.m.check_foreign_owner(config, Path(config["unit_path"]))
            try:
                owner = self.owner(config) if active else None
            except (OSError, ValueError):
                owner = None  # Unknown process identity blocks activation, not attachment.
            generation = self.generation(config)
            if self.current(config, owner, generation):
                return config
            if not active:
                return self.activate(config)
            pending = self.read_state("update", config)
            notice = pending is None or pending.get("generation") != generation
            offer = (notice or not pending.get("prompted")) and (
                (prompt == "cli" and sys.stdin.isatty() and sys.stderr.isatty())
                or (prompt == "desktop" and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
                    and shutil.which("zenity")))
            if notice or offer:
                try:
                    idle = probe(config)["idle"]
                except (OSError, ValueError, KeyError, TypeError, EOFError):
                    pass
            self.queue(config, generation)
            if offer and not idle:
                pending = self.read_state("update", config)
                pending["prompted"] = True
                self.write_state("update", config, pending)
        if (notice or offer) and idle:
            deadline = time.monotonic() + 15
            while self.state_path("update").exists() and time.monotonic() < deadline:
                time.sleep(0.2)
            return self.m.read_config(self.path)
        if notice or offer:
            print("Hydex app-server update queued; it will restart automatically when shared work is idle.", file=sys.stderr)
            if offer and self.restart_prompt(prompt) and self.state_path("update").exists():
                self.request_restart(config)
        return config

    def restart_prompt(self, kind):
        message = ("A Hydex app-server update is queued and will apply automatically when idle. "
                   "Restart now interrupts shared tasks and pending approval or input requests.")
        if kind == "cli" and sys.stdin.isatty() and sys.stderr.isatty():
            print(message + "\nRestart now? [y/N] ", end="", file=sys.stderr, flush=True)
            return sys.stdin.readline().strip().lower() in ("y", "yes")
        if kind == "desktop" and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            if shutil.which("zenity"):
                result = self.m.run(["zenity", "--question", "--no-markup", "--default-cancel",
                                     "--title=Hydex service update", "--ok-label=Restart now",
                                     "--cancel-label=Keep working", "--text=" + message], check=False, timeout=None)
                return result.returncode == 0
        return False

    def request_restart(self, config):
        with self.locked():
            self.queue(config, self.generation(config), force=True)

    def activate(self, config):
        old = Path(config["unit_path"])
        if old.name == self.m.LEGACY_UNIT:
            new = old.with_name(self.m.UNIT)
            self.m.check_foreign_owner(config, new)
            self.m.run(["systemctl", "--user", "disable", self.m.LEGACY_UNIT], check=False)
            if old.exists():
                old.unlink()
            config = {**config, "unit_path": str(new)}
            self.m.atomic_write(self.path, json.dumps(config, indent=2) + "\n")
        self.m.setup(config["app_dir"], linger=False, path=self.path)
        return self.m.read_config(self.path)

    def step(self):
        with self.locked():
            config = self.m.read_config(self.path)
            pending = self.read_state("update", config)
            if pending is None:
                return False
            owner = self.owner(config)
            generation = self.generation(config)
            if self.current(config, owner, generation) and not pending.get("force"):
                probe(config)  # Confirm the replacement socket accepts initialized clients.
                self.state_path("update").unlink()
                self.m.run(["systemctl", "--user", "disable", WORKER])
                print("Hydex app-server update applied: " + generation, flush=True)
                return False
            if owner is None:
                self.activate(config)
                pending.update(phase="starting", force=False, generation=generation)
            elif pending.get("force"):
                self.m.validate_install(config)
                self.m.run(["systemctl", "--user", "stop", Path(config["unit_path"]).name], timeout=None)
                pending.update(phase="waiting", force=False)
            elif pending.get("phase") == "draining" and pending.get("process") == owner:
                return True  # No timeout, repeated signal, or forced fallback during drain.
            else:
                activity = probe(config)
                if not activity["idle"]:
                    return True
                self.m.validate_install(config)
                if (self.generation(config) != generation or not probe(config)["idle"]
                        or self.owner(config) != owner):
                    return True
                pidfd = None
                try:
                    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                        pidfd = os.pidfd_open(owner["pid"])
                        if process_identity(owner["pid"]) != owner:
                            return True
                        signal.pidfd_send_signal(pidfd, signal.SIGHUP)
                    else:
                        self.m.run(["systemctl", "--user", "kill", "--kill-who=main", "--signal=HUP",
                                    Path(config["unit_path"]).name])
                finally:
                    if pidfd is not None:
                        os.close(pidfd)
                pending.update(phase="draining", process=owner, generation=generation)
                print("Hydex app-server is draining for an idle update", flush=True)
            self.write_state("update", config, pending)
            return True

    def worker(self):
        previous_error = None
        while os.path.lexists(self.path):
            try:
                if not self.step():
                    return
                previous_error = None
            except (OSError, ValueError, KeyError, TypeError, EOFError, subprocess.SubprocessError) as error:
                message = str(error)
                if message != previous_error:
                    print("Hydex update remains queued: " + message, file=sys.stderr, flush=True)
                previous_error = message
            time.sleep(5)

    def cleanup(self, config):
        self.check_worker(config)
        self.m.run(["systemctl", "--user", "disable", "--now", WORKER], check=False)
        with self.locked():
            for name in STATE_NAMES:
                if self.read_state(name, config) is not None:
                    self.state_path(name).unlink()
            worker = self.worker_path(config)
            if worker.exists():
                worker.unlink()
            unit = Path(config["unit_path"])
            self.m.run(["systemctl", "--user", "disable", "--now", unit.name])
            unit.unlink()
            self.path.unlink()
