"""Authenticated, target-bound ExecCredential grants over a private Unix socket."""

import argparse
import asyncio
import base64
import ctypes
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import socket
import stat
import struct
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Never, Protocol

import aiohttp
from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from lab.errors import LabError
from lab.tools import protect_process

PROTOCOL = "client.authentication.k8s.io/v1"
CAPABILITY_ENV = "LAB_GRANT_CAPABILITY"
BODY_LIMIT = 64 * 1024


class CredentialSession(Protocol):
    cluster_id: str
    endpoint: str
    tls_name: str
    ca_data: str | None

    @property
    def remaining(self) -> float: ...

    @property
    def token(self) -> SecretStr: ...

    def check(self) -> None: ...


class ClusterInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)
    server: str
    tls_name: str = Field(alias="tls-server-name")
    ca_data: str | None = Field(default=None, alias="certificate-authority-data")
    insecure: bool = Field(default=False, alias="insecure-skip-tls-verify")
    proxy_url: str | None = Field(default=None, alias="proxy-url")
    disable_compression: bool = Field(default=False, alias="disable-compression")
    config: dict[str, Any] | None = None


class CredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    grant_id: str
    cluster: ClusterInfo


@dataclass(frozen=True)
class Grant:
    """A client configuration whose environment additions contain a secret capability."""

    id: str
    config_path: Path
    environment: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class _Binding:
    grant: Grant
    capability: SecretStr
    cluster_id: str
    context: str
    endpoint: str
    tls_name: str
    ca_fingerprint: bytes | None


def _fingerprint(value: str | None) -> bytes | None:
    return hashlib.sha256(base64.b64decode(value, validate=True)).digest() if value else None


def _private(path: Path, *, directory: bool) -> None:
    details = path.lstat()
    mode = 0o700 if directory else 0o600
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(details.st_mode) or details.st_uid != os.geteuid():
        raise LabError("A session runtime path has unexpected ownership or type.")
    if stat.S_IMODE(details.st_mode) != mode:
        raise LabError("A session runtime path has unsafe permissions.")


