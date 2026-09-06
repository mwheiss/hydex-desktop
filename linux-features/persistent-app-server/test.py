"""No real systemd, loginctl, make, account, or network mutations in these tests."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("persistent_server", HERE / "manage.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class System:
    def __init__(self):
        self.calls = []
        self.active = False
        self.fragment = ""
        self.linger = "yes"

    def __call__(self, args, **kwargs):
        self.calls.append([str(v) for v in args])
        stdout, code = "", 0
        if "--property=FragmentPath" in args:
            stdout = self.fragment + "\n"
        elif "is-active" in args:
            code = 0 if self.active else 3
        elif "enable" in args:
            self.active = True
        elif "disable" in args:
            self.active = False
        elif "show-user" in args:
            stdout = self.linger + "\n"
        elif "enable-linger" in args:
            self.linger = "yes"
        if kwargs.get("check", True) and code:
            raise subprocess.CalledProcessError(code, args)
        return subprocess.CompletedProcess(args, code, stdout, "")


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-persist-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.app = self.root / "app with space"
        self.feature = self.app / ".codex-linux/features/persistent-app-server"
        self.feature.mkdir(parents=True)
        shutil.copyfile(HERE / "manage.py", self.feature / "manage.py")
        shutil.copyfile(HERE / "vscode-proxy.py", self.feature / "codex-vscode-proxy")
        (self.feature / "codex-vscode-proxy").chmod(0o755)
        self.cli = self.app / "resources/codex"
        self.cli.parent.mkdir()
        self.cli.write_text("#!/bin/sh\nexit 0\n")
        self.cli.chmod(0o755)
        self.code_mode_host = self.app / "resources/codex-code-mode-host"
        self.code_mode_host.write_text("#!/bin/sh\nexit 0\n")
        self.code_mode_host.chmod(0o755)
        self.mobile_marker = self.app / ".codex-linux/desktop-app-server-remote-control-enabled"
        self.mobile_marker.write_text("version=1\nowner=desktop\n")
        self.environment = patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "CODEX_HOME": str(self.home / ".codex"), "PATH": "/usr/bin:/bin",
            "CODEX_LINUX_APP_DIR": str(self.app),
        })
        self.environment.start()
        self.system = System()
        self.runner = patch.object(m, "run", self.system)
        self.runner.start()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.start = self.output.__enter__
        self.output.start()

    def tearDown(self):
        self.output.__exit__(None, None, None)
        self.runner.stop()
        self.environment.stop()
        self.tmp.cleanup()

    def install(self, linger=True):
        m.setup(str(self.app), linger=linger)
        return m.read_config(m.config_path())

    def test_manifest_uses_existing_transport_and_requires_mobile(self):
        value = json.loads((HERE / "feature.json").read_text())
        self.assertEqual(value["requires"], ["remote-mobile-control"])
        self.assertFalse(value["defaultEnabled"])
        self.assertEqual(set(value["conflicts"]), m.INCOMPATIBLE)
        self.assertNotIn("entrypoints", value)
        self.assertNotIn("afterExit", value["runtimeHooks"])
        self.assertEqual(value["packageHooks"], [{
            "source": "package-pacman.sh", "formats": ["pacman"]}])

    def test_pacman_package_hook_links_the_packaged_cli(self):
        package_root = self.root / "package"
        package_app = package_root / "opt/codex-desktop"
        resources = package_app / "resources"
        resources.mkdir(parents=True)
        for executable in ("codex", "codex-code-mode-host"):
            path = resources / executable
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        (package_root / "usr/bin").mkdir(parents=True)
        subprocess.run(["bash", str(HERE / "package-pacman.sh")], check=True, env={
            **os.environ,
            "PACKAGE_ROOT": str(package_root),
            "PACKAGE_APP_DIR": str(package_app),
            "PACKAGE_NAME": "codex-desktop",
        })
        self.assertEqual(os.readlink(package_root / "usr/bin/codex"),
                         "/opt/codex-desktop/resources/codex")
        self.assertEqual(os.readlink(package_root / "usr/bin/codex-code-mode-host"),
                         "/opt/codex-desktop/resources/codex-code-mode-host")

    def test_client_adapter_attaches_vscode_and_desktop_launches(self):
        config = self.install()
        socket_path = m.socket_path(config)
        socket_path.parent.mkdir(parents=True)
        invocations = [
            ["-c", "features.code_mode_host=true", "app-server",
             "--analytics-default-enabled"],
            ["-c", "features.code_mode_host=true", "app-server", "proxy", "--sock",
             str(socket_path), "-c", 'mcp_servers.codex_app={"enabled"=true}'],
        ]
        for invocation in invocations:
            with self.subTest(invocation=invocation):
                if socket_path.exists():
                    socket_path.unlink()
                received = []
                ready = threading.Event()

                def server():
                    with socket.socket(socket.AF_UNIX) as listener:
                        listener.bind(str(socket_path))
                        listener.listen(1)
                        ready.set()
                        connection, _ = listener.accept()
                        with connection:
                            request = bytearray()
                            while b"\r\n\r\n" not in request:
                                request.extend(connection.recv(4096))
                            headers = {}
                            for line in bytes(request).split(b"\r\n")[1:]:
                                name, separator, value = line.partition(b":")
                                if separator:
                                    headers[name.strip().lower()] = value.strip()
                            key = headers[b"sec-websocket-key"].decode("ascii")
                            accept = base64.b64encode(hashlib.sha1(
                                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                            ).digest()).decode("ascii")
                            connection.sendall((
                                "HTTP/1.1 101 Switching Protocols\r\n"
                                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                                f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii"))
                            first, second = connection.recv(2)
                            self.assertEqual(first, 0x81)
                            self.assertTrue(second & 0x80)
                            length = second & 0x7F
                            if length == 126:
                                length = struct.unpack("!H", connection.recv(2))[0]
                            mask = connection.recv(4)
                            payload = bytearray()
                            while len(payload) < length:
                                payload.extend(connection.recv(length - len(payload)))
                            received.append(bytes(
                                value ^ mask[index % 4]
                                for index, value in enumerate(payload)))
                            response = b'{"id":0,"result":{"ok":true}}'
                            connection.sendall(bytes((0x81, len(response))) + response)
                            connection.sendall(bytes((0x88, 0)))

                thread = threading.Thread(target=server)
                thread.start()
                self.assertTrue(ready.wait(timeout=5))
                result = subprocess.run(
                    [sys.executable, str(HERE / "vscode-proxy.py"), *invocation],
                    check=False,
                    input='{"method":"initialize","id":0,"params":{}}\n',
                    text=True,
                    capture_output=True,
                    env=os.environ,
                )
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    received, [b'{"method":"initialize","id":0,"params":{}}'])
                self.assertEqual(result.stdout.strip(), '{"id":0,"result":{"ok":true}}')

    def test_vscode_proxy_forwards_cli_probes_and_rejects_unknown_server_launches(self):
        self.install()
        log = self.root / "vscode-proxy.log"
        self.cli.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$PROXY_LOG\"\n")
        subprocess.run(
            [sys.executable, str(HERE / "vscode-proxy.py"), "--version"],
            check=True,
            env={**os.environ, "PROXY_LOG": str(log)},
        )
        self.assertEqual(log.read_text().splitlines(), ["--version"])
        rejected = subprocess.run(
            [sys.executable, str(HERE / "vscode-proxy.py"), "app-server", "--stdio"],
            text=True,
            capture_output=True,
            env=os.environ,
        )
        self.assertEqual(rejected.returncode, 78)
        self.assertIn("refusing an unrecognized app-server launch", rejected.stderr)
        wrong_socket = subprocess.run(
            [sys.executable, str(HERE / "vscode-proxy.py"), "app-server", "proxy",
             "--sock", str(self.root / "wrong.sock")],
            text=True,
            capture_output=True,
            env=os.environ,
        )
        self.assertEqual(wrong_socket.returncode, 78)
        self.assertIn("refusing an unrecognized app-server launch", wrong_socket.stderr)

    def test_feature_selection_preserves_other_features_and_settings(self):
        repo = self.root / "repo"
        (repo / "linux-features").mkdir(parents=True)
        path = repo / "linux-features/features.json"
        original = {"enabled": ["hydex-offload", "shared-app-server-socket"],
                    "settings": {"hydex-offload": {"example": 1}}}
        path.write_text(json.dumps(original))
        m.enable_selection(repo)
        first = path.read_text()
        m.enable_selection(repo)
        self.assertEqual(path.read_text(), first)
        value = json.loads(first)
        self.assertEqual(value["enabled"], ["hydex-offload", "remote-mobile-control", m.FEATURE])
        self.assertEqual(value["settings"], original["settings"])
        self.assertEqual(json.loads(path.with_name("features.json.before-persistent-app-server").read_text()), original)

    def test_setup_rejects_unverified_mobile_launch_transform(self):
        self.mobile_marker.unlink()
        with self.assertRaisesRegex(ValueError, "mobile launch transform"):
            self.install()
        self.assertFalse(m.config_path().exists())
        self.assertEqual(self.system.calls, [])

    def test_setup_rejects_partial_or_linked_mobile_marker(self):
        self.mobile_marker.write_text("partial\n")
        with self.assertRaisesRegex(ValueError, "mobile launch transform"):
            self.install()
        self.mobile_marker.unlink()
        target = self.root / "marker"
        target.write_text("version=1\nowner=desktop\n")
        self.mobile_marker.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "mobile launch transform"):
            self.install()
        self.assertFalse(m.config_path().exists())

    def test_setup_enables_real_foreground_unit(self):
        config = self.install()
        unit = Path(config["unit_path"]).read_text()
        self.assertIn("Type=simple", unit)
        self.assertIn('"serve" "--config"', unit)
        self.assertNotIn("remote-control start", unit)
        self.assertNotIn("PartOf=", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn(["systemctl", "--user", "enable", "--now", m.UNIT], self.system.calls)

    def test_setup_is_idempotent_and_never_restarts_running_server(self):
        config = self.install()
        before = Path(config["unit_path"]).read_bytes()
        self.install()
        self.assertEqual(before, Path(config["unit_path"]).read_bytes())
        self.assertFalse(any("restart" in v or "stop" in v for v in self.system.calls))

    def test_reinstall_preserves_codex_home_and_path(self):
        first = self.install()
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / "other"), "PATH": "/other/bin"}):
            second = self.install()
        self.assertEqual(first, second)

    def test_refuses_external_unit_even_with_no_socket(self):
        self.system.fragment = "/usr/lib/systemd/user/foreign.service"
        with self.assertRaisesRegex(ValueError, "Another service"):
            self.install()
        self.assertFalse(m.config_path().exists())

    def test_refuses_existing_unmarked_user_unit(self):
        unit = m.config_path().parent.parent / "systemd/user" / m.UNIT
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\nExecStart=/bin/true\n")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.install()
        self.assertIn("ExecStart=/bin/true", unit.read_text())

    def test_refuses_existing_socket_without_deleting_it(self):
        p = self.home / ".codex/app-server-control/app-server-control.sock"
        p.parent.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(p))
            with self.assertRaisesRegex(ValueError, "Existing socket"):
                self.install()
            self.assertTrue(p.exists())
            self.assertFalse(any("enable" in v for v in self.system.calls))

    def test_refuses_live_detached_updater_without_signalling_it(self):
        p = self.home / ".codex/app-server-daemon/app-server-updater.pid"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"pid": os.getpid()}))
        with patch.object(m.os, "kill") as signal:
            with self.assertRaisesRegex(ValueError, "managed daemon/updater"):
                self.install()
            signal.assert_called_once_with(os.getpid(), 0)
        self.assertTrue(p.exists())

    def test_invalid_daemon_metadata_does_not_get_deleted(self):
        p = self.home / ".codex/app-server-daemon/app-server-updater.pid"
        p.parent.mkdir(parents=True)
        p.write_text("[]")
        with self.assertRaisesRegex(ValueError, "unrecognized daemon"):
            self.install()
        self.assertTrue(p.exists())

    def test_cli_capabilities_probed_without_starting_server(self):
        self.install()
        calls = [v for v in self.system.calls if v[0] == str(self.cli)]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(v[-1] == "--help" for v in calls))
        self.assertIn("proxy", calls[1])

    def test_linger_enabled_for_this_user_only(self):
        self.system.linger = "no"
        self.install()
        user = m.pwd.getpwuid(os.getuid()).pw_name
        self.assertIn(["loginctl", "enable-linger", user], self.system.calls)
        self.assertFalse(any("--global" in v for v in self.system.calls))

    def test_no_linger_option_never_calls_loginctl(self):
        self.install(linger=False)
        self.assertFalse(any(v[0] == "loginctl" for v in self.system.calls))

    def test_config_is_private(self):
        self.install()
        self.assertEqual(m.config_path().stat().st_mode & 0o777, 0o600)
        m.config_path().chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            m.read_config(m.config_path())

    def test_config_symlink_is_rejected(self):
        self.install()
        p = m.config_path()
        other = p.with_suffix(".real")
        p.rename(other)
        p.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            m.read_config(p)

    def test_environment_is_aligned_and_does_not_manage_service(self):
        config = self.install()
        self.system.calls.clear()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            m.emit_environment(config)
        lines = stream.getvalue().splitlines()
        self.assertIn("env CODEX_HOME=" + config["codex_home"], lines)
        self.assertIn("env CODEX_CLI_PATH=" + str(m.client_adapter_path(config)), lines)
        self.assertIn("env CODEX_REMOTE_CONTROL_APP_SERVER_MODE=proxy", lines)
        self.assertIn("env CODEX_REMOTE_CONTROL_DAEMON_AUTOSTART_DISABLED=1", lines)
        self.assertIn("env CODEX_REMOTE_CONTROL_APP_SERVER_PROXY_SOCKET=" + str(m.socket_path(config)), lines)
        self.assertEqual(self.system.calls, [])

    def test_another_installation_cannot_attach_accidentally(self):
        config = self.install()
        with patch.dict(os.environ, {"CODEX_LINUX_APP_DIR": str(self.root / "other")}):
            with self.assertRaisesRegex(ValueError, "not the installation"):
                m.emit_environment(config)

    def test_missing_setup_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "not configured"):
            m.read_config(m.config_path())
        self.assertEqual(self.system.calls, [])

    def test_root_main_is_rejected_before_install(self):
        with patch.object(m.os, "getuid", return_value=0):
            with self.assertRaisesRegex(ValueError, "not root"):
                m.main(["install"])
        self.assertEqual(self.system.calls, [])

    def test_serve_execs_packaged_cli_with_same_home(self):
        config = self.install()
        with patch.object(m.os, "execve") as execute, patch.object(m.os, "chdir"):
            m.serve(config)
        binary, argv, environment = execute.call_args.args
        self.assertEqual(binary, str(self.cli))
        self.assertEqual(argv[-4:], ["app-server", "--remote-control", "--listen", "unix://"])
        self.assertEqual(environment["CODEX_HOME"], config["codex_home"])
        self.assertTrue(environment["PATH"].startswith(str(self.cli.parent) + ":"))

    def test_remove_only_removes_feature_state(self):
        config = self.install()
        history = Path(config["codex_home"]) / "history.jsonl"
        history.parent.mkdir(parents=True)
        history.write_text("keep me")
        self.system.calls.clear()
        m.remove(m.config_path())
        self.assertEqual(history.read_text(), "keep me")
        self.assertFalse(Path(config["unit_path"]).exists())
        self.assertFalse(m.config_path().exists())
        self.assertFalse(any("loginctl" in v for v in self.system.calls))

    def test_generated_unit_passes_systemd_static_verification(self):
        tool = shutil.which("systemd-analyze")
        if not tool:
            self.skipTest("systemd-analyze is unavailable")
        config = self.install()
        unit = Path(config["unit_path"])
        self.assertIn("WorkingDirectory=%h\n", unit.read_text())
        result = subprocess.run([tool, "verify", str(unit)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unit_escaping_and_path_rejection(self):
        self.assertEqual(m.quote_unit('/home/a%u/$HOME/"x"', argument=True),
                         '"/home/a%%u/$$HOME/\\"x\\""')
        self.assertEqual(m.quote_unit("/home/$cash"), '"/home/$cash"')
        with self.assertRaises(ValueError):
            m.absolute("relative")
        with self.assertRaises(ValueError):
            m.absolute("/tmp/one\ntwo")

    def test_real_exec_process_survives_two_local_client_disconnects(self):
        # A real Unix listener, not Codex, WebSocket, Electron, or systemd.
        config = self.install()
        self.cli.write_text('''#!/usr/bin/python3
import json, os, pathlib, socket, sys
root = pathlib.Path(os.environ['CODEX_HOME'])
p = root / 'app-server-control/app-server-control.sock'
p.parent.mkdir(parents=True, exist_ok=True)
s = socket.socket(socket.AF_UNIX)
s.bind(str(p)); s.listen()
(root / 'process.json').write_text(json.dumps({'pid':os.getpid(), 'args':sys.argv}))
while True:
    c, _ = s.accept()
    with c:
        data = c.recv(1024)
        if data: c.sendall(data)
''')
        script = "import importlib.util,json,sys;s=importlib.util.spec_from_file_location('m',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.serve(json.loads(sys.argv[2]))"
        process = subprocess.Popen([
            sys.executable, "-B", "-c", script, str(HERE / "manage.py"), json.dumps(config)])
        try:
            info = Path(config["codex_home"]) / "process.json"
            for _ in range(100):
                if info.exists():
                    break
                if process.poll() is not None:
                    self.fail("Fake foreground listener exited")
                time.sleep(.02)
            self.assertEqual(json.loads(info.read_text())["pid"], process.pid)
            for _ in range(2):
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(m.socket_path(config)))
                    client.sendall(b"still alive")
                    self.assertEqual(client.recv(1024), b"still alive")
                m.emit_environment(config)
                self.assertIsNone(process.poll())
            self.assertTrue(m.socket_path(config).exists())
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_cli_preflight_failure_does_not_install_service(self):
        normal = self.system
        def fail_cli(args, **kwargs):
            if str(args[0]) == str(self.cli):
                raise subprocess.CalledProcessError(2, args)
            return normal(args, **kwargs)
        with patch.object(m, "run", fail_cli):
            with self.assertRaises(subprocess.CalledProcessError):
                self.install()
        self.assertFalse(m.config_path().exists())
        self.assertFalse(any("enable" in v for v in self.system.calls))

    def test_install_wrapper_builds_before_user_setup(self):
        repo = self.root / "checkout"
        (repo / "linux-features/remote-mobile-control").mkdir(parents=True)
        (repo / "linux-features/persistent-app-server").mkdir()
        (repo / "Makefile").write_text("# fixture; never executed\n")
        (repo / "linux-features/remote-mobile-control/feature.json").write_text("{}")
        (repo / "linux-features/remote-mobile-control/patch.js").write_text(
            "CODEX_REMOTE_CONTROL_APP_SERVER_MODE CODEX_REMOTE_CONTROL_APP_SERVER_PROXY_SOCKET")
        fake_file = repo / "linux-features/persistent-app-server/manage.py"
        with patch.object(m, "__file__", str(fake_file)), patch.object(m.os, "getuid", return_value=12345), patch.object(m, "setup") as setup:
            m.main(["install", "--no-linger"])
            setup.assert_called_once_with("/opt/codex-desktop", linger=False)
        self.assertIn(["make", "install-native"], self.system.calls)
        self.assertEqual(self.system.calls[0], ["node", str(fake_file.with_name("check-mobile.cjs")), str(repo)])
        selected = json.loads((repo / "linux-features/features.json").read_text())["enabled"]
        self.assertEqual(selected, ["remote-mobile-control", "persistent-app-server"])

    def test_existing_remote_mobile_source_contract(self):
        source = HERE.parent / "remote-mobile-control/patch.js"
        if not source.is_file() or not shutil.which("node"):
            self.skipTest("Run in a full checkout with Node to audit the actual mobile source")
        subprocess.run([shutil.which("node"), str(HERE / "check-mobile.cjs"),
                        str(HERE.parents[1])], check=True)

    def test_failed_mobile_audit_prevents_build_and_configuration_changes(self):
        repo = self.root / "checkout"
        (repo / "linux-features/remote-mobile-control").mkdir(parents=True)
        (repo / "linux-features/remote-mobile-control/feature.json").write_text("{}")
        (repo / "Makefile").write_text("# fixture; never executed\n")
        fake_file = repo / "linux-features/persistent-app-server/manage.py"
        with patch.object(m, "__file__", str(fake_file)), patch.object(m.os, "getuid", return_value=12345), patch.object(m, "run", side_effect=subprocess.CalledProcessError(1, ["node"])) as run, patch.object(m, "setup") as setup:
            with self.assertRaises(subprocess.CalledProcessError):
                m.main(["install"])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], "node")
            setup.assert_not_called()
        self.assertFalse((repo / "linux-features/features.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
