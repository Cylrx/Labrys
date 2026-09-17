"""Owned raw TCP routes with a stable, explicitly enabled loopback listener."""

import asyncio
import os
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path

from lab.errors import LabError


class Relay:
    """Forward bounded client streams to one destination while the session permits access."""

    def __init__(
        self, permitted: Callable[[], bool], failure: Callable[[], Awaitable[None]]
    ) -> None:
        self._permitted = permitted
        self._failure = failure
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self.destination: tuple[str, int] | None = None
        self.port = 0

    async def start(self) -> None:
        if self._server is None:
            self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        if not self._permitted() or self.destination is None or len(self._tasks) >= 128:
            writer.close()
            return
        self._tasks.add(task)
        self._writers.add(writer)
        upstream: asyncio.StreamWriter | None = None
        try:
            try:
                upstream_reader, upstream = await asyncio.wait_for(
                    asyncio.open_connection(*self.destination), 5
                )
            except (OSError, TimeoutError):
                await self._failure()
                return
            assert upstream is not None
            self._writers.add(upstream)
            if not self._permitted():
                return
            pumps = [
                asyncio.create_task(self._pump(reader, upstream)),
                asyncio.create_task(self._pump(upstream_reader, writer)),
            ]
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pump in pumps:
                    pump.cancel()
                results = await asyncio.gather(*pumps, return_exceptions=True)
            if any(isinstance(result, (OSError, TimeoutError)) for result in results):
                await self._failure()
        except (ConnectionError, OSError, TimeoutError):
            # A client stream ending does not establish a transport-wide outage.
            pass
        finally:
            for stream in (writer, upstream):
                if stream is not None:
                    self._writers.discard(stream)
                    stream.transport.abort()
            self._tasks.discard(task)

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while self._permitted():
            data = await reader.read(64 * 1024)
            if not data or not self._permitted():
                return
            writer.write(data)
            await asyncio.wait_for(writer.drain(), 5)

    async def disconnect(self) -> None:
        for writer in tuple(self._writers):
            writer.transport.abort()
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.destination = None

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self.disconnect()


class SSHForward:
    """Own one SSH forwarder through a parent-liveness supervisor."""

    def __init__(self, python: Path, ssh: Path, target: str, deadline: float) -> None:
        self.python = python
        self.ssh = ssh
        self.target = target
        self.deadline = deadline
        self.process: asyncio.subprocess.Process | None = None
        self._liveness: int | None = None
        self.port = 0

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self, host: str, port: int) -> tuple[str, int]:
        for _ in range(3):
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                self.port = reservation.getsockname()[1]
            read_fd, self._liveness = os.pipe()
            os.set_inheritable(self._liveness, False)
            try:
                self.process = await asyncio.create_subprocess_exec(
                    str(self.python),
                    "-I",
                    "-m",
                    "lab.supervisor",
                    "--parent-fd",
                    str(read_fd),
                    "--deadline",
                    str(self.deadline),
                    "--ssh",
                    str(self.ssh),
                    "--target",
                    self.target,
                    "--host",
                    host,
                    "--port",
                    str(port),
                    "--local-port",
                    str(self.port),
                    pass_fds=(read_fd,),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=_ssh_environment(),
                )
            except OSError:
                await self.close()
                raise LabError("The owned SSH transport could not start.") from None
            finally:
                os.close(read_fd)
            # ExitOnForwardFailure resolves actual bind failures; the probe is only readiness.
            for _ in range(50):
                if not self.alive:
                    break
                try:
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", self.port), 0.1
                    )
                    writer.close()
                    await writer.wait_closed()
                    if self.alive:
                        return "127.0.0.1", self.port
                except (OSError, TimeoutError):
                    pass
                await asyncio.sleep(0.1)
            await self.close()
        raise LabError("SSH forwarding failed. Check the configured SSH target and reachability.")

    async def close(self) -> None:
        if self._liveness is not None:
            os.close(self._liveness)
            self._liveness = None
        if self.process is not None:
            try:
                await asyncio.wait_for(self.process.wait(), 4)
            except TimeoutError:
                # The supervisor owns the SSH process group; do not kill it before cleanup.
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 4)
                except TimeoutError:
                    raise LabError("SSH transport cleanup could not be confirmed.") from None
            self.process = None


def _ssh_environment() -> dict[str, str]:
    allowed = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "SSH_AUTH_SOCK", "TMPDIR")
    return {key: os.environ[key] for key in allowed if key in os.environ}
