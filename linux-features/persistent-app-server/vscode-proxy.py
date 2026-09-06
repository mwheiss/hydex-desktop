#!/usr/bin/python3
"""Attach the current Codex VS Code JSONL transport to a Unix WebSocket server."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import threading


EXPECTED_APP_SERVER_ARGS = [
    "-c",
    "features.code_mode_host=true",
    "app-server",
    "--analytics-default-enabled",
]
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HTTP_HEADER_BYTES = 16 * 1024
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


def fail(message):
    print("codex-vscode-proxy: " + message, file=sys.stderr)
    raise SystemExit(78)


def absolute(value, label):
    value = str(value)
    if not value or any(c in value for c in "\x00\r\n") or not Path(value).is_absolute():
        fail(f"invalid {label}: {value!r}")
    return Path(value).resolve()


def read_config():
    config_root = absolute(
        os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")),
        "configuration root",
    )
    path = config_root / "codex-desktop/persistent-app-server.json"
    try:
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.getuid():
            fail(f"unsafe or foreign configuration: {path}")
        if file_stat.st_mode & 0o077:
            fail(f"configuration must have mode 0600: {path}")
        config = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        fail(f"cannot read persistent service configuration: {error}")
    if config.get("version") != 1 or config.get("feature") != "persistent-app-server":
        fail("unrecognized persistent service configuration")
    return config


class UnixWebSocket:
    def __init__(self, connection, buffered=b""):
        self.connection = connection
        self.buffered = bytearray(buffered)
        self.write_lock = threading.Lock()

    def read_exact(self, size):
        value = bytearray()
        if self.buffered:
            take = min(size, len(self.buffered))
            value.extend(self.buffered[:take])
            del self.buffered[:take]
        while len(value) < size:
            chunk = self.connection.recv(size - len(value))
            if not chunk:
                raise EOFError("persistent app-server closed the socket")
            value.extend(chunk)
        return bytes(value)

    def send_frame(self, opcode, payload=b""):
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError("outbound app-server message exceeds 64 MiB")
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self.write_lock:
            self.connection.sendall(header + mask + masked)

    def receive_text(self):
        fragments = []
        message_opcode = None
        message_size = 0
        while True:
            first, second = self.read_exact(2)
            if first & 0x70:
                raise ValueError("unsupported WebSocket extension bits")
            final = bool(first & 0x80)
            opcode = first & 0x0F
            if second & 0x80:
                raise ValueError("server WebSocket frame must not be masked")
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self.read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self.read_exact(8))[0]
            if length > MAX_MESSAGE_BYTES or message_size + length > MAX_MESSAGE_BYTES:
                raise ValueError("inbound app-server message exceeds 64 MiB")
            payload = self.read_exact(length)
            if opcode >= 0x8:
                if not final or length > 125:
                    raise ValueError("invalid WebSocket control frame")
                if opcode == 0x8:
                    try:
                        self.send_frame(0x8, payload)
                    except OSError:
                        pass
                    return None
                if opcode == 0x9:
                    self.send_frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                raise ValueError(f"unsupported WebSocket opcode: {opcode}")
            if opcode in (0x1, 0x2):
                if message_opcode is not None:
                    raise ValueError("received a new WebSocket message before continuation")
                message_opcode = opcode
            elif opcode == 0x0:
                if message_opcode is None:
                    raise ValueError("unexpected WebSocket continuation frame")
            else:
                raise ValueError(f"unsupported WebSocket opcode: {opcode}")
            fragments.append(payload)
            message_size += length
            if final:
                if message_opcode != 0x1:
                    raise ValueError("app-server sent a non-text WebSocket message")
                return b"".join(fragments).decode("utf-8")

    def close(self):
        try:
            self.connection.close()
        except OSError:
            pass


def connect_unix_websocket(socket_path):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(10)
    connection.connect(str(socket_path))
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET /rpc HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    connection.sendall(request)
    response = bytearray()
    while b"\r\n\r\n" not in response:
        chunk = connection.recv(4096)
        if not chunk:
            connection.close()
            raise EOFError("persistent app-server closed during WebSocket handshake")
        response.extend(chunk)
        if len(response) > MAX_HTTP_HEADER_BYTES:
            connection.close()
            raise ValueError("WebSocket handshake header exceeds 16 KiB")
    header_bytes, buffered = bytes(response).split(b"\r\n\r\n", 1)
    lines = header_bytes.decode("ascii").split("\r\n")
    if not lines or not lines[0].startswith("HTTP/1.1 101 "):
        connection.close()
        raise ValueError(f"WebSocket upgrade failed: {lines[0] if lines else 'empty response'}")
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if not separator:
            connection.close()
            raise ValueError("malformed WebSocket handshake response")
        headers[name.strip().lower()] = value.strip()
    expected_accept = base64.b64encode(
        hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
    ).decode("ascii")
    if headers.get("sec-websocket-accept") != expected_accept:
        connection.close()
        raise ValueError("WebSocket handshake returned an invalid accept key")
    connection.settimeout(None)
    return UnixWebSocket(connection, buffered)


def bridge_jsonl_to_websocket(socket_path):
    websocket = connect_unix_websocket(socket_path)
    writer_error = []

    def write_stdin():
        try:
            for line in sys.stdin.buffer:
                payload = line.rstrip(b"\r\n")
                if len(payload) > MAX_MESSAGE_BYTES:
                    raise ValueError("outbound app-server message exceeds 64 MiB")
                if payload:
                    payload.decode("utf-8")
                    websocket.send_frame(0x1, payload)
        except (EOFError, OSError, UnicodeError, ValueError) as error:
            writer_error.append(error)
            try:
                websocket.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        try:
            websocket.send_frame(0x8)
        except OSError:
            pass

    writer = threading.Thread(target=write_stdin, name="codex-vscode-proxy-stdin", daemon=True)
    writer.start()
    try:
        while True:
            message = websocket.receive_text()
            if message is None:
                break
            sys.stdout.buffer.write(message.encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()
    except EOFError:
        pass
    finally:
        websocket.close()
        writer.join(timeout=1)
    if writer_error:
        raise writer_error[0]


def main():
    config = read_config()
    app_dir = absolute(config.get("app_dir", ""), "app directory")
    codex_home = absolute(config.get("codex_home", ""), "Codex home")
    binary = app_dir / "resources/codex"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        fail(f"packaged Codex CLI is unavailable: {binary}")

    args = sys.argv[1:]
    if args == EXPECTED_APP_SERVER_ARGS:
        bridge_jsonl_to_websocket(codex_home / "app-server-control/app-server-control.sock")
        return
    if "app-server" in args:
        fail(
            "refusing an unrecognized app-server launch; update the persistent "
            "proxy contract before reloading VS Code"
        )
    os.execve(binary, [str(binary), *args], dict(os.environ, CODEX_HOME=str(codex_home)))


if __name__ == "__main__":
    try:
        main()
    except (EOFError, OSError, UnicodeError, ValueError) as error:
        fail(str(error))
