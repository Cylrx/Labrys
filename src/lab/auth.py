"""One private, expiring token handoff between two same-user processes."""

import asyncio
import contextlib
import ctypes
import json
import math
import os
import select
import shlex
import socket
import stat
import struct
import sys
import tempfile
import termios
import time
from pathlib import Path

from lab.errors import LabError

LIFETIME = 300
MAX_FRAME = 100000


def _error(message="Authentication request is invalid, unavailable, or expired."):
    return LabError(message, 3)


def peer_uid(connection: socket.socket) -> int:
    """Require kernel-reported Unix peer identity on Linux and macOS."""
    if hasattr(socket, "SO_PEERCRED"):
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        return struct.unpack("3i", credentials)[1]
    if sys.platform == "darwin":
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
            return uid.value
    raise _error("Unix peer identity is unavailable; authentication was refused.")


def _peer(connection):
    if peer_uid(connection) != os.getuid():
        raise _error("Authentication peer belongs to a different user.")


def _encode(value):
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
    if len(raw) > MAX_FRAME:
        raise _error()
    return raw


def _decode(raw):
    try:

        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError
                result[key] = item
            return result

        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise _error("Invalid authentication protocol frame.") from None


def description(args):
    """Describe parsed options without a token or an executable command payload."""
    if args.command == "session" and args.action == "start":
        return {"command": "session", "action": "start"}
    return {
        key: value
        for key, value in vars(args).items()
        if key not in {"request_auth", "token_stdin"} and value is not None
    }


def _validate_manifest(value):
    if set(value) != {"version", "id", "expires", "action"}:
        raise _error()
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["id"], str)
    ):
        raise _error()
    if len(value["id"]) != 32 or any(c not in "0123456789abcdef" for c in value["id"]):
        raise _error()
    expiry = value["expires"]
    if type(expiry) not in {int, float} or not math.isfinite(expiry):
        raise _error()
    if not 0 < expiry - time.time() <= LIFETIME + 2:
        raise _error()
    action = value["action"]
    if action == {"command": "session", "action": "start"}:
        return value
    if not isinstance(action, dict) or action.get("command") != "notebook":
        raise _error()
    allowed: dict[str, type | tuple[type, ...]] = {
        "command": str,
        "action": str,
        "cluster": str,
        "namespace": str,
        "name": str,
        "pod": str,
        "container": str,
        "helper_python": str,
        "operation_id": str,
        "preset": str,
        "image": str,
        "gpu_type": str,
        "gpus": int,
        "cpu": str,
        "memory": str,
        "node": str,
        "storage_source": str,
        "mount_path": str,
        "workdir": str,
        "owner": str,
        "wait": bool,
        "timeout": (int, float),
        "yes": bool,
        "json": bool,
    }
    if action.get("action") not in {
        "list",
        "create",
        "status",
        "shell",
        "open",
        "start",
        "stop",
        "delete",
        "retry",
        "editor-restart",
    }:
        raise _error()
    if not action.get("cluster") or not (action.get("namespace") or action.get("operation_id")):
        raise _error()
    for key, item in action.items():
        expected = allowed.get(key)
        if expected is None or type(item) not in (
            expected if isinstance(expected, tuple) else (expected,)
        ):
            raise _error()
        if isinstance(item, float) and not math.isfinite(item):
            raise _error()
    return value


