"""Reusable local authorization with one connection per kubectl client."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from lab import clock
from lab.auth import peer_uid
from lab.config import Index, cluster_name, parse_cluster, parse_index
from lab.editor import stop_client
from lab.errors import LabError
from lab.policy import load_profile
from lab.secrets import Secrets
from lab.session import Session, Tools
from lab.tools import Toolchain

IDLE_TIMEOUT = 900
FRAME_LIMIT = 4 * 1024 * 1024


def socket_path(identity: str) -> Path:
    """Resolve an opaque session identifier without accepting arbitrary paths."""
    if len(identity) > 64 or not re.fullmatch(r"lab-session-[a-z0-9_]+", identity):
        raise LabError("Invalid session ID. Use the ID printed by lab session start.", 2)
    return Path("/tmp") / identity / "socket"


def _check_socket(path: Path) -> None:
    try:
        for entry, kind, mode in (
            (path.parent, stat.S_ISDIR, 0o700),
            (path, stat.S_ISSOCK, 0o600),
        ):
            info = entry.lstat()
            if (
                not kind(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != mode
            ):
                raise OSError
    except OSError:
        raise LabError("Session unavailable. Start a new authorization session.", 3) from None


def _check_peer(writer: asyncio.StreamWriter) -> None:
    if peer_uid(writer.get_extra_info("socket")) != os.getuid():
        raise LabError("Authorization session belongs to another user.", 3)


async def _send(writer: asyncio.StreamWriter, value: dict) -> None:
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    if len(data) > FRAME_LIMIT:
        raise LabError("Session response exceeds its size limit.", 4)
    writer.write(data)
    await writer.drain()


async def _receive(reader: asyncio.StreamReader) -> dict:
    try:
        raw = await reader.readline()
        if not raw:
            raise LabError("Authorization session ended. Start a new session.", 3)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise LabError("Invalid authorization session message.", 2) from None


class Authorization:
    """Keep 1Password access in one foreground process until close or expiry.

    Each connected client owns its operation and its cleanup. A kubectl client
    holds its socket open for as long as it uses the associated cluster Session.
    """

    def __init__(self, secrets: Secrets, bootstrap: dict, index: Index, authorized_at: float):
        self.secrets: Secrets | None = secrets
        self.index_ref = bootstrap["index_ref"]
        self.profiles_dir = Path(bootstrap["profiles_dir"])
        self.authorized_at = authorized_at
        self.max_age = index.session.max_age_seconds
        self.deadline = authorized_at + self.max_age
        secrets.deadline = self.deadline
        self.idle_since = clock.now()
        self.clients: set[asyncio.Task] = set()
        self.closers: set[asyncio.Task] = set()
        self.stopping = asyncio.Event()
        self.closed = asyncio.Event()
        self.reason = "explicit close"
        self.cleanup_error: LabError | None = None

    def _check(self) -> None:
        if self.stopping.is_set() or clock.now() >= self.deadline:
            raise LabError("Authorization session ended. Start a new session.", 3)

    async def _index(self) -> Index:
        self._check()
        assert self.secrets is not None
        return parse_index(await self.secrets.read(self.index_ref))

    async def _cluster(self, identity: str):
        index = await self._index()
        entry = next((entry for entry in index.clusters if entry.id == identity), None)
        if entry is None:
            raise LabError("Cluster is not registered. Use lab cluster list with this session.", 4)
        assert self.secrets is not None
        return parse_cluster(await self.secrets.read(entry.config_ref), identity)

    async def _inspect(self, identity: str) -> dict:
        cluster = await self._cluster(identity)
        result = {
            "id": identity,
            "context": cluster.kubernetes.context,
            "server": cluster.connection.server,
            "transport": cluster.transport.model_dump(mode="json"),
            "profile": None,
        }
        try:
            result["profile"] = load_profile(self.profiles_dir, identity).model_dump(mode="json")
        except LabError as error:
            result["profile_error"] = str(error)
        return result

    async def _kubectl(self, identity: str, reader, writer) -> None:
        cluster = await self._cluster(identity)
        tools = Toolchain.load()
        executable = tools.require("kubectl")
        connection = Session(
            cluster.connection,
            cluster.transport,
            self.max_age,
            Tools(tools.ssh, tools.python, tools.credential),
            cluster_id=identity,
            authorized_at=self.authorized_at,
        )
        try:
            await connection.connect()
            grant = await connection.create_grant()
            self._check()
            await _send(
                writer,
                {
                    "data": {
                        "executable": str(executable),
                        "kubeconfig": str(grant.config_path),
                        "environment": grant.environment,
                    }
                },
            )
            await reader.read(1)
        finally:
            cleanup = asyncio.create_task(self._close_connection(connection))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

    async def _close_connection(self, connection: Session) -> None:
        try:
            await connection.close()
        except (LabError, OSError):
            self.cleanup_error = LabError(
                "Local connection cleanup could not be fully confirmed.", 8
            )
            self.stopping.set()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.clients.add(task)
        try:
            self._check()
            _check_peer(writer)
            async with asyncio.timeout(5):
                request = await _receive(reader)
            operation = request.get("operation")
            if operation in {"list", "close"}:
                expected = {"operation"}
            elif operation in {"inspect", "kubectl"}:
                expected = {"operation", "cluster"}
                identity = request.get("cluster")
                if not isinstance(identity, str):
                    raise LabError("Invalid cluster ID.", 2)
                try:
                    cluster_name(identity)
                except (ValueError, TypeError, AttributeError):
                    raise LabError("Invalid cluster ID.", 2) from None
            else:
                raise LabError("Unknown authorization session operation.", 2)
            if set(request) != expected:
                raise LabError("Invalid authorization session request.", 2)
            data: dict
            if operation == "close":
                self.closers.add(task)
                self.stopping.set()
                await self.closed.wait()
                if self.cleanup_error:
                    raise self.cleanup_error
                data = {"status": "Closed"}
            elif operation == "list":
                index = await self._index()
                data = {"clusters": [{"id": entry.id} for entry in index.clusters]}
            elif operation == "inspect":
                data = await self._inspect(request["cluster"])
            else:
                await self._kubectl(request["cluster"], reader, writer)
                return
            await _send(writer, {"data": data})
        except LabError as error:
            with contextlib.suppress(OSError):
                await _send(writer, {"error": str(error), "code": error.code})
        except (OSError, TimeoutError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            with contextlib.suppress(OSError):
                await _send(writer, {"error": "Authorization session request failed.", "code": 8})
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.clients.discard(task)
            self.closers.discard(task)
            if not self.clients:
                self.idle_since = clock.now()

    async def _watch(self) -> None:
        while not self.stopping.is_set():
            now = clock.now()
            if now >= self.deadline:
                self.reason = "authorization expired"
                self.stopping.set()
            elif not self.clients and now - self.idle_since >= IDLE_TIMEOUT:
                self.reason = "idle timeout"
                self.stopping.set()
            else:
                await asyncio.sleep(0.2)

    async def serve(self, ready: Callable[[dict], None]) -> None:
        """Publish a private socket, then drain all clients before removing it."""
        with tempfile.TemporaryDirectory(prefix="lab-session-", dir="/tmp") as directory:
            identity = Path(directory).name
            path = socket_path(identity)
            server = await asyncio.start_unix_server(self._handle, path, limit=FRAME_LIMIT)
            os.chmod(path, 0o600)
            watcher = asyncio.create_task(self._watch())
            try:
                self._check()
                ready(
                    {
                        "session": identity,
                        "expires_in": max(0, self.deadline - clock.now()),
                        "idle_timeout": IDLE_TIMEOUT,
                    }
                )
                await self.stopping.wait()
            finally:
                self.stopping.set()
                server.close()
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                running = self.clients - self.closers
                for task in running:
                    task.cancel()
                await asyncio.gather(*running, return_exceptions=True)
                self.secrets = None
                self.closed.set()
                await asyncio.gather(*self.closers, return_exceptions=True)
                await server.wait_closed()
        if self.cleanup_error:
            raise self.cleanup_error


@contextlib.asynccontextmanager
async def _client(identity: str, operation: str, cluster: str | None = None):
    path = socket_path(identity)
    _check_socket(path)
    writer = None
    try:
        async with asyncio.timeout(90):
            reader, writer = await asyncio.open_unix_connection(path, limit=FRAME_LIMIT)
            _check_peer(writer)
            request = {"operation": operation}
            if cluster is not None:
                request["cluster"] = cluster
            await _send(writer, request)
            response = await _receive(reader)
            if "error" in response:
                raise LabError(response["error"], response["code"])
            if "data" not in response:
                raise LabError("Invalid authorization session response.", 3)
        yield reader, response["data"]
    except (OSError, TimeoutError):
        raise LabError("Session unavailable. Start a new authorization session.", 3) from None
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


async def request(identity: str, operation: str, cluster: str | None = None) -> dict:
    """Run one metadata or close operation through an existing authorization."""
    async with _client(identity, operation, cluster) as (_, data):
        return data


async def kubectl(identity: str, cluster: str, arguments: list[str]) -> int:
    """Run native kubectl with inherited stdio for the duration of one socket lease."""
    async with _client(identity, "kubectl", cluster) as (reader, grant):
        process = None
        tasks: list[asyncio.Task] = []
        try:
            process = await asyncio.create_subprocess_exec(
                grant["executable"],
                *arguments,
                env={**os.environ, **grant["environment"], "KUBECONFIG": grant["kubeconfig"]},
            )
            exited = asyncio.create_task(process.wait())
            disconnected = asyncio.create_task(reader.read(1))
            tasks = [exited, disconnected]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if exited in done:
                code = exited.result()
                return code if code >= 0 else 128 - code
            raise LabError("Authorization session ended; the local client was disconnected.", 3)
        except OSError:
            raise LabError("kubectl could not be started.", 9) from None
        finally:
            if process is not None:
                await stop_client(process)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
