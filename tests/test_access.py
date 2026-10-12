import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from fixtures import CLUSTER_ID, EXAMPLES, cluster_data, profile, profiles_dir

from lab import access, clock
from lab.config import parse_index
from lab.errors import LabError


@pytest.fixture
async def authorization(tmp_path, monkeypatch):
    instant = [1000.0]
    monkeypatch.setattr(clock, "now", lambda: instant[0])
    index_data = yaml.safe_load((EXAMPLES / "index.yaml").read_text())
    index_data["clusters"].append({"id": "second", "config_ref": "op://vault/second/notesPlain"})
    index_text = yaml.safe_dump(index_data)
    index = parse_index(index_text)
    index_ref = "op://vault/index/notesPlain"
    documents = {index_ref: index_text}
    documents.update({entry.config_ref: yaml.safe_dump(cluster_data()) for entry in index.clusters})
    secrets = SimpleNamespace(read=AsyncMock(side_effect=documents.__getitem__))
    bootstrap = {"index_ref": index_ref, "profiles_dir": str(profiles_dir(tmp_path))}
    owner = access.Authorization(
        secrets, bootstrap, index, instant[0], index.session.max_age_seconds, parent=os.getppid()
    )
    state = SimpleNamespace(
        owner=owner,
        index=index,
        secrets=secrets,
        instant=instant,
        sessions=[],
        phase=None,
        failure=False,
        entered=asyncio.Event(),
        release=asyncio.Event(),
        cleaning=asyncio.Event(),
        cleanup_release=asyncio.Event(),
        slow_cleanup=False,
        cleanup_failure=False,
        expected_owner_error=None,
    )

    class LocalSession:
        def __init__(
            self, connection, transport, max_age, tools, *, cluster_id, authorized_at, route
        ):
            self.cluster_id = cluster_id
            self.route = route
            self.deadline = authorized_at + max_age
            self.path = tmp_path / f"grant-{len(state.sessions)}"
            self.environment = {"LAB_TEST_GRANT": self.path.name}
            self.closed = asyncio.Event()
            state.sessions.append(self)

        async def pause(self, phase):
            if state.phase == phase:
                state.entered.set()
                if state.failure:
                    raise LabError("Synthetic connection failure.", 4)
                await state.release.wait()

        async def connect(self):
            self.path.write_text("synthetic grant")
            await self.pause("connect")

        async def create_grant(self):
            await self.pause("grant")
            return SimpleNamespace(config_path=self.path, environment=self.environment)

        async def close(self):
            state.cleaning.set()
            if state.slow_cleanup:
                await state.cleanup_release.wait()
            self.path.unlink(missing_ok=True)
            self.environment.clear()
            self.closed.set()
            if state.cleanup_failure:
                raise LabError("Synthetic cleanup failure.", 8)

    executable = tmp_path / "kubectl"
    executable.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(0)\n")
    executable.chmod(0o700)
    state.executable = executable
    toolchain = SimpleNamespace(
        require=lambda name: executable,
        ssh=None,
        python=Path(sys.executable),
        credential=tmp_path / "credential",
    )
    monkeypatch.setattr(access, "Session", LocalSession)
    monkeypatch.setattr(access.Toolchain, "load", lambda: toolchain)
    ready = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(owner.serve(ready.set_result))
    state.task = task
    try:
        state.identity = (await asyncio.wait_for(asyncio.shield(ready), 2))["session"]
        yield state
    finally:
        state.release.set()
        state.cleanup_release.set()
        if not task.done():
            owner.stopping.set()
        try:
            await asyncio.wait_for(task, 2)
        except LabError as error:
            if error is not state.expected_owner_error:
                raise


async def lease(identity, cluster=CLUSTER_ID):
    reader, writer = await asyncio.open_unix_connection(access.socket_path(identity))
    writer.write(json.dumps({"operation": "kubectl", "cluster": cluster}).encode() + b"\n")
    await writer.drain()
    return reader, writer


async def response(reader):
    return json.loads(await asyncio.wait_for(reader.readline(), 2))


async def disconnect(writer):
    writer.close()
    await writer.wait_closed()


