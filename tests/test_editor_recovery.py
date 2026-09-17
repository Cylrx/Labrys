"""Recovery coordination uses synthetic API objects and a retained protocol subprocess."""

import asyncio
import copy
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lab import cli
from lab import editor_recovery as recovery
from lab.errors import LabError
from lab.remote import editor_recovery as remote

COMMIT = "1" * 40
PROFILE = recovery.Profile("fixture@sha256:" + "a" * 64, "x86_64", "x64", COMMIT, "/usr/bin/sleep")
NOTEBOOK = {"metadata": {"name": "fixture", "namespace": "research", "uid": "notebook-uid"}}
PARENT = {
    "metadata": {
        "name": "fixture",
        "uid": "parent-uid",
        "ownerReferences": [
            {
                "controller": True,
                "uid": "notebook-uid",
                "name": "fixture",
                "kind": "Notebook",
                "apiVersion": "kubeflow.org/v1",
            }
        ],
    }
}
POD = {
    "metadata": {
        "name": "fixture-0",
        "uid": "pod-uid",
        "ownerReferences": [
            {
                "controller": True,
                "uid": "parent-uid",
                "name": "fixture",
                "kind": "StatefulSet",
                "apiVersion": "apps/v1",
            }
        ],
    },
    "spec": {"containers": [{"name": "notebook", "command": ["sleep", "infinity"]}]},
    "status": {
        "conditions": [{"type": "Ready", "status": "True"}],
        "containerStatuses": [
            {
                "name": "notebook",
                "containerID": "containerd://fixture",
                "imageID": PROFILE.image_id,
                "restartCount": 0,
                "state": {"running": {"startedAt": "fixture"}},
            }
        ],
    },
}


@pytest.fixture
def environment(monkeypatch, tmp_path):
    pod, parent = copy.deepcopy(POD), copy.deepcopy(PARENT)
    grant = SimpleNamespace(config_path=tmp_path / "grant", environment={})
    session = SimpleNamespace(
        check=lambda: None,
        remaining=60,
        create_grant=AsyncMock(return_value=grant),
        revoke_grant=AsyncMock(),
        state="ready",
    )

    async def collection(path, **kwargs):
        return [parent] if "statefulsets" in path else [pod]

    api = SimpleNamespace(session=session, collection=collection)
    service = SimpleNamespace(
        api=api,
        get=AsyncMock(return_value=NOTEBOOK),
        cluster=SimpleNamespace(cluster_id="cluster-fixture"),
    )
    tools = SimpleNamespace(require=lambda name: Path("/fixture") / name)
    return SimpleNamespace(**locals())


async def test_unqualified_profiles_never_start_helper(environment):
    result = await recovery.restart(environment.service, NOTEBOOK, environment.tools, AsyncMock())
    assert result.status == "Unsupported"
    assert "image digest" in result.data()["message"]
    environment.session.create_grant.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    ["hostpid", "shared", "probe", "supervisor", "owner", "deleting", "privileged", "server_mount"],
)
async def test_unsafe_lifecycle_or_identity_refused(environment, mutation):
    pod = environment.pod
    if mutation == "hostpid":
        pod["spec"]["hostPID"] = True
    elif mutation == "shared":
        pod["spec"]["shareProcessNamespace"] = True
    elif mutation == "probe":
        pod["spec"]["containers"][0]["livenessProbe"] = {"exec": {"command": ["check"]}}
    elif mutation == "supervisor":
        pod["spec"]["containers"][0]["command"] = ["sh", "-c", "node server; wait"]
    elif mutation == "owner":
        pod["metadata"]["ownerReferences"][0]["kind"] = "WrongKind"
    elif mutation == "deleting":
        pod["metadata"]["deletionTimestamp"] = "now"
    elif mutation == "privileged":
        pod["spec"]["containers"][0]["securityContext"] = {"privileged": True}
    else:
        pod["spec"]["containers"][0]["volumeMounts"] = [{"name": "shared", "mountPath": "/root"}]
    with pytest.raises(LabError) as error:
        await recovery.snapshot(environment.service, NOTEBOOK)
    assert error.value.data["outcome"] == "Refused"


