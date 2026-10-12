import asyncio
import functools
import os
import stat
import sys

import pytest
from test_session import api as api

from lab.clock import now
from lab.errors import LabError
from lab.transport import Relay


async def test_relay_closes_active_stream_and_rejects_cached_clients():
    writers = []

    async def echo(reader, writer):
        writers.append(writer)
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
        writer.close()

    allowed = [True]

    async def failure():
        pytest.fail("Healthy synthetic upstream must not fail")

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    relay = Relay(lambda: allowed[0], failure)
    await relay.start()
    relay.destination = functools.partial(
        asyncio.open_connection, "127.0.0.1", upstream.sockets[0].getsockname()[1]
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
    writer.write(b"hello")
    await writer.drain()
    assert await reader.readexactly(5) == b"hello"
    allowed[0] = False
    await relay.disconnect()
    assert await asyncio.wait_for(reader.read(), 1) == b""
    denied_reader, denied_writer = await asyncio.open_connection("127.0.0.1", relay.port)
    assert await asyncio.wait_for(denied_reader.read(), 1) == b""
    writer.close()
    denied_writer.close()
    await relay.close()
    upstream.close()
    await upstream.wait_closed()
    for upstream_writer in writers:
        upstream_writer.close()


async def test_forwarding_rechecks_deadline_before_resumed_data():
    received = []

    async def sink(reader, writer):
        received.append(await reader.read(100))
        writer.close()

    allowed = [True]

    async def failure():
        pass

    upstream = await asyncio.start_server(sink, "127.0.0.1", 0)
    relay = Relay(lambda: allowed[0], failure)
    await relay.start()
    relay.destination = functools.partial(
        asyncio.open_connection, "127.0.0.1", upstream.sockets[0].getsockname()[1]
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
    await asyncio.sleep(0.02)
    allowed[0] = False
    writer.write(b"must not reach upstream after resume")
    await writer.drain()
    await asyncio.wait_for(reader.read(), 1)
    await asyncio.sleep(0.02)
    assert received == [b""]
    writer.close()
    await relay.close()
    upstream.close()
    await upstream.wait_closed()


async def test_supervisor_parent_eof_kills_owned_process_group(tmp_path):
    fake = tmp_path / "fake-ssh"
    record = tmp_path / "pid"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os,time\n"
        f"open({str(record)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    fake.chmod(0o700)
    read_fd, write_fd = os.pipe()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "lab.supervisor",
        "--parent-fd",
        str(read_fd),
        "--deadline",
        str(now() + 30),
        "--ssh",
        str(fake),
        "--target",
        "synthetic",
        "--host",
        "127.0.0.1",
        "--port",
        "443",
        "--local-socket",
        str(tmp_path / "forward.sock"),
        pass_fds=(read_fd,),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    os.close(read_fd)
    try:
        for _ in range(50):
            if record.exists():
                break
            await asyncio.sleep(0.02)
        assert record.exists()
        child_pid = int(record.read_text())
        os.close(write_fd)
        write_fd = None
        output, error = await asyncio.wait_for(process.communicate(), 3)
        assert process.returncode == 0
        assert not output and not error
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if write_fd is not None:
            os.close(write_fd)
        if process.returncode is None:
            process.terminate()
            await process.wait()


async def test_supervisor_independent_deadline_without_parent_command(tmp_path):
    fake = tmp_path / "fake-ssh"
    record = tmp_path / "pid"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os,time\n"
        f"open({str(record)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    fake.chmod(0o700)
    read_fd, write_fd = os.pipe()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "lab.supervisor",
        "--parent-fd",
        str(read_fd),
        "--deadline",
        str(now() + 0.7),
        "--ssh",
        str(fake),
        "--target",
        "synthetic",
        "--host",
        "127.0.0.1",
        "--port",
        "443",
        "--local-socket",
        str(tmp_path / "forward.sock"),
        pass_fds=(read_fd,),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    os.close(read_fd)
    try:
        output, error = await asyncio.wait_for(process.communicate(), 3)
        assert process.returncode == 0
        assert not output and not error
        with pytest.raises(ProcessLookupError):
            os.kill(int(record.read_text()), 0)
    finally:
        os.close(write_fd)


async def test_forced_parent_termination_does_not_orphan_ssh(tmp_path):
    fake = tmp_path / "fake-ssh"
    record = tmp_path / "pid"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os,time\n"
        f"open({str(record)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    fake.chmod(0o700)
    parent_code = (
        "import os,subprocess,sys,time\n"
        "from lab.clock import now\n"
        "read_fd, write_fd = os.pipe()\n"
        "subprocess.Popen([sys.executable, '-I', '-m', 'lab.supervisor',\n"
        "'--parent-fd', str(read_fd), '--deadline', str(now()+30),\n"
        f"'--ssh', {str(fake)!r}, '--target', 'synthetic', '--host', '127.0.0.1',\n"
        "'--port', '443', '--local-socket', 'unused.sock'], pass_fds=(read_fd,),\n"
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "os.close(read_fd)\n"
        "time.sleep(60)\n"
    )
    parent = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        parent_code,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if record.exists():
                break
            await asyncio.sleep(0.02)
        assert record.exists()
        child_pid = int(record.read_text())
        parent.kill()
        await parent.wait()
        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("The SSH child survived forced parent termination")
    finally:
        if parent.returncode is None:
            parent.kill()
            await parent.wait()


def forwarding_ssh(tmp_path, record, child, failures):
    """Write a fake ssh that records its arguments and forwards like `ssh -L`."""
    fake = tmp_path / "fake-ssh"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import asyncio,json,os,sys\n"
        "from pathlib import Path\n"
        f"record=Path({str(record)!r})\n"
        "attempts=json.loads(record.read_text()) if record.exists() else []\n"
        "attempts.append(sys.argv[1:])\n"
        "record.write_text(json.dumps(attempts))\n"
        f"if len(attempts)<={failures}: sys.exit(255)\n"
        f"Path({str(child)!r}).write_text(str(os.getpid()))\n"
        "local_socket,remote_host,remote_port=sys.argv[sys.argv.index('-L')+1].rsplit(':',2)\n"
        "async def accept(reader,writer):\n"
        "    remote_reader,remote_writer=await asyncio.open_connection(\n"
        "        remote_host,int(remote_port))\n"
        "    async def pump(source,destination):\n"
        "        while data:=await source.read(65536):\n"
        "            destination.write(data)\n"
        "            await destination.drain()\n"
        "    tasks=[asyncio.create_task(pump(reader,remote_writer)),\n"
        "           asyncio.create_task(pump(remote_reader,writer))]\n"
        "    try: await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)\n"
        "    finally:\n"
        "        for task in tasks: task.cancel()\n"
        "        await asyncio.gather(*tasks,return_exceptions=True)\n"
        "        writer.close(); remote_writer.close()\n"
        "async def main():\n"
        "    server=await asyncio.start_unix_server(accept,local_socket)\n"
        "    async with server: await server.serve_forever()\n"
        "asyncio.run(main())\n"
    )
    fake.chmod(0o700)
    return fake


async def test_ssh_forward_uses_a_private_socket_and_stable_reconnect(api, tmp_path):
    import json
    from pathlib import Path
    from types import SimpleNamespace

    import aiohttp

    from lab.session import Session, Tools

    connection, _ = api
    record = tmp_path / "attempts.json"
    child = tmp_path / "child-pid"
    fake = forwarding_ssh(tmp_path, record, child, failures=0)
    session = Session(
        connection,
        SimpleNamespace(mode="ssh", ssh_target="synthetic-target"),
        30,
        Tools(ssh=fake, python=Path(sys.executable), credential=Path("/protected/lab-credential")),
        cluster_id="synthetic-cluster",
    )
    async with session:
        endpoint = session.endpoint
        attempts = json.loads(record.read_text())
        assert len(attempts) == 1
        assert "StreamLocalBindMask=0177" in attempts[-1]
        assert "ControlMaster=no" in attempts[-1]
        assert "ControlPath=none" in attempts[-1]
        assert attempts[-1][-2:] == ["--", "synthetic-target"]
        async with aiohttp.ClientSession() as client:
            stream = await client.ws_connect(
                endpoint + "/stream",
                ssl=session.ssl_context,
                server_hostname=session.tls_name,
            )
            await stream.send_str("owned SSH path")
            assert (await stream.receive()).data == "owned SSH path"
            await session.disconnect()
            assert (await asyncio.wait_for(stream.receive(), 1)).type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
            )
        await session.reconnect()
        assert session.endpoint == endpoint
        await session.check_health()