def _identity(path, kind, mode):
    info = path.lstat()
    if (
        not kind(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != mode
        or (kind is not stat.S_ISDIR and info.st_nlink != 1)
    ):
        raise _error()
    return info.st_dev, info.st_ino


def _request(path):
    if not path.is_absolute() or path.name != "request.json" or path.resolve() != path:
        raise _error()
    directory = path.parent
    directory_id = _identity(directory, stat.S_ISDIR, 0o700)
    request_id = _identity(path, stat.S_ISREG, 0o600)
    socket_id = _identity(directory / "socket", stat.S_ISSOCK, 0o600)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (info.st_dev, info.st_ino) != request_id:
            raise _error()
        raw = source.read(MAX_FRAME + 1)
    if len(raw) > MAX_FRAME:
        raise _error()
    return _validate_manifest(_decode(raw)), (directory_id, request_id, socket_id)


async def _receive(connection, deadline):
    loop = asyncio.get_running_loop()
    raw = bytearray()
    async with asyncio.timeout_at(deadline):
        while len(raw) < MAX_FRAME:
            value = await loop.sock_recv(connection, 1)
            if not value:
                raise _error("Authentication connection closed before completion.")
            raw.extend(value)
            if value == b"\n":
                return _decode(raw)
    raise _error("Authentication protocol frame exceeds its size limit.")


async def request_token(action: dict, *, lifetime: float = LIFETIME) -> str:
    """Wait once for a human sender; keep the token only in process memory."""
    lifetime = min(lifetime, LIFETIME)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + lifetime
    with tempfile.TemporaryDirectory(prefix="lab-auth-", dir="/tmp") as temporary:
        directory = Path(temporary).resolve()
        os.chmod(directory, 0o700)
        manifest = {
            "version": 1,
            "id": os.urandom(16).hex(),
            "expires": time.time() + lifetime,
            "action": action,
        }
        path = directory / "request.json"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(directory / "socket"))
            os.chmod(directory / "socket", 0o600)
            listener.listen(1)
            listener.setblocking(False)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(_encode(manifest))
            original_request = _request(path)
            print(
                "In your own terminal, run:\n"
                + shlex.join([sys.executable, "-m", "lab.cli", "auth", "--request", str(path)]),
                file=sys.stderr,
                flush=True,
            )
            try:
                async with asyncio.timeout_at(deadline):
                    connection, _ = await loop.sock_accept(listener)
                    with connection:
                        connection.setblocking(False)
                        _peer(connection)
                        hello = await _receive(connection, deadline)
                        if type(hello.get("version")) is not int or hello != {
                            "version": 1,
                            "id": manifest["id"],
                            "type": "hello",
                        }:
                            raise _error()
                        if _request(path) != original_request:
                            raise _error()
                        await loop.sock_sendall(
                            connection, _encode({"type": "ready", "request": manifest})
                        )
                        frame = await _receive(connection, deadline)
                        if (
                            set(frame) != {"type", "id", "token"}
                            or frame["type"] != "token"
                            or frame["id"] != manifest["id"]
                            or not isinstance(frame["token"], str)
                            or not 0 < len(frame["token"].encode()) <= 16384
                            or any(ord(c) < 32 or ord(c) == 127 for c in frame["token"])
                        ):
                            raise _error()
                        if _request(path) != original_request:
                            raise _error()
                        token = frame.pop("token")
                        listener.close()
                        path.unlink()
                        (directory / "socket").unlink()
                        # ACK only acknowledges receipt, never successful 1Password authorization.
                        with contextlib.suppress(OSError):
                            await loop.sock_sendall(connection, _encode({"type": "received"}))
                        return token
            except (TimeoutError, OSError):
                raise _error(
                    "Authentication handoff timed out or disconnected; no retry was made."
                ) from None


def _read(connection, deadline):
    raw = bytearray()
    while len(raw) < MAX_FRAME:
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        if time.monotonic() >= deadline:
            raise _error()
        value = connection.recv(1)
        if not value:
            raise _error()
        raw.extend(value)
        if value == b"\n":
            return _decode(raw)
    raise _error()