async def test_qualified_weka_data_mount_and_unqualified_host_mount(environment, monkeypatch):
    spec = environment.pod["spec"]
    spec["volumes"] = [{"name": "data", "hostPath": {"path": "/mnt/weka"}}]
    spec["containers"][0]["volumeMounts"] = [{"name": "data", "mountPath": "/mnt/weka"}]
    monkeypatch.setattr(
        recovery,
        "QUALIFIED_PROFILES",
        (replace(PROFILE, data_mounts=(("/mnt/weka", "/mnt/weka"),)),),
    )
    await recovery.snapshot(environment.service, NOTEBOOK)
    spec["volumes"][0]["hostPath"]["path"] = "/var/run"
    with pytest.raises(LabError):
        await recovery.snapshot(environment.service, NOTEBOOK)


@pytest.mark.parametrize(
    "version", ["1.137.0", "1.137.0\ninvalid\nx64", "1.137.0\n" + "2" * 40 + "\nx64"]
)
async def test_full_build_commit_required(environment, monkeypatch, version):
    monkeypatch.setattr(recovery, "QUALIFIED_PROFILES", (PROFILE,))
    monkeypatch.setattr(
        recovery,
        "_output",
        AsyncMock(side_effect=[version, "ms-vscode-remote.remote-containers@0.469.0"]),
    )
    selected = await recovery.snapshot(environment.service, NOTEBOOK)
    with pytest.raises(LabError) as error:
        await recovery.qualify(environment.tools, selected)
    assert error.value.data["outcome"] == "Unsupported"


