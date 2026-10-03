"""Service lifecycle and real Unix WebSocket activity checks without host mutations."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import threading
import unittest
from unittest.mock import patch

import server_activity
import service_update as u
import test as support


class Fixture(unittest.TestCase):
    def setUp(self):
        self.fixture = support.Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.config = self.fixture.install()
        self.helper = support.m.updates()


class Updates(Fixture):
    def setUp(self):
        super().setUp()
        self.pidfd = patch.object(u.os, "pidfd_open", side_effect=lambda pid: os.open(os.devnull, os.O_RDONLY))
        self.pidfd.start()
        self.addCleanup(self.pidfd.stop)
        self.signals = patch.object(u.signal, "pidfd_send_signal")
        self.send_signal = self.signals.start()
        self.addCleanup(self.signals.stop)
        self.process = patch.object(u, "process_identity", return_value=self.fixture.identity)
        self.process.start()
        self.addCleanup(self.process.stop)

    def queue(self):
        with self.fixture.feature.joinpath("service_update.py").open("a") as stream:
            stream.write("\n# Installed update\n")
        self.helper.queue(self.config, self.helper.generation(self.config))

    def test_current_generation_does_not_enqueue_or_signal(self):
        self.fixture.system.calls.clear()
        self.assertEqual(self.helper.ensure(self.fixture.app), self.config)
        self.assertFalse(self.helper.state_path("update").exists())
        self.send_signal.assert_not_called()
        self.assertFalse(self.fixture.system.worker_active)

    def test_concurrent_launches_share_one_worker_and_pending_generation(self):
        with self.fixture.feature.joinpath("manage.py").open("a") as stream:
            stream.write("\n# Update\n")
        self.fixture.system.calls.clear()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(self.helper.ensure, self.fixture.app) for _ in range(2)]
            self.assertEqual([result.result(timeout=5) for result in results], [self.config, self.config])
        self.assertEqual(self.fixture.system.calls.count(["systemctl", "--user", "daemon-reload"]), 1)
        self.assertEqual(self.helper.read_state("update", self.config)["generation"], self.helper.generation(self.config))
        self.send_signal.assert_not_called()

    def test_keep_working_prompt_keeps_update_queued_and_is_shown_once(self):
        self.queue()
        with patch.object(u.sys.stdin, "isatty", return_value=True), \
                patch.object(u.sys.stderr, "isatty", return_value=True), \
                patch.object(self.helper, "restart_prompt", return_value=False) as prompt:
            self.helper.ensure(self.fixture.app, prompt="cli")
            self.helper.ensure(self.fixture.app, prompt="cli")
        prompt.assert_called_once_with("cli")
        self.assertFalse(self.helper.read_state("update", self.config)["force"])
        self.send_signal.assert_not_called()

    def test_changed_scripts_and_same_version_binary_replacements_enqueue(self):
        initial = self.helper.generation(self.config)
        with self.fixture.feature.joinpath("manage.py").open("a") as stream:
            stream.write("\n")
        self.helper.ensure(self.fixture.app)
        first = self.helper.read_state("update", self.config)
        self.assertNotEqual(first["generation"], initial)
        replacement = self.fixture.cli.with_suffix(".new")
        replacement.write_bytes(self.fixture.cli.read_bytes())
        replacement.chmod(0o755)
        replacement.replace(self.fixture.cli)
        self.helper.ensure(self.fixture.app)
        second = self.helper.read_state("update", self.config)
        self.assertNotEqual(second["generation"], first["generation"])
        self.assertEqual(second["phase"], "waiting")
        self.send_signal.assert_not_called()

    def test_busy_and_unknown_checks_preserve_durable_update(self):
        self.queue()
        for result in ({"idle": False}, TimeoutError("unresponsive"), ValueError("unsupported")):
            with self.subTest(result=result), patch.object(u, "probe") as probe:
                if isinstance(result, Exception):
                    probe.side_effect = result
                    with self.assertRaises(type(result)):
                        self.helper.step()
                else:
                    probe.return_value = result
                    self.assertTrue(self.helper.step())
                self.assertEqual(self.helper.read_state("update", self.config)["phase"], "waiting")
        self.send_signal.assert_not_called()

    def test_idle_legacy_service_drains_then_migrates_and_confirms_readiness(self):
        old = Path(self.config["unit_path"])
        legacy = old.with_name(support.m.LEGACY_UNIT)
        old.rename(legacy)
        self.config["unit_path"] = str(legacy)
        support.m.atomic_write(support.m.config_path(), json.dumps(self.config))
        self.queue()
        with patch.object(u, "probe", return_value={"idle": True}):
            self.assertTrue(self.helper.step())
            self.assertEqual(self.helper.read_state("update", self.config)["phase"], "draining")
            self.assertTrue(self.helper.step())  # Still waiting: no second signal or stop.
            self.assertEqual(self.send_signal.call_count, 1)
            self.assertEqual(self.send_signal.call_args.args[1], signal.SIGHUP)
            self.assertTrue(legacy.exists())
            self.fixture.system.active = False
            self.assertTrue(self.helper.step())
            self.config = support.m.read_config(support.m.config_path())
            self.assertEqual(Path(self.config["unit_path"]), old)
            self.assertFalse(legacy.exists())
            self.helper.write_state("runtime", self.config, {
                "generation": self.helper.generation(self.config), "process": self.fixture.identity, "unit": support.m.UNIT,
            })
            self.assertFalse(self.helper.step())
        self.assertFalse(self.helper.state_path("update").exists())
        self.assertFalse(self.fixture.system.worker_active)
        self.assertFalse(any("stop" in call or "restart" in call for call in self.fixture.system.calls))

    def test_replaced_pid_during_check_does_not_receive_signal(self):
        self.queue()
        with patch.object(u, "probe", return_value={"idle": True}), \
                patch.object(u.ServiceUpdate, "owner", side_effect=[self.fixture.identity, {"pid": 9876}]):
            self.assertTrue(self.helper.step())
        self.send_signal.assert_not_called()

    def test_failed_payload_preflight_does_not_signal_idle_server(self):
        self.queue()
        self.fixture.mobile_marker.unlink()
        with patch.object(u, "probe", return_value={"idle": True}):
            with self.assertRaisesRegex(ValueError, "mobile launch transform"):
                self.helper.step()
        self.send_signal.assert_not_called()
        self.assertEqual(self.helper.read_state("update", self.config)["phase"], "waiting")

    def test_explicit_restart_is_the_only_path_that_stops_a_busy_service(self):
        self.helper.request_restart(self.config)
        self.assertTrue(self.helper.step())
        self.assertIn(["systemctl", "--user", "stop", support.m.UNIT], self.fixture.system.calls)
        self.send_signal.assert_not_called()

    def test_explicit_restart_can_override_a_pending_graceful_drain(self):
        self.queue()
        with patch.object(u, "probe", return_value={"idle": True}):
            self.helper.step()
        self.helper.request_restart(self.config)
        self.helper.step()
        self.assertIn(["systemctl", "--user", "stop", support.m.UNIT], self.fixture.system.calls)

    def test_foreign_worker_and_unsafe_pending_state_are_preserved(self):
        worker = self.helper.worker_path(self.config)
        worker.write_text("[Service]\nExecStart=/bin/true\n")
        with self.assertRaisesRegex(ValueError, "foreign update worker"):
            self.queue()
        self.assertFalse(self.helper.state_path("update").exists())
        worker.unlink()
        self.queue()
        pending = self.helper.state_path("update")
        pending.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            self.helper.step()
        self.send_signal.assert_not_called()

    def test_removal_stops_worker_and_removes_only_recognized_update_state(self):
        self.queue()
        support.m.remove(support.m.config_path())
        self.assertFalse(self.helper.state_path("update").exists())
        self.assertFalse(self.helper.state_path("runtime").exists())
        self.assertFalse(self.helper.worker_path(self.config).exists())
        calls = self.fixture.system.calls
        worker_stop = calls.index(["systemctl", "--user", "disable", "--now", u.WORKER])
        backend_stop = calls.index(["systemctl", "--user", "disable", "--now", support.m.UNIT])
        self.assertLess(worker_stop, backend_stop)

    def test_worker_unit_passes_systemd_verification(self):
        path = self.helper.worker_path(self.config)
        path.write_text(self.helper.worker_text(self.config))
        result = subprocess.run(["systemd-analyze", "verify", str(path)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class RpcServer:
    def __init__(self, path, respond):
        self.respond = respond
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(path))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(2)

                def read(size):
                    value = b""
                    while len(value) < size:
                        chunk = connection.recv(size - len(value))
                        if not chunk:
                            raise EOFError()
                        value += chunk
                    return value

                try:
                    header = b""
                    while b"\r\n\r\n" not in header:
                        header += read(1)
                    key = header.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
                    accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                    connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                    while not self.stop.is_set():
                        first, second = read(2)
                        if first & 0x0F == 8:
                            break
                        size = second & 0x7F
                        if size == 126:
                            size = struct.unpack("!H", read(2))[0]
                        mask, body = read(4), read(size)
                        request = json.loads(bytes(v ^ mask[i % 4] for i, v in enumerate(body)))
                        if "id" not in request:
                            continue
                        result = {"id": request["id"], **self.respond(request)}
                        data = json.dumps(result).encode()
                        prefix = bytes((0x81, len(data))) if len(data) < 126 else b"\x81\x7e" + struct.pack("!H", len(data))
                        connection.sendall(prefix + data)
                except (OSError, EOFError):
                    pass

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        self.listener.close()


class Activity(Fixture):
    def setUp(self):
        super().setUp()
        self.path = support.m.socket_path(self.config)
        self.path.parent.mkdir(parents=True, mode=0o700)
        self.status, self.terminals = "idle", []
        self.loaded_calls = 0
        self.change_threads, self.unavailable = False, False

        def respond(request):
            method = request["method"]
            if method == "initialize":
                return {"result": {}}
            if method == "thread/loaded/list":
                self.loaded_calls += 1
                return {"result": {"data": ["a", "b"] if self.change_threads and self.loaded_calls > 1 else ["a"], "nextCursor": None}}
            if method == "thread/read":
                return {"result": {"thread": {"status": {"type": self.status, "activeFlags": ["waitingOnUserInput"]}}}}
            if self.unavailable:
                return {"error": {"message": "unsupported"}}
            return {"result": {"data": self.terminals, "nextCursor": None}}

        self.server = RpcServer(self.path, respond)
        self.addCleanup(self.server.close)

    def test_idle_socket_and_owned_private_symlink(self):
        actual = self.path.with_suffix(".actual")
        self.path.rename(actual)
        self.path.symlink_to(actual)
        self.assertTrue(server_activity.probe(self.config)["idle"])

    def test_active_pending_input_and_unknown_statuses_block_updates(self):
        for status in ("active", "systemError", "notLoaded", "futureStatus"):
            with self.subTest(status=status):
                self.status = status
                self.assertFalse(server_activity.probe(self.config)["idle"])

    def test_background_commands_and_thread_changes_block_updates(self):
        self.terminals = [{"processId": "running"}]
        self.assertFalse(server_activity.probe(self.config)["idle"])
        self.terminals, self.change_threads = [], True
        self.loaded_calls = 0
        self.assertFalse(server_activity.probe(self.config)["idle"])

    def test_unavailable_api_and_nonprivate_socket_fail_closed(self):
        self.unavailable = True
        with self.assertRaisesRegex(ValueError, "unavailable"):
            server_activity.probe(self.config)
        self.path.parent.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private directory"):
            server_activity.probe(self.config)


if __name__ == "__main__":
    unittest.main(verbosity=2)