def hidden_token(deadline: float) -> str:
    """Read only from a real terminal with echo disabled; never fall back to stdin."""
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        raise _error("A controlling terminal is required for hidden token input.") from None
    try:
        settings = termios.tcgetattr(fd)
        hidden = list(settings)
        hidden[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON)
        hidden[6] = list(settings[6])
        hidden[6][termios.VMIN] = 1
        hidden[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSAFLUSH, hidden)
        try:
            os.set_blocking(fd, False)
            os.write(fd, b"Service Account Token (hidden): ")
            raw = bytearray()
            complete = False
            too_long = False
            while not complete:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                    raise _error("Authentication request expired during token input.")
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    raise _error()
                for value in chunk:
                    if value in {10, 13}:
                        complete = True
                        break
                    if too_long:
                        continue
                    if value in {8, 127}:
                        if raw:
                            start = len(raw) - 1
                            while start > 0 and raw[start] & 0xC0 == 0x80:
                                start -= 1
                            del raw[start:]
                    elif value == 21:
                        raw.clear()
                    elif value < 32:
                        raise _error("Token input cancelled or contains a control character.")
                    else:
                        raw.append(value)
                    if len(raw) > 16384:
                        too_long = True
            if not raw or too_long:
                raise _error("Token must contain between 1 and 16384 UTF-8 bytes.")
            token = raw.decode("utf-8")
            if any(ord(c) < 32 or ord(c) == 127 for c in token):
                raise _error("Token contains invalid control characters.")
            return token
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, settings)
            os.write(fd, b"\n")
    except (OSError, UnicodeError, termios.error):
        raise _error("Hidden terminal input failed; no token was sent.") from None
    finally:
        os.close(fd)


def show_request(action: dict) -> None:
    """Render only the intended operation and material options as escaped plain text."""
    reusable = action.get("command") == "session"
    print(
        "Authorize reusable lab access" if reusable else "Authorize independent lab access",
        file=sys.stderr,
    )
    if reusable:
        print(
            "  Scope: discover indexed clusters and run kubectl with their existing permissions.\n"
            "  Authorization remains available until explicit close or the session timeout.",
            file=sys.stderr,
        )
    labels = {
        "action": "Session action" if reusable else "Notebook action",
        "cluster": "Cluster",
        "namespace": "Namespace",
        "name": "Notebook",
        "pod": "Pod",
        "container": "Container",
        "operation_id": "Operation",
        "helper_python": "Helper interpreter",
    }
    for key, value in action.items():
        if key in {"command", "json", "wait", "yes"}:
            continue
        print(
            f"  {labels.get(key, key.replace('_', ' ').capitalize())}: "
            f"{json.dumps(value, ensure_ascii=True)}",
            file=sys.stderr,
        )
    if action.get("yes"):
        print(
            "  Confirmation: accept the requested operation without another prompt", file=sys.stderr
        )
    print("The token goes once to the waiting process and is never saved.", file=sys.stderr)


def send_token(request: str) -> None:
    """Verify an explicit live rendezvous, show its action, and submit exactly once."""
    transmitted = False
    try:
        path = Path(request)
        manifest, identities = _request(path)
        deadline = time.monotonic() + min(LIFETIME, manifest["expires"] - time.time())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(min(10, max(0.001, deadline - time.monotonic())))
            connection.connect(str(path.parent / "socket"))
            _peer(connection)
            if _request(path) != (manifest, identities):
                raise _error()
            connection.sendall(_encode({"version": 1, "id": manifest["id"], "type": "hello"}))
            if _read(connection, min(deadline, time.monotonic() + 10)) != {
                "type": "ready",
                "request": manifest,
            }:
                raise _error()
            show_request(manifest["action"])
            token = hidden_token(deadline)
            if time.monotonic() >= deadline or _request(path) != (manifest, identities):
                raise _error()
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            transmitted = True
            connection.sendall(_encode({"type": "token", "id": manifest["id"], "token": token}))
            token = ""
            if _read(connection, min(deadline, time.monotonic() + 10)) != {"type": "received"}:
                raise _error()
        print(
            "Token received by the requesting process. Check that process for authorization status."
        )
    except (LabError, OSError, ValueError, KeyboardInterrupt):
        if transmitted:
            raise _error(
                "Handoff outcome unknown: the requester may have received the token. "
                "Check the original process; do not resend to a replacement request."
            ) from None
        raise _error(
            "Authentication request was refused or cancelled; no token was sent."
        ) from None