async def fake_helper(monkeypatch, status="OriginalProcessExited", *, lost=False):
    create = asyncio.create_subprocess_exec
    source = (
        "import json,sys\nx=json.loads(input())\n"
        "print(json.dumps({'version':1,'operation':x['operation'],'status':'AwaitingConfirmation',"
        "'pid':42,'start':99}),flush=True)\n"
        "line=sys.stdin.readline()\n"
        "if line:\n"
        " p=json.loads(line)\n"
        " assert p=={'version':1,'operation':x['operation'],'action':'proceed'}\n"
        + (
            " sys.exit(0)\n"
            if lost
            else (
                " print(json.dumps({'version':1,'operation':x['operation'],"
                f"'status':{status!r}}}),flush=True)\n"
            )
        )
    )
    calls = []

    async def launch(*args, **kwargs):
        calls.append(args)
        return await create(
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            source,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    monkeypatch.setattr(recovery.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(recovery, "qualify", AsyncMock(return_value=PROFILE))
    return calls


async def test_retained_helper_single_proceed_scoped_cleanup(environment, monkeypatch):
    calls = await fake_helper(monkeypatch)
    result = await recovery.restart(
        environment.service, NOTEBOOK, environment.tools, AsyncMock(return_value=True)
    )
    assert result.status == "OriginalProcessExited"
    assert len(calls) == 1
    assert "-i" in calls[0] and "-it" not in calls[0]
    assert calls[0][calls[0].index("--") + 1 :][:4] == ("/usr/bin/python3", "-I", "-S", "-B")
    environment.session.revoke_grant.assert_awaited_once_with(environment.grant)
    assert environment.session.state == "ready"
    assert not environment.session.editor_recoveries


@pytest.mark.parametrize("change", ["pod", "container", "restart", "cancel", "expiry"])
async def test_confirmation_revalidates_incarnation_and_authorization(
    environment, monkeypatch, change
):
    calls = await fake_helper(monkeypatch)

    async def confirm(*args):
        if change == "pod":
            environment.pod["metadata"]["uid"] = "replacement"
        elif change == "container":
            environment.pod["status"]["containerStatuses"][0]["containerID"] = "replacement"
        elif change == "restart":
            environment.pod["status"]["containerStatuses"][0]["restartCount"] = 1
        elif change == "expiry":
            environment.session.check = lambda: (_ for _ in ()).throw(LabError("Expired", 3))
        else:
            return False
        return True

    result = await recovery.restart(environment.service, NOTEBOOK, environment.tools, confirm)
    assert result.status == (
        "Cancelled"
        if change == "cancel"
        else "AuthorizationUnavailable"
        if change == "expiry"
        else "Refused"
    )
    assert len(calls) == 1
    environment.session.revoke_grant.assert_awaited_once()


async def test_lost_transport_after_proceed_never_retries(environment, monkeypatch):
    calls = await fake_helper(monkeypatch, lost=True)
    result = await recovery.restart(
        environment.service, NOTEBOOK, environment.tools, AsyncMock(return_value=True)
    )
    assert result.status == "OutcomeUnknown"
    assert len(calls) == 1


async def test_cleanup_failure_preserves_known_outcome(environment, monkeypatch):
    await fake_helper(monkeypatch)
    environment.session.revoke_grant.side_effect = LabError("synthetic cleanup failure")
    result = await recovery.restart(
        environment.service, NOTEBOOK, environment.tools, AsyncMock(return_value=True)
    )
    assert result.status == "OriginalProcessExited"
    assert result.warning


async def test_duplicate_submission_does_not_start_second_helper(environment, monkeypatch):
    calls = await fake_helper(monkeypatch)
    seen = []

    async def confirm(*args):
        with pytest.raises(LabError, match="already active"):
            await recovery.restart(
                environment.service,
                NOTEBOOK,
                environment.tools,
                AsyncMock(),
                "fixture-0",
                "notebook",
            )
        seen.append(True)
        return False

    assert (
        await recovery.restart(environment.service, NOTEBOOK, environment.tools, confirm)
    ).status == "Cancelled"
    assert seen and len(calls) == 1


def test_prefix_does_not_read_trailing_secret(tmp_path):
    path = tmp_path / "cmdline"
    path.write_bytes(b"node\0server-main.js\0synthetic-private-server-token\0")
    assert remote.prefix(path, 2) == ["node", "server-main.js"]


@pytest.mark.parametrize("already", [True, False])
def test_pidfd_send_is_at_most_once_with_zero_flags(monkeypatch, already):
    sends = []
    monkeypatch.setattr(remote, "exited", lambda fd: already)
    monkeypatch.setattr(remote, "identity", lambda pid, profile: 99)
    monkeypatch.setattr(
        remote.signal, "pidfd_send_signal", lambda *args: sends.append(args), raising=False
    )
    monkeypatch.setattr(
        remote.select,
        "poll",
        lambda: SimpleNamespace(register=lambda *args: None, poll=lambda timeout: [1]),
        raising=False,
    )
    assert remote.stop(5, 42, 99, {}) == ("AlreadyExited" if already else "OriginalProcessExited")
    assert sends == ([] if already else [(5, remote.signal.SIGTERM, None, 0)])


async def test_standalone_stop_does_not_open_editor(environment, monkeypatch):
    monkeypatch.setattr(
        recovery, "restart", AsyncMock(return_value=recovery.Result("OriginalProcessExited"))
    )
    monkeypatch.setattr(cli, "open_editor", AsyncMock(side_effect=AssertionError("opened editor")))
    application = cli.Application(environment.service, environment.tools)
    args = cli.parser().parse_args(
        [
            "notebook",
            "editor-restart",
            "fixture",
            "--namespace",
            "research",
            "--cluster",
            "cluster-fixture",
            "--json",
            "--yes",
        ]
    )
    await application.dispatch(args)
    assert application.result["status"] == "OriginalProcessExited"
    cli.open_editor.assert_not_called()


async def test_cancel_after_proceed_reports_unknown(environment, monkeypatch):
    calls = await fake_helper(monkeypatch)
    original = recovery.read_frame
    reads = 0

    async def interrupt(process, operation, deadline):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise asyncio.CancelledError
        return await original(process, operation, deadline)

    monkeypatch.setattr(recovery, "read_frame", interrupt)
    with pytest.raises(LabError) as error:
        await recovery.restart(
            environment.service, NOTEBOOK, environment.tools, AsyncMock(return_value=True)
        )
    assert error.value.code == 5
    assert error.value.data["outcome"] == "OutcomeUnknown"
    assert len(calls) == 1
    environment.session.revoke_grant.assert_awaited_once()


async def test_reopen_revalidates_target(environment):
    selected = await recovery.snapshot(environment.service, NOTEBOOK)
    environment.pod["metadata"]["uid"] = "new-pod"
    with pytest.raises(LabError, match="Target changed"):
        await recovery.revalidate(environment.service, NOTEBOOK, selected)


@pytest.mark.parametrize("stage", ["client", "grant"])
async def test_cancellation_during_cleanup_preserves_known_result(environment, monkeypatch, stage):
    await fake_helper(monkeypatch)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = recovery.stop_client if stage == "client" else environment.session.revoke_grant

    async def delayed(value):
        entered.set()
        await finish.wait()
        await original(value)

    if stage == "client":
        monkeypatch.setattr(recovery, "stop_client", delayed)
    else:
        environment.session.revoke_grant = AsyncMock(side_effect=delayed)
    task = asyncio.create_task(
        recovery.restart(
            environment.service, NOTEBOOK, environment.tools, AsyncMock(return_value=True)
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    finish.set()
    with pytest.raises(LabError) as error:
        await asyncio.wait_for(task, 3)
    assert error.value.data["outcome"] == "OriginalProcessExited"
    assert error.value.code == 0
    assert not environment.session.editor_recoveries
    environment.session.revoke_grant.assert_awaited_once()


@pytest.mark.parametrize("stage", ["client", "grant"])
async def test_cleanup_operation_cancellation_keeps_result_and_releases_guard(
    environment, monkeypatch, stage
):
    await fake_helper(monkeypatch)
    if stage == "client":
        original = recovery.stop_client

        async def cancelled_client(process):
            await original(process)
            raise asyncio.CancelledError

        monkeypatch.setattr(recovery, "stop_client", cancelled_client)
    else:
        environment.session.revoke_grant.side_effect = asyncio.CancelledError
    result = await recovery.restart(
        environment.service, NOTEBOOK, environment.tools, AsyncMock(return_value=True)
    )
    assert result.status == "OriginalProcessExited"
    assert result.warning
    assert not environment.session.editor_recoveries
    environment.session.revoke_grant.assert_awaited_once()


async def test_native_work_phases_surround_confirmation_without_nesting(environment, monkeypatch):
    await fake_helper(monkeypatch)
    phases = []
    in_work = False

    async def work(awaitable, title):
        nonlocal in_work
        assert not in_work
        in_work = True
        phases.append(title)
        try:
            return await awaitable
        finally:
            in_work = False

    async def confirm(*args):
        assert not in_work
        phases.append("Confirmation")
        return True

    result = await recovery.restart(
        environment.service, NOTEBOOK, environment.tools, confirm, work=work
    )
    assert result.status == "OriginalProcessExited"
    assert phases == [
        "Checking Notebook identity and lifecycle",
        "Checking editor recovery support",
        "Inspecting the original VS Code Server process",
        "Confirmation",
        "Revalidating the selected container",
        "Waiting for the original process to exit",
    ]


@pytest.mark.parametrize("authorized_wrapper", [False, True])
async def test_authorization_deadline_during_cleanup_preserves_dispatch_result(
    environment, monkeypatch, authorized_wrapper
):
    await fake_helper(monkeypatch)
    original = recovery.stop_client

    async def delayed(process):
        await asyncio.sleep(0.4)
        await original(process)

    monkeypatch.setattr(recovery, "stop_client", delayed)
    application = cli.Application(environment.service, environment.tools)
    args = cli.parser().parse_args(
        [
            "notebook",
            "editor-restart",
            "fixture",
            "--namespace",
            "research",
            "--cluster",
            "cluster-fixture",
            "--json",
            "--yes",
        ]
    )

    async def dispatched():
        await application.dispatch(args)
        return application.result

    operation = dispatched
    if authorized_wrapper:
        environment.tools.python = Path(sys.executable)
        environment.tools.ssh = None
        environment.tools.credential = Path("/fixture/credential")
        environment.service.cluster.connection = None
        environment.service.cluster.transport = None
        environment.session.connect = AsyncMock()
        environment.session.close = AsyncMock()
        monkeypatch.setattr(cli, "parse_cluster", lambda *args: environment.service.cluster)
        monkeypatch.setattr(cli, "data_key", lambda value: b"fixture")
        monkeypatch.setattr(cli, "Repository", lambda *args: SimpleNamespace(verify=lambda: None))
        monkeypatch.setattr(cli.Toolchain, "load", lambda: environment.tools)
        monkeypatch.setattr(cli, "Session", lambda *args, **kwargs: environment.session)
        monkeypatch.setattr(cli, "Kubernetes", lambda session: environment.service.api)
        monkeypatch.setattr(cli, "Notebooks", lambda *args: environment.service)
        index = SimpleNamespace(
            clusters=[SimpleNamespace(id="cluster-fixture", config_ref="fixture")],
            data_key_ref="fixture",
            session=SimpleNamespace(max_age_seconds=600),
        )
        secrets = SimpleNamespace(read=AsyncMock(return_value="fixture"))

        async def authorized():
            return await cli.run_authorized(
                args, index, cli.clock.now(), secrets, None, Path("/profiles")
            )

        operation = authorized
    result = await cli.until_deadline(cli.clock.now() + 0.05, operation())
    assert result["status"] == "OriginalProcessExited"
    assert result["data"]["outcome"] == "OriginalProcessExited"
    assert not environment.session.editor_recoveries
    environment.session.revoke_grant.assert_awaited_once()
    if authorized_wrapper:
        environment.session.close.assert_awaited_once()


async def test_native_work_cancel_before_proceed_is_cancelled(environment):
    async def work(awaitable, title):
        awaitable.close()
        raise LabError("Cancelled.", 130)

    result = await recovery.restart(
        environment.service, NOTEBOOK, environment.tools, AsyncMock(), work=work
    )
    assert result.status == "Cancelled"
    environment.session.create_grant.assert_not_called()
    assert not environment.session.editor_recoveries


async def test_native_deadline_unwinds_without_opening_another_page(environment, monkeypatch):
    from lab.interface import Interactive

    await fake_helper(monkeypatch)
    original = recovery.stop_client

    async def delayed(process):
        await asyncio.sleep(0.4)
        await original(process)

    monkeypatch.setattr(recovery, "stop_client", delayed)

    async def work(awaitable, *args, **kwargs):
        return await awaitable

    screens = SimpleNamespace(
        menu=AsyncMock(return_value="notebook editor-restart fixture --namespace research --yes"),
        work=work,
        details=AsyncMock(side_effect=AssertionError("Opened a page after cancellation")),
    )
    native = Interactive(environment.service, environment.tools, screens)
    native.target = AsyncMock(return_value=("fixture-0", "notebook"))
    with pytest.raises(LabError) as error:
        await cli.until_deadline(cli.clock.now() + 0.05, native._run(False))
    assert error.value.data["outcome"] == "OriginalProcessExited"
    assert error.value.code == 0
    screens.details.assert_not_called()
    screens.menu.assert_awaited_once()
    assert not environment.session.editor_recoveries


async def test_recovery_refuses_notebook_with_stop_requested(environment):
    fresh = copy.deepcopy(NOTEBOOK)
    fresh["metadata"]["annotations"] = {"kubeflow-resource-stopped": "now"}
    environment.service.get.return_value = fresh
    result = await recovery.restart(environment.service, NOTEBOOK, environment.tools, AsyncMock())
    assert result.status == "Refused"
    assert "stopping or stopped" in result.data()["message"]
    environment.session.create_grant.assert_not_called()