async def test_shared_ssh_forward_outlives_sessions_and_restarts_after_exit(api, tmp_path):
    import json
    import signal
    from pathlib import Path
    from types import SimpleNamespace

    from lab.session import Session, Tools
    from lab.transport import SSHForward

    connection, _ = api
    record = tmp_path / "attempts.json"
    child = tmp_path / "child-pid"
    fake = forwarding_ssh(tmp_path, record, child, failures=0)
    tools = Tools(
        ssh=fake, python=Path(sys.executable), credential=Path("/protected/lab-credential")
    )
    transport = SimpleNamespace(mode="ssh", ssh_target="synthetic-target")
    route = SSHForward(tools.python, fake, "synthetic-target", now() + 30)

    async def use_route():
        async with Session(
            connection, transport, 30, tools, cluster_id="synthetic-cluster", route=route
        ) as session:
            await session.check_health()

    try:
        await use_route()
        directory = route._directory
        assert directory is not None
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        await use_route()
        assert route._directory == directory
        assert route.alive
        assert len(json.loads(record.read_text())) == 1
        os.kill(int(child.read_text()), signal.SIGTERM)
        for _ in range(50):
            if not route.alive:
                break
            await asyncio.sleep(0.1)
        assert not route.alive
        await use_route()
        assert len(json.loads(record.read_text())) == 2
        with pytest.raises(LabError, match="another target"):
            Session(
                connection,
                SimpleNamespace(mode="ssh", ssh_target="other-target"),
                30,
                tools,
                cluster_id="synthetic-cluster",
                route=route,
            )
    finally:
        await route.close()
    assert not route.alive
    assert not directory.exists()