async def test_discovery_reads_only_index_and_inspection_never_exposes_credentials(authorization):
    state = authorization
    path = access.socket_path(state.identity)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert await access.request(state.identity, "list") == {
        "clusters": [{"id": CLUSTER_ID}, {"id": "second"}]
    }
    state.secrets.read.assert_awaited_once_with(state.owner.index_ref)
    result = await access.request(state.identity, "inspect", CLUSTER_ID)
    assert result == {
        "id": CLUSTER_ID,
        "context": "fictional",
        "server": "https://api.example.invalid",
        "transport": {"mode": "direct", "ssh_target": None},
        "profile": profile().model_dump(mode="json"),
    }
    assert "FICTIONAL_NOT_A_CREDENTIAL" not in json.dumps(result)
    assert not state.sessions
    assert all("KEY_ITEM_ID" not in call.args[0] for call in state.secrets.read.await_args_list)


async def test_missing_profile_does_not_block_fresh_connections_to_multiple_clusters(authorization):
    state = authorization
    result = await access.request(state.identity, "inspect", "second")
    assert result["profile"] is None
    assert "not found" in result["profile_error"]
    original_deadline = state.owner.deadline
    for cluster in (CLUSTER_ID, "second", CLUSTER_ID):
        state.instant[0] += 10
        reader, writer = await lease(state.identity, cluster)
        grant = (await response(reader))["data"]
        session = state.sessions[-1]
        assert session.cluster_id == cluster
        assert session.deadline == original_deadline
        assert Path(grant["kubeconfig"]).exists()
        await disconnect(writer)
        await asyncio.wait_for(session.closed.wait(), 2)
        assert not Path(grant["kubeconfig"]).exists()
        assert not session.environment
    assert len(state.sessions) == 3
    assert state.owner.secrets is state.secrets
    assert state.secrets.deadline == original_deadline


async def test_requested_duration_and_clusters_bound_the_authorization(authorization):
    state = authorization
    owner = access.Authorization(
        SimpleNamespace(),
        {"index_ref": "", "profiles_dir": ""},
        state.index,
        0,
        60,
        parent=os.getppid(),
    )
    assert owner.deadline == 60
    with pytest.raises(LabError, match="not registered: missing"):
        access.Authorization(
            SimpleNamespace(), {}, state.index, 0, 60, ("missing",), parent=os.getppid()
        )
    state.owner.clusters = ("second",)
    assert await access.request(state.identity, "list") == {"clusters": [{"id": "second"}]}
    with pytest.raises(LabError, match="not available in this session"):
        await access.request(state.identity, "inspect", CLUSTER_ID)


async def test_ssh_sessions_share_one_forwarder_per_cluster_until_shutdown(
    authorization, monkeypatch
):
    state = authorization
    forwarders = []

    class Forwarder:
        def __init__(self, python, ssh, target, deadline):
            self.target = target
            self.deadline = deadline
            self.closed = 0
            forwarders.append(self)

        async def close(self):
            self.closed += 1

    monkeypatch.setattr(access, "SSHForward", Forwarder)
    access.Toolchain.load().ssh = Path("/protected/ssh")
    routed = cluster_data()
    routed["transport"] = {"mode": "ssh", "ssh_target": "synthetic-target"}
    index_document = state.secrets.read.side_effect
    state.secrets.read.side_effect = lambda ref: (
        index_document(ref) if ref == state.owner.index_ref else yaml.safe_dump(routed)
    )
    for cluster in (CLUSTER_ID, CLUSTER_ID, "second"):
        reader, writer = await lease(state.identity, cluster)
        assert "data" in await response(reader)
        await disconnect(writer)
    first, again, other = (session.route for session in state.sessions)
    assert first is again and first is not other
    assert len(forwarders) == 2
    assert {forwarder.deadline for forwarder in forwarders} == {state.owner.deadline}
    assert [forwarder.closed for forwarder in forwarders] == [0, 0]
    state.owner.stopping.set()
    await asyncio.wait_for(state.task, 2)
    assert [forwarder.closed for forwarder in forwarders] == [1, 1]


def test_authorization_refuses_a_requester_that_exited_before_approval(authorization):
    state = authorization
    with pytest.raises(LabError, match="requesting process exited"):
        access.Authorization(
            SimpleNamespace(),
            {"index_ref": "", "profiles_dir": ""},
            state.index,
            0,
            60,
            parent=os.getppid() + 1,
        )