def _peer_uid(peer: Any) -> int | None:
    if peer is None:
        return None
    if hasattr(socket, "SO_PEERCRED"):
        data = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", data)[1]
    if sys.platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        getpeereid = library.getpeereid
        getpeereid.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        getpeereid.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if getpeereid(peer.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise OSError("Peer identity unavailable")
        return uid.value
    return None


class CredentialService:
    """Issue only the current session's credential to authenticated, matching live grants."""

    def __init__(self, session: CredentialSession, helper: Path) -> None:
        self._session = session
        self._helper = helper
        self._root: Path | None = None
        self.socket_path: Path | None = None
        self._runner: web.AppRunner | None = None
        self._bindings: dict[str, _Binding] = {}
        self._active = 0
        self._lock = asyncio.Lock()

    async def _start(self) -> None:
        if self._runner is not None:
            return
        self._root = Path(tempfile.mkdtemp(prefix="lab-", dir="/tmp"))
        os.chmod(self._root, 0o700)
        _private(self._root, directory=True)
        self.socket_path = self._root / "credentials.sock"
        app = web.Application(client_max_size=BODY_LIMIT)
        app.router.add_post("/v1/credential", self._handle)
        logger = logging.Logger("lab.credential.http")
        logger.disabled = True
        self._runner = web.AppRunner(
            app, access_log=None, logger=logger, shutdown_timeout=1, keepalive_timeout=5
        )
        await self._runner.setup()
        await web.UnixSite(self._runner, str(self.socket_path)).start()
        os.chmod(self.socket_path, 0o600)

    async def create_grant(self) -> Grant:
        async with self._lock:
            self._session.check()
            await self._start()
            self._session.check()
            assert self._root is not None and self.socket_path is not None
            _private(self._root, directory=True)
            grant_id = str(uuid.uuid4())
            capability = secrets.token_urlsafe(32)
            directory = self._root / grant_id
            directory.mkdir(mode=0o700)
            _private(directory, directory=True)
            context = f"lab-{self._session.cluster_id}-{grant_id}"
            config_path = directory / "kubeconfig.json"
            cluster = {
                "server": self._session.endpoint,
                "tls-server-name": self._session.tls_name,
            }
            if self._session.ca_data:
                cluster["certificate-authority-data"] = self._session.ca_data
            config = {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": context,
                "clusters": [{"name": context, "cluster": cluster}],
                "contexts": [{"name": context, "context": {"cluster": context, "user": context}}],
                "users": [
                    {
                        "name": context,
                        "user": {
                            "exec": {
                                "apiVersion": PROTOCOL,
                                "command": str(self._helper),
                                "args": ["--socket", str(self.socket_path), "--grant", grant_id],
                                "interactiveMode": "Never",
                                "provideClusterInfo": True,
                            }
                        },
                    }
                ],
            }
            try:
                fd = os.open(
                    config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "w") as output:
                    json.dump(config, output, separators=(",", ":"))
                    output.flush()
                    os.fsync(output.fileno())
                grant = Grant(grant_id, config_path, {CAPABILITY_ENV: capability})
                self._session.check()
                self._bindings[grant_id] = _Binding(
                    grant,
                    SecretStr(capability),
                    self._session.cluster_id,
                    context,
                    self._session.endpoint,
                    self._session.tls_name,
                    _fingerprint(self._session.ca_data),
                )
                return grant
            except BaseException:
                shutil.rmtree(directory)
                raise

    async def revoke_grant(self, grant: Grant) -> None:
        async with self._lock:
            binding = self._bindings.pop(grant.id, None)
            if binding is None:
                return
            assert self._root is not None
            _private(self._root, directory=True)
            directory = binding.grant.config_path.parent
            _private(directory, directory=True)
            shutil.rmtree(directory)
            binding.grant.environment.clear()

    async def _handle(self, request: web.Request) -> web.Response:
        if self._active >= 32:
            return self._error("busy", 429)
        self._active += 1
        try:
            async with asyncio.timeout(5):
                transport = request.transport
                uid = _peer_uid(transport.get_extra_info("socket") if transport else None)
                if uid is not None and uid != os.geteuid():
                    return self._error("denied", 403)
                self._session.check()
                if request.content_type != "application/json":
                    return self._error("malformed", 400)
                data = await request.read()
                payload = CredentialRequest.model_validate_json(data)
                binding = self._bindings.get(payload.grant_id)
                supplied = request.headers.get("Authorization", "")
                if len(supplied) > 256 or not supplied.startswith("Bearer "):
                    return self._error("denied", 403)
                expected = (
                    binding.capability.get_secret_value() if binding else secrets.token_urlsafe(32)
                )
                if (
                    not hmac.compare_digest(supplied[7:].encode(), expected.encode())
                    or binding is None
                ):
                    return self._error("denied", 403)
                cluster = payload.cluster
                if (
                    binding.cluster_id != self._session.cluster_id
                    or cluster.server != binding.endpoint
                    or cluster.tls_name != binding.tls_name
                    or cluster.insecure
                    or cluster.proxy_url is not None
                    or _fingerprint(cluster.ca_data) != binding.ca_fingerprint
                ):
                    return self._error("denied", 403)
                self._session.check()
                lifetime = min(60.0, self._session.remaining)
                if lifetime <= 0:
                    return self._error("expired", 403)
                expires = datetime.now(UTC) + timedelta(seconds=lifetime)
                response = {
                    "apiVersion": PROTOCOL,
                    "kind": "ExecCredential",
                    "status": {
                        "expirationTimestamp": expires.isoformat().replace("+00:00", "Z"),
                        "token": self._session.token.get_secret_value(),
                    },
                }
                self._session.check()
                return web.json_response(response, headers={"Cache-Control": "no-store"})
        except web.HTTPRequestEntityTooLarge:
            return self._error("malformed", 413)
        except (ValidationError, ValueError, UnicodeError, json.JSONDecodeError):
            return self._error("malformed", 400)
        except LabError:
            return self._error("unavailable", 403)
        except (OSError, TimeoutError):
            return self._error("unavailable", 503)
        finally:
            self._active -= 1

    @staticmethod
    def _error(code: str, status: int) -> web.Response:
        return web.json_response(
            {"error": code}, status=status, headers={"Cache-Control": "no-store"}
        )

    async def close(self) -> None:
        async with self._lock:
            for binding in self._bindings.values():
                binding.grant.environment.clear()
            self._bindings.clear()
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
            if self._root is not None:
                _private(self._root, directory=True)
                shutil.rmtree(self._root)
                self._root = None
                self.socket_path = None


async def _fetch(socket_path: Path, grant: str, capability: str, exec_info: str) -> dict[str, Any]:
    _private(socket_path.parent, directory=True)
    details = socket_path.lstat()
    if (
        not stat.S_ISSOCK(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise LabError("The credential socket is unavailable.")
    if len(exec_info.encode()) > BODY_LIMIT or len(capability) > 128 or not capability:
        raise LabError("The credential request is invalid.")
    info = json.loads(exec_info)
    if (
        not isinstance(info, dict)
        or info.get("apiVersion") != PROTOCOL
        or info.get("kind") != "ExecCredential"
        or not isinstance(info.get("spec"), dict)
        or info["spec"].get("interactive") is not False
    ):
        raise LabError("A noninteractive v1 ExecCredential request is required.")
    cluster = ClusterInfo.model_validate(info["spec"].get("cluster"))
    connector = aiohttp.UnixConnector(path=str(socket_path))
    async with (
        aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=5), trust_env=False
        ) as client,
        client.post(
            "http://localhost/v1/credential",
            headers={"Authorization": f"Bearer {capability}"},
            json={
                "grant_id": grant,
                "cluster": cluster.model_dump(by_alias=True, exclude_none=True),
            },
            allow_redirects=False,
        ) as response,
    ):
        if response.status != 200:
            raise LabError(
                "The credential grant is unavailable. Return to the authorized lab session."
            )
        raw = bytearray()
        async for chunk in response.content.iter_chunked(16 * 1024):
            raw.extend(chunk)
            if len(raw) > BODY_LIMIT:
                raise LabError("The credential response is invalid.")
        result = json.loads(raw)
        if (
            not isinstance(result, dict)
            or result.get("apiVersion") != PROTOCOL
            or result.get("kind") != "ExecCredential"
            or not isinstance(result.get("status"), dict)
            or not isinstance(result["status"].get("token"), str)
            or not result["status"]["token"]
            or len(result["status"]["token"].encode()) > 16 * 1024
            or not isinstance(result["status"].get("expirationTimestamp"), str)
        ):
            raise LabError("The credential response is invalid.")
        return result


class _HelperParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise LabError("The credential helper arguments are invalid.")


def main() -> int:
    """Write a validated credential only to captured stdout for the fixed local grant."""
    try:
        protect_process()
        output_mode = os.fstat(sys.stdout.fileno()).st_mode
        if not (stat.S_ISFIFO(output_mode) or stat.S_ISSOCK(output_mode)):
            raise LabError("The credential helper requires captured output.")
        parser = _HelperParser(add_help=False, exit_on_error=False)
        parser.add_argument("--socket", type=Path, required=True)
        parser.add_argument("--grant", required=True)
        args = parser.parse_args()
        capability = os.environ.pop(CAPABILITY_ENV, "")
        info = os.environ.pop("KUBERNETES_EXEC_INFO", "")
        result = asyncio.run(_fetch(args.socket, args.grant, capability, info))
        sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 0
    except (Exception, SystemExit):
        sys.stderr.write("lab: credential grant unavailable; return to the authorized session.\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
