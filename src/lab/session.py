"""Authorization lifetime, validated connectivity, and per-client credential grants."""

import asyncio
import base64
import contextlib
import functools
import math
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import SecretStr

from lab import clock
from lab.credential import CredentialService, Grant
from lab.errors import LabError
from lab.transport import Dial, Relay, SSHForward


class Connection(Protocol):
    server: str
    tls_name: str
    ca_data: str | None
    token: SecretStr


class TransportConfig(Protocol):
    @property
    def mode(self) -> str: ...

    @property
    def ssh_target(self) -> str | None: ...


@dataclass(frozen=True)
class Tools:
    """Explicit executable paths selected and verified by the local installation."""

    ssh: Path | None
    python: Path
    credential: Path


class Session:
    """Keep access bounded by one authorization deadline and explicit reconnects.

    :param authorized_at: Suspend-inclusive authorization time; defaults to the current time.
    :param cluster_id: Stable operator-assigned cluster identity.
    :param route: SSH forwarder owned by the caller and shared with other sessions; the
        session starts and uses it but never closes it.
    """

    def __init__(
        self,
        connection: Connection,
        transport: TransportConfig,
        max_age_seconds: int,
        tools: Tools,
        *,
        cluster_id: str,
        authorized_at: float | None = None,
        route: SSHForward | None = None,
    ) -> None:
        authorized_at = clock.now() if authorized_at is None else authorized_at
        if not math.isfinite(authorized_at) or max_age_seconds <= 0:
            raise LabError("The session authorization deadline is invalid.")
        parsed = urlsplit(connection.server)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise LabError("The Kubernetes connection must be an authenticated HTTPS endpoint.")
        if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise LabError("The Kubernetes endpoint cannot contain a path, query, or fragment.")
        try:
            self._destination = (parsed.hostname, parsed.port or 443)
        except ValueError:
            raise LabError("The Kubernetes endpoint port is invalid.") from None
        if not connection.tls_name or len(connection.token.get_secret_value().encode()) > 16 * 1024:
            raise LabError("The Kubernetes credential or TLS identity is invalid.")
        if not connection.token.get_secret_value():
            raise LabError("The Kubernetes bearer credential is missing.")
        self.cluster_id = cluster_id
        self.tls_name = connection.tls_name
        self.ca_data = connection.ca_data
        self._token: SecretStr | None = connection.token
        self.ssl_context = ssl.create_default_context()
        if self.ca_data:
            try:
                pem = base64.b64decode(self.ca_data, validate=True).decode("ascii")
                self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                self.ssl_context.load_verify_locations(cadata=pem)
            except (ValueError, UnicodeError, ssl.SSLError):
                raise LabError("The embedded Kubernetes CA is invalid.") from None
        self.tools = tools
        self.deadline = authorized_at + max_age_seconds
        self.state = "authorized"
        self.endpoint = ""
        self._relay = Relay(self._permitted, self._confirm_failure)
        self._ssh: SSHForward | None = None
        self._owns_ssh = route is None
        if transport.mode == "ssh":
            if tools.ssh is None or not transport.ssh_target:
                raise LabError(
                    "SSH mode requires a configured target and a verified SSH executable."
                )
            if route is not None and route.target != transport.ssh_target:
                raise LabError("The shared SSH route belongs to another target.")
            self._ssh = route or SSHForward(
                tools.python, tools.ssh, transport.ssh_target, self.deadline
            )
        elif transport.mode != "direct":
            raise LabError("The configured transport mode is unsupported.")
        self._credentials = CredentialService(self, tools.credential)
        self._watcher: asyncio.Task[None] | None = None
        self._transition = asyncio.Lock()
        self._health_lock = asyncio.Lock()
        self.cleanup_error: LabError | None = None

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - clock.now())

    @property
    def token(self) -> SecretStr:
        self.check()
        assert self._token is not None
        return self._token

    def _permitted(self) -> bool:
        return self.state == "connected" and self.remaining > 0

    def check(self) -> None:
        """Reject operations after expiry, disconnection, or incomplete cleanup."""
        if self.cleanup_error is not None:
            raise self.cleanup_error
        if self.remaining <= 0:
            raise LabError("The session has expired. Start a new authorized session.", code=3)
        if self.state != "connected":
            raise LabError("The session is disconnected. Use explicit Reconnect.", code=4)

    async def connect(self) -> None:
        """Establish and health-check a route before enabling grants and forwarding."""
        async with self._transition:
            if self.state == "connected":
                self.check()
                return
            if self.state != "authorized":
                raise LabError("Use explicit Reconnect for a disconnected session.")
            await self._connect()

    async def reconnect(self) -> None:
        """Reconnect using the original credentials and unchanged authorization deadline."""
        async with self._transition:
            if self.state != "disconnected":
                raise LabError("Reconnect is available only for a disconnected session.")
            await self._connect()

    async def _connect(self) -> None:
        if self.remaining <= 0:
            raise LabError("The session has expired. Start a new authorized session.", code=3)
        self.state = "connecting"
        if self._watcher is None:
            self._watcher = asyncio.create_task(self._watch(), name="lab-session-deadline")
        try:
            async with asyncio.timeout(min(20.0, self.remaining)):
                await self._relay.start()
                self.endpoint = f"https://127.0.0.1:{self._relay.port}"
                dial: Dial = functools.partial(asyncio.open_connection, *self._destination)
                if self._ssh is not None:
                    dial = await self._ssh.start(*self._destination)
                await self._probe(dial)
                if self.remaining <= 0:
                    raise LabError("The session expired while connecting.", code=3)
                self._relay.destination = dial
                self.state = "connected"
        except BaseException as error:
            self.state = "disconnected"
            await self._relay.disconnect()
            await self._release_ssh()
            if isinstance(error, asyncio.CancelledError):
                raise
            if isinstance(error, LabError):
                raise
            raise LabError(
                "The Kubernetes transport could not be established and verified."
            ) from None

    async def _probe(self, dial: Dial) -> None:
        """Verify TLS to the configured identity through the route, then read one status."""
        if self._token is None or self.remaining <= 0:
            raise LabError("The session authorization has expired.", code=3)
        async with asyncio.timeout(max(0.001, min(5.0, self.remaining))):
            reader, writer = await dial(ssl=self.ssl_context, server_hostname=self.tls_name)
            try:
                writer.write(
                    f"GET /version HTTP/1.1\r\nHost: {self.tls_name}\r\n"
                    "Connection: close\r\n\r\n".encode()
                )
                await writer.drain()
                status = (await reader.readline()).split()
            finally:
                writer.transport.abort()
        # An authorization failure still proves the verified TLS route is reachable.
        if len(status) < 2 or status[1] not in (b"200", b"401", b"403"):
            raise LabError("The Kubernetes transport health check failed.")

    async def check_health(self) -> None:
        """Confirm route health; disconnect the whole session only on confirmed failure."""
        self.check()
        async with self._health_lock:
            self.check()
            destination = self._relay.destination
            assert destination is not None
            try:
                await self._probe(destination)
            except (OSError, TimeoutError, ValueError, LabError):
                await self.disconnect()
                raise LabError(
                    "The Kubernetes connection was lost. Use explicit Reconnect."
                ) from None

    async def _confirm_failure(self) -> None:
        if self.state == "connected":
            with contextlib.suppress(LabError):
                await self.check_health()

    async def disconnect(self) -> None:
        """Disable every owned path after a transient outage while retaining authorization."""
        async with self._transition:
            if self.state in ("closing", "closed", "disconnected"):
                return
            self.state = "disconnected"
            await self._relay.disconnect()
            await self._release_ssh()

    async def _release_ssh(self) -> None:
        if self._ssh is not None and self._owns_ssh:
            await self._ssh.close()

    async def create_grant(self) -> Grant:
        """Create an independent token-free client configuration and secret capability."""
        self.check()
        return await self._credentials.create_grant()

    async def revoke_grant(self, grant: Grant) -> None:
        """Revoke one client grant without ending the surrounding session."""
        await self._credentials.revoke_grant(grant)

    async def _watch(self) -> None:
        try:
            while self.state not in ("closing", "closed"):
                if self.remaining <= 0:
                    await self.close()
                    return
                if self.state == "connected" and self._ssh is not None and not self._ssh.alive:
                    await self.disconnect()
                await asyncio.sleep(min(0.2, self.remaining))
        except asyncio.CancelledError:
            raise
        except LabError as error:
            self.cleanup_error = error

    async def close(self) -> None:
        """Deny access, close streams and grants, remove owned files, and release credentials."""
        async with self._transition:
            if self.state == "closed":
                if self.cleanup_error:
                    raise self.cleanup_error
                return
            self.state = "closing"
            if self._watcher is not None and self._watcher is not asyncio.current_task():
                self._watcher.cancel()
                await asyncio.gather(self._watcher, return_exceptions=True)
            errors: list[BaseException] = []
            for operation in (
                self._relay.close,
                self._release_ssh,
                self._credentials.close,
            ):
                try:
                    await operation()
                except (OSError, LabError) as error:
                    errors.append(error)
            self._token = None
            self.state = "closed"
            if errors:
                self.cleanup_error = LabError(
                    "Session cleanup could not be fully confirmed.", code=8
                )
                raise self.cleanup_error

    async def __aenter__(self) -> "Session":
        try:
            await self.connect()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