async def test_authorization_ends_when_its_requesting_process_exits(authorization, monkeypatch):
    state = authorization
    monkeypatch.setattr(access.os, "getppid", lambda: state.owner.parent + 1)
    await asyncio.wait_for(state.task, 2)
    assert state.owner.reason == "requesting process exited"


@pytest.mark.parametrize("ending", ["close", "expiry"])
async def test_shutdown_waits_for_grant_cleanup_and_disconnects_clients(authorization, ending):
    state = authorization
    reader, writer = await lease(state.identity)
    grant = (await response(reader))["data"]
    state.slow_cleanup = True
    closer = None
    try:
        if ending == "close":
            closer = asyncio.create_task(access.request(state.identity, "close"))
        else:
            state.instant[0] = state.owner.deadline
        await asyncio.wait_for(state.cleaning.wait(), 2)
        assert Path(grant["kubeconfig"]).exists()
        assert not state.task.done()
        if closer is not None:
            assert not closer.done()
        state.cleanup_release.set()
        if closer is not None:
            assert await asyncio.wait_for(closer, 2) == {"status": "Closed"}
        await asyncio.wait_for(state.task, 2)
        assert await asyncio.wait_for(reader.read(), 2) == b""
        assert not Path(grant["kubeconfig"]).exists()
        assert state.owner.secrets is None
        assert not access.socket_path(state.identity).parent.exists()
        with pytest.raises(LabError, match="unavailable"):
            await access.request(state.identity, "list")
    finally:
        state.cleanup_release.set()
        await disconnect(writer)
        if closer is not None:
            await closer


@pytest.mark.parametrize("phase", ["connect", "grant"])
@pytest.mark.parametrize("failure", [False, True], ids=["cancelled", "failed"])
async def test_partial_connection_cleanup(authorization, phase, failure):
    state = authorization
    state.phase, state.failure = phase, failure
    reader, writer = await lease(state.identity)
    try:
        await asyncio.wait_for(state.entered.wait(), 2)
        if failure:
            assert (await response(reader))["code"] == 4
        else:
            assert await access.request(state.identity, "close") == {"status": "Closed"}
            assert await asyncio.wait_for(reader.read(), 2) == b""
        session = state.sessions[0]
        await asyncio.wait_for(session.closed.wait(), 2)
        assert not session.path.exists()
        assert not session.environment
    finally:
        await disconnect(writer)


async def test_native_kubectl_preserves_argv_exit_status_and_stdio(
    authorization, tmp_path, capfd, monkeypatch
):
    state = authorization
    providers = {
        "AWS_PROFILE": "synthetic-profile",
        "GOOGLE_APPLICATION_CREDENTIALS": "/synthetic/cloud-account.json",
        "HTTPS_PROXY": "http://proxy.example.invalid",
    }
    for name, value in providers.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KUBECONFIG", "/synthetic/ambient-config")
    monkeypatch.setenv("LAB_TEST_GRANT", "old-grant")
    state.executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"providers = {list(providers)!r}\n"
        "print(json.dumps({'argv': sys.argv[1:], 'input': sys.stdin.read(), "
        "'config': os.environ['KUBECONFIG'], 'grant': os.environ['LAB_TEST_GRANT'], "
        "'providers': {name: os.environ.get(name) for name in providers}}))\n"
        "print('synthetic stderr', file=sys.stderr)\n"
        "sys.exit(17)\n"
    )
    input_file = tmp_path / "stdin"
    input_file.write_text("piped input\n")
    saved_stdin = os.dup(0)
    arguments = ["exec", "-it", "pod/example", "--", "sh", "-c", "printf '%s' '$HOME'"]
    try:
        with input_file.open() as stream:
            os.dup2(stream.fileno(), 0)
            assert await access.kubectl(state.identity, CLUSTER_ID, arguments) == 17
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
    output = capfd.readouterr()
    result = json.loads(output.out)
    assert result["argv"] == arguments
    assert result["input"] == "piped input\n"
    assert result["providers"] == providers
    assert result["grant"] == "grant-0"
    assert result["config"] != "/synthetic/ambient-config"
    assert output.err == "synthetic stderr\n"
    await asyncio.wait_for(state.sessions[0].closed.wait(), 2)
    assert not Path(result["config"]).exists()


