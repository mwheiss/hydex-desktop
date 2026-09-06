#!/usr/bin/python3
"""Attach local Codex JSONL clients to the persistent Unix WebSocket server."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
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
DESKTOP_MCP_CONFIG_KEY = "mcp_servers.codex_app"
THREAD_CONFIG_METHODS = frozenset(("thread/start", "thread/resume", "thread/fork"))
DESKTOP_MCP_ENV_VARS = frozenset((
    "CODEX_APP_TOOLS_PIPE_PATH",
    "CODEX_MCP_NODE_PATH",
    "CODEX_BROWSER_USE_NODE_PATH",
    "CODEX_ELECTRON_RESOURCES_PATH",
    "CODEX_CLI_PATH",
    "XDG_CACHE_HOME",
    "HOME",
    "USERPROFILE",
    "LOCALAPPDATA",
    "PATH",
))


def fail(message):
    print("codex-vscode-proxy: " + message, file=sys.stderr)
    raise SystemExit(78)


def absolute(value, label):
    value = str(value)
    if not value or any(c in value for c in "\x00\r\n") or not Path(value).is_absolute():
        fail(f"invalid {label}: {value!r}")
    return Path(value).resolve()


def persistent_config_path():
    config_root = absolute(
        os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")),
        "configuration root",
    )
    return config_root / "codex-desktop/persistent-app-server.json"


def read_config():
    path = persistent_config_path()
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


def ensure_config():
    manager = Path(__file__).resolve().with_name("manage.py")
    try:
        subprocess.run(
            [sys.executable, str(manager), "ensure", "--no-linger"],
            check=True,
        )
    except subprocess.CalledProcessError as error:
        fail(f"cannot configure the persistent user service: exit {error.returncode}")
    return read_config()


def config_overrides_only(args):
    index = 0
    while index < len(args):
        argument = args[index]
        if argument in ("-c", "--config", "--enable", "--disable"):
            if index + 1 >= len(args):
                return False
            index += 2
            continue
        if argument.startswith(("-c=", "--config=", "--enable=", "--disable=")):
            index += 1
            continue
        return False
    return True


def desktop_proxy_config_args(args, socket_path):
    try:
        command_index = args.index("app-server")
    except ValueError:
        return None
    expected = ["app-server", "proxy", "--sock", str(socket_path)]
    if args[command_index:command_index + len(expected)] != expected:
        return None
    remaining = args[:command_index] + args[command_index + len(expected):]
    return remaining if config_overrides_only(remaining) else None


def cli_config_values(args, target_key):
    values = []
    index = 0
    while index < len(args):
        argument = args[index]
        if argument in ("-c", "--config"):
            index += 1
            assignment = args[index]
        elif argument.startswith(("-c=", "--config=")):
            assignment = argument.split("=", 1)[1]
        else:
            index += 2 if argument in ("--enable", "--disable") else 1
            continue
        key, separator, value = assignment.partition("=")
        if separator and key.strip() == target_key:
            values.append(value.strip())
        index += 1
    return values


def parse_generated_inline_table(value):
    """Parse the strict TOML subset emitted by the Desktop launch bridge."""
    source = value.strip()
    if not source.startswith("{") or not source.endswith("}"):
        raise ValueError("expected an inline table")
    output = []
    containers = []
    index = 0
    while index < len(source):
        char = source[index]
        if (
            char == '"'
            and containers
            and containers[-1][0] == "object"
            and containers[-1][1]
        ):
            start = index
            index += 1
            escaped = False
            while index < len(source):
                current = source[index]
                index += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
            else:
                raise ValueError("unterminated quoted key")
            key = source[start:index]
            if not isinstance(json.loads(key), str):
                raise ValueError("invalid quoted key")
            while index < len(source) and source[index].isspace():
                index += 1
            if index >= len(source) or source[index] != "=":
                raise ValueError("expected a quoted key assignment")
            output.append(key + ":")
            containers[-1][1] = False
            index += 1
            continue
        if char == '"':
            start = index
            index += 1
            escaped = False
            while index < len(source):
                current = source[index]
                index += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
            else:
                raise ValueError("unterminated basic string")
            output.append(source[start:index])
            continue
        if char == "{":
            containers.append(["object", True])
            output.append(char)
            index += 1
            continue
        if char == "[":
            containers.append(["array", False])
            output.append(char)
            index += 1
            continue
        if char in "}]":
            expected = "object" if char == "}" else "array"
            if not containers or containers[-1][0] != expected:
                raise ValueError("mismatched inline container")
            containers.pop()
            output.append(char)
            index += 1
            continue
        if containers and containers[-1][0] == "object" and containers[-1][1]:
            if char.isspace():
                output.append(char)
                index += 1
                continue
            start = index
            while index < len(source) and (source[index].isalnum() or source[index] in "_-"):
                index += 1
            key = source[start:index]
            while index < len(source) and source[index].isspace():
                index += 1
            if not key or index >= len(source) or source[index] != "=":
                raise ValueError("expected a bare key assignment")
            output.append(json.dumps(key) + ":")
            containers[-1][1] = False
            index += 1
            continue
        if char == ",":
            if containers and containers[-1][0] == "object":
                containers[-1][1] = True
            output.append(char)
            index += 1
            continue
        if char in "='":
            raise ValueError("unsupported generated TOML syntax")
        output.append(char)
        index += 1
    if containers:
        raise ValueError("unterminated inline container")
    parsed = json.loads("".join(output))
    if not isinstance(parsed, dict):
        raise ValueError("expected an inline table")
    return parsed


def desktop_mcp_config(config_args):
    values = cli_config_values(config_args, DESKTOP_MCP_CONFIG_KEY)
    if not values:
        return None
    if len(values) != 1:
        fail(f"expected exactly one {DESKTOP_MCP_CONFIG_KEY} launch override")
    try:
        config = parse_generated_inline_table(values[0])
    except (json.JSONDecodeError, ValueError) as error:
        fail(f"cannot parse {DESKTOP_MCP_CONFIG_KEY} launch override: {error}")
    if not isinstance(config, dict) or not isinstance(config.get("command"), str):
        fail(f"{DESKTOP_MCP_CONFIG_KEY} launch override must define a stdio command")

    environment = config.get("env")
    if environment is None:
        environment = {}
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ):
        fail(f"{DESKTOP_MCP_CONFIG_KEY}.env must contain only string values")
    environment = dict(environment)

    env_vars = config.get("env_vars", [])
    if not isinstance(env_vars, list):
        fail(f"{DESKTOP_MCP_CONFIG_KEY}.env_vars must be an array")
    for entry in env_vars:
        if isinstance(entry, str):
            name, source = entry, None
        elif isinstance(entry, dict):
            name, source = entry.get("name"), entry.get("source")
        else:
            fail(f"{DESKTOP_MCP_CONFIG_KEY}.env_vars contains an invalid entry")
        if not isinstance(name, str) or source not in (None, "local"):
            fail(f"{DESKTOP_MCP_CONFIG_KEY}.env_vars contains an invalid entry")
        if name not in DESKTOP_MCP_ENV_VARS:
            fail(f"{DESKTOP_MCP_CONFIG_KEY}.env_vars contains an unapproved name: {name}")
        if name in os.environ:
            environment.setdefault(name, os.environ[name])
    if any(
        (entry == "CODEX_APP_TOOLS_PIPE_PATH")
        or (isinstance(entry, dict) and entry.get("name") == "CODEX_APP_TOOLS_PIPE_PATH")
        for entry in env_vars
    ) and "CODEX_APP_TOOLS_PIPE_PATH" not in environment:
        fail("Desktop app-tools pipe is unavailable in the adapter environment")

    config["env"] = environment
    return config


def merge_dict(target, source):
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_dict(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def set_dotted_value(target, dotted_key, value):
    parts = dotted_key.split(".")
    if not parts or any(not part for part in parts):
        fail(f"invalid {DESKTOP_MCP_CONFIG_KEY} request override: {dotted_key!r}")
    current = target
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = copy.deepcopy(value)


def inject_desktop_mcp_config(payload, base_config):
    if base_config is None:
        return payload
    try:
        request = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"cannot parse Desktop app-server request: {error}")
    if not isinstance(request, dict) or request.get("method") not in THREAD_CONFIG_METHODS:
        return payload
    params = request.get("params")
    if params is None:
        params = {}
        request["params"] = params
    if not isinstance(params, dict):
        fail("Desktop thread request params must be an object")
    overrides = params.get("config")
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        fail("Desktop thread config overrides must be an object")
    overrides = dict(overrides)

    merged = copy.deepcopy(base_config)
    parent_override = overrides.pop(DESKTOP_MCP_CONFIG_KEY, None)
    if parent_override is not None:
        if not isinstance(parent_override, dict):
            fail(f"{DESKTOP_MCP_CONFIG_KEY} request override must be an object")
        merge_dict(merged, parent_override)
    prefix = DESKTOP_MCP_CONFIG_KEY + "."
    for key in list(overrides):
        if key.startswith(prefix):
            set_dotted_value(merged, key[len(prefix):], overrides.pop(key))
    overrides[DESKTOP_MCP_CONFIG_KEY] = merged
    params["config"] = overrides
    return json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


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


def bridge_jsonl_to_websocket(socket_path, desktop_config=None):
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
                    payload = inject_desktop_mcp_config(payload, desktop_config)
                    if len(payload) > MAX_MESSAGE_BYTES:
                        raise ValueError("rewritten app-server message exceeds 64 MiB")
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
    args = sys.argv[1:]
    if os.path.lexists(persistent_config_path()):
        config = read_config()
    else:
        provisional_home = absolute(
            os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
            "Codex home",
        )
        provisional_socket = provisional_home / "app-server-control/app-server-control.sock"
        if (
            "app-server" in args
            and args != EXPECTED_APP_SERVER_ARGS
            and desktop_proxy_config_args(args, provisional_socket) is None
        ):
            fail(
                "refusing an unrecognized app-server launch; update the persistent "
                "proxy contract before reloading VS Code"
            )
        config = ensure_config()
    app_dir = absolute(config.get("app_dir", ""), "app directory")
    codex_home = absolute(config.get("codex_home", ""), "Codex home")
    binary = app_dir / "resources/codex"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        fail(f"packaged Codex CLI is unavailable: {binary}")

    socket_path = codex_home / "app-server-control/app-server-control.sock"
    config_args = desktop_proxy_config_args(args, socket_path)
    if (
        "app-server" in args
        and args != EXPECTED_APP_SERVER_ARGS
        and config_args is None
    ):
        fail(
            "refusing an unrecognized app-server launch; update the persistent "
            "proxy contract before reloading VS Code"
        )
    if "app-server" in args and not socket_path.exists():
        config = ensure_config()
    if args == EXPECTED_APP_SERVER_ARGS:
        bridge_jsonl_to_websocket(socket_path)
        return
    if config_args is not None:
        bridge_jsonl_to_websocket(socket_path, desktop_mcp_config(config_args))
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