@pytest.mark.parametrize("signal_name", ["SIGHUP", "SIGKILL"])
async def test_signal_to_owner_process_group_does_not_orphan_shared_ssh(tmp_path, signal_name):
    import signal

    record = tmp_path / "attempts.json"
    child = tmp_path / "child-pid"
    fake = forwarding_ssh(tmp_path, record, child, failures=0)
    owner_code = (
        "import asyncio,sys\n"
        "from pathlib import Path\n"
        "from lab.clock import now\n"
        "from lab.transport import SSHForward\n"
        "async def main():\n"
        f"    route=SSHForward(Path(sys.executable),Path({str(fake)!r}),'synthetic',now()+30)\n"
        "    await route.start('127.0.0.1',9)\n"
        "    await asyncio.sleep(60)\n"
        "asyncio.run(main())\n"
    )
    owner = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", owner_code, start_new_session=True
    )
    try:
        for _ in range(250):
            if child.exists():
                break
            await asyncio.sleep(0.02)
        ssh_pid = int(child.read_text())
        os.killpg(owner.pid, getattr(signal, signal_name))
        await owner.wait()
        for _ in range(150):
            try:
                os.kill(ssh_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
        else:
            os.kill(ssh_pid, signal.SIGKILL)
            pytest.fail(f"SSH survived {signal_name} to its owner's process group")
    finally:
        if owner.returncode is None:
            owner.kill()
            await owner.wait()