async def test_closing_authorization_stops_a_running_native_client(authorization, tmp_path):
    state = authorization
    started = tmp_path / "client-pid"
    state.executable.write_text(
        f"#!{sys.executable}\n"
        "import os, signal\n"
        f"with open({str(started)!r}, 'w') as stream:\n"
        "    stream.write(str(os.getpid()))\n"
        "signal.pause()\n"
    )
    client = asyncio.create_task(access.kubectl(state.identity, CLUSTER_ID, ["logs", "-f", "pod"]))
    try:
        async with asyncio.timeout(2):
            while not started.exists() or not started.stat().st_size:
                await asyncio.sleep(0.01)
        pid = int(started.read_text())
        assert await access.request(state.identity, "close") == {"status": "Closed"}
        with pytest.raises(LabError, match="disconnected"):
            await asyncio.wait_for(client, 2)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert state.sessions[0].closed.is_set()
    finally:
        client.cancel()
        await asyncio.gather(client, return_exceptions=True)


async def test_native_kubectl_keeps_one_interactive_shell_state(authorization, tmp_path):
    from test_auth_pty import Terminal

    state = authorization
    state.executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert '-it' in sys.argv\n"
        "assert all(os.isatty(fd) for fd in (0, 1, 2))\n"
        "print('PTY_READY', flush=True)\n"
        "os.execv('/bin/sh', ['/bin/sh', '-i'])\n"
    )
    terminal = Terminal(
        "import asyncio\nfrom lab.access import kubectl\n"
        f"code = asyncio.run(kubectl({state.identity!r}, {CLUSTER_ID!r}, "
        "['exec', '-it', 'pod/example', '--', 'sh']))\n"
        "print(f'KUBECTL_EXIT_{code}', flush=True)\n"
    )
    try:
        await asyncio.to_thread(terminal.until, "PTY_READY")
        terminal.send(f"cd '{tmp_path}'\nstored=kept\nprintf 'FIRST_%s\\n' READY\n")
        await asyncio.to_thread(terminal.until, "FIRST_READY")
        terminal.send('printf \'STATE_%s:%s\\n\' "$stored" "$PWD"\n')
        await asyncio.to_thread(terminal.until, f"STATE_kept:{tmp_path}")
        terminal.send("exit\n")
        await asyncio.to_thread(terminal.until, "KUBECTL_EXIT_0")
        await asyncio.wait_for(state.sessions[0].closed.wait(), 2)
        assert not state.sessions[0].path.exists()
    finally:
        await asyncio.to_thread(terminal.close)


async def test_cleanup_failure_is_reported_to_closer_and_authorization_owner(authorization):
    state = authorization
    reader, writer = await lease(state.identity)
    await response(reader)
    state.cleanup_failure = True
    try:
        with pytest.raises(LabError, match="cleanup") as client_error:
            await access.request(state.identity, "close")
        assert client_error.value.code == 8
        with pytest.raises(LabError, match="cleanup") as owner_error:
            await asyncio.wait_for(state.task, 2)
        assert owner_error.value.code == 8
        state.expected_owner_error = owner_error.value
        assert state.owner.secrets is None
        assert not access.socket_path(state.identity).exists()
        with pytest.raises(LabError, match="unavailable"):
            await access.request(state.identity, "list")
    finally:
        await disconnect(writer)


async def test_explicit_close_waits_for_cleanup_already_started_by_client_eof(authorization):
    state = authorization
    reader, writer = await lease(state.identity)
    grant = (await response(reader))["data"]
    state.slow_cleanup = True
    await disconnect(writer)
    await asyncio.wait_for(state.cleaning.wait(), 2)
    closer = asyncio.create_task(access.request(state.identity, "close"))
    try:
        await asyncio.wait_for(state.owner.stopping.wait(), 2)
        await asyncio.sleep(0.05)
        assert not closer.done()
        assert not state.task.done()
        assert Path(grant["kubeconfig"]).exists()
        state.cleanup_release.set()
        assert await asyncio.wait_for(closer, 2) == {"status": "Closed"}
        await asyncio.wait_for(state.task, 2)
        assert state.sessions[0].closed.is_set()
        assert not Path(grant["kubeconfig"]).exists()
    finally:
        state.cleanup_release.set()
        await closer
