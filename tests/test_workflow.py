"""Synthetic end-to-end contracts across services and durable local state."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from cryptography.fernet import Fernet
from fixtures import CLUSTER_ID, EXAMPLES, cluster, inputs, profile_data, profiles_dir
from pydantic import SecretStr

from lab import cli, setup
from lab.errors import LabError
from lab.history import Repository
from lab.kubernetes import ApiError, Kubernetes
from lab.notebooks import Notebooks
from lab.storage import Paths, read_bootstrap, write_bootstrap


class FakeAPI:
    def __init__(self, repository):
        self.repository = repository
        self.session = SimpleNamespace(check=lambda: None)
        self.notebook = None
        self.posts = []
        self.next_error = None
        self.accept_before_error = False
        self.pod_state = "Ready"

    async def request(self, method, path, *, body=None):
        if method == "GET":
            if self.notebook is None:
                raise ApiError(404, "GET")
            return deepcopy(self.notebook)
        assert method == "POST"
        operation_id = body["metadata"]["labels"]["lab.operations/id"]
        saved = self.repository.history.read()["operations"][operation_id]
        assert saved["state"] == "Sending"
        assert saved["manifest"] == body
        self.posts.append(deepcopy(body))
        if self.next_error is None or self.accept_before_error:
            self.notebook = deepcopy(body)
            self.notebook["metadata"].update(uid="synthetic-server-uid", resourceVersion="1")
        if self.next_error is not None:
            error, self.next_error = self.next_error, None
            raise error
        return deepcopy(self.notebook)

    async def collection(self, path, **params):
        if path.endswith("/statefulsets"):
            return []
        if path.endswith("/notebooks"):
            return [deepcopy(self.notebook)] if self.notebook else []
        assert path.endswith("/pods")
        if self.notebook is None:
            return []
        return [
            {
                "metadata": {
                    "name": "owned-pod",
                    "namespace": "research",
                    "ownerReferences": [{"uid": self.notebook["metadata"]["uid"]}],
                },
                "spec": {"containers": [{"name": "main", "image": "example/image:v1"}]},
                "status": {
                    "phase": "Running" if self.pod_state == "Ready" else self.pod_state,
                    "conditions": [{"type": "Ready", "status": "True"}]
                    if self.pod_state == "Ready"
                    else [],
                },
            }
        ]


@pytest.fixture
def workflow(tmp_path):
    paths = Paths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    key = Fernet.generate_key()
    repository = Repository(paths, key)
    api = FakeAPI(repository)
    service = Notebooks(api, cluster(), repository, profiles_dir(tmp_path))
    return SimpleNamespace(paths=paths, key=key, repository=repository, api=api, service=service)


async def test_create_accept_ready_and_explicit_preset(workflow, monkeypatch):
    receipt = workflow.service.prepare(inputs(), workflow.service.profile())
    assert workflow.repository.candidates(CLUSTER_ID, "research", "image") == []
    notebook = await workflow.service.submit(receipt)
    assert workflow.repository.operation(receipt["operation_id"])["state"] == "Accepted"
    assert workflow.repository.candidates(CLUSTER_ID, "research", "image") == [inputs().image]
    assert workflow.repository.presets(CLUSTER_ID, "research") == []
    await workflow.service.wait(receipt, notebook, timeout=0.1)
    assert workflow.repository.operation(receipt["operation_id"])["state"] == "Ready"
    from lab.interface import Interactive

    screens = SimpleNamespace(
        choose=AsyncMock(return_value="new"),
        form=AsyncMock(return_value={"name": "Research template"}),
        details=AsyncMock(return_value="back"),
    )
    app = Interactive(workflow.service, None, screens)
    receipt = workflow.service.receipt(receipt["operation_id"])
    await app.save_preset(receipt)
    preset = workflow.repository.presets(CLUSTER_ID, "research")[0]
    assert preset["editable_fields"]["cpu"] == "2000m"
    assert "name" not in preset["editable_fields"]
    assert "namespace" not in preset["editable_fields"]
    await app.save_preset(receipt)
    assert screens.choose.await_count == 1


@pytest.mark.parametrize("accepted_remotely", [False, True])
async def test_unknown_creation_retry_keeps_original_identity(workflow, accepted_remotely):
    receipt = workflow.service.prepare(inputs(), workflow.service.profile())
    workflow.api.next_error = LabError("Synthetic interrupted response", 5)
    workflow.api.accept_before_error = accepted_remotely
    with pytest.raises(LabError, match="outcome unknown"):
        await workflow.service.submit(receipt)
    assert workflow.repository.operation(receipt["operation_id"])["state"] == "Unknown"
    assert workflow.repository.candidates(CLUSTER_ID, "research", "image") == []
    notebook = await workflow.service.retry(receipt["operation_id"])
    assert notebook["metadata"]["name"] == receipt["name"]
    assert notebook["metadata"]["labels"]["lab.operations/id"] == receipt["operation_id"]
    assert len(workflow.api.posts) == (1 if accepted_remotely else 2)
    assert all(manifest == receipt["manifest"] for manifest in workflow.api.posts)
    assert workflow.repository.operation(receipt["operation_id"])["state"] == "Accepted"


async def test_retry_conflicting_operation_never_posts(workflow):
    receipt = workflow.service.prepare(inputs(), workflow.service.profile())
    workflow.api.notebook = deepcopy(receipt["manifest"])
    workflow.api.notebook["metadata"]["labels"]["lab.operations/id"] = "other-operation"
    with pytest.raises(LabError, match="different operation"):
        await workflow.service.retry(receipt["operation_id"])
    assert workflow.api.posts == []


async def test_retry_revalidates_current_policy_before_resubmitting(workflow):
    receipt = workflow.service.prepare(inputs(), workflow.service.profile())
    data = profile_data()
    data["namespace_rules"]["research"]["storage"]["mount_read_only"] = True
    profiles_dir(workflow.service.profiles_dir.parent, data)
    with pytest.raises(LabError, match="policy changed"):
        await workflow.service.retry(receipt["operation_id"])
    assert workflow.api.posts == []
    assert workflow.repository.operation(receipt["operation_id"])["manifest"] == receipt["manifest"]


async def test_failed_presubmit_receipt_write_prevents_submission(workflow, monkeypatch):
    def full_store(*args, **kwargs):
        raise LabError("Synthetic full local store", 8)

    monkeypatch.setattr(workflow.repository, "record_operation", full_store)
    with pytest.raises(LabError, match="full local store"):
        receipt = workflow.service.prepare(inputs(), workflow.service.profile())
        await workflow.service.submit(receipt)
    assert workflow.api.posts == []


async def test_post_acceptance_recording_failure_preserves_remote_identity(workflow, monkeypatch):
    receipt = workflow.service.prepare(inputs(), workflow.service.profile())

    def full_store(*args, **kwargs):
        raise LabError("Synthetic full local store", 8)

    monkeypatch.setattr(workflow.repository, "remember", full_store)
    with pytest.raises(LabError) as caught:
        await workflow.service.submit(receipt)
    assert caught.value.code == 8
    assert receipt["operation_id"] in str(caught.value)
    assert "synthetic-server-uid" in str(caught.value)
    assert workflow.api.notebook is not None
    assert workflow.repository.operation(receipt["operation_id"])["state"] == "Accepted"
    assert len(workflow.api.posts) == 1


class SetupSecrets:
    root_ref = "op://VAULT_ID/NEW_ROOT_ID/notesPlain"
    cluster_ref = "op://VAULT_ID/CLUSTER_ITEM_ID/notesPlain"
    key_ref = "op://VAULT_ID/KEY_ITEM_ID/password"

    def __init__(self, key, change=None):
        self.key = key
        self.change = change
        self.calls = []
        self.root_reads = 0

    async def read(self, reference):
        self.calls.append(reference)
        if reference == self.root_ref:
            self.root_reads += 1
            text = (EXAMPLES / "index.yaml").read_text()
            if self.change == "root" and self.root_reads > 1:
                text = text.replace("research-example", "changed-name")
            return text
        if reference == self.key_ref:
            if self.change == "key" and self.root_reads > 1:
                return Fernet.generate_key().decode()
            return self.key.decode()
        raise AssertionError("Setup must not read cluster credentials")


def setup_inputs(monkeypatch, secrets, directory):
    monkeypatch.setattr(setup, "terminal_required", lambda: None)
    monkeypatch.setattr(setup, "clear_terminal", lambda: None)
    screens = SimpleNamespace(
        menu=AsyncMock(return_value="existing"),
        form=AsyncMock(return_value={"profiles_dir": str(directory)}),
        details=AsyncMock(return_value="save"),
    )

    async def work(awaitable, title, **kwargs):
        return await awaitable

    screens.work = work
    monkeypatch.setattr(setup, "Screens", lambda **kwargs: screens)
    monkeypatch.setattr(setup, "select_reference", AsyncMock(return_value=secrets.root_ref))


async def test_setup_success_verifies_final_selected_reference_chain(workflow, monkeypatch):
    secrets = SetupSecrets(workflow.key)
    setup_inputs(monkeypatch, secrets, workflow.service.profiles_dir)
    await setup.initialize(secrets, workflow.paths)
    assert read_bootstrap(workflow.paths)["index_ref"] == secrets.root_ref
    final_root = max(
        i for i, reference in enumerate(secrets.calls) if reference == secrets.root_ref
    )
    assert set(secrets.calls[final_root + 1 :]) == {secrets.key_ref}


@pytest.mark.parametrize("change", ["root", "key"])
async def test_setup_race_preserves_bootstrap_and_encrypted_state(workflow, monkeypatch, change):
    write_bootstrap(
        workflow.paths, "op://VAULT_ID/ORIGINAL_ROOT_ID/notesPlain", workflow.service.profiles_dir
    )
    workflow.repository.remember(CLUSTER_ID, "research", {"image": "example/image:v1"})
    original_bootstrap = workflow.paths.bootstrap_path.read_bytes()
    original_history = workflow.paths.history_path.read_bytes()
    secrets = SetupSecrets(workflow.key, change)
    setup_inputs(monkeypatch, secrets, workflow.service.profiles_dir)
    with pytest.raises(LabError, match="changed during setup"):
        await setup.initialize(secrets, workflow.paths)
    assert workflow.paths.bootstrap_path.read_bytes() == original_bootstrap
    assert workflow.paths.history_path.read_bytes() == original_history


@pytest.mark.parametrize("position", ["root", "notebook", "action"])
def test_common_arguments_survive_all_command_positions(position):
    common = ["--cluster", "research-example", "--token-stdin", "--json", "--yes"]
    tokens = {
        "root": [*common, "notebook", "list"],
        "notebook": ["notebook", *common, "list"],
        "action": ["notebook", "list", *common],
    }[position]
    args = cli.parser().parse_args([*tokens, "--namespace", "research"])
    cli.validate_arguments(args)
    assert args.cluster == "research-example"
    assert args.token_stdin and args.json and args.yes


@pytest.mark.parametrize(
    "options",
    [
        ["create", "--name", "example", "--namespace", "research", "--timeout", "nan"],
        ["status", "example", "--namespace", "research", "--operation-id", "saved-operation"],
    ],
)
def test_invalid_arguments_fail_before_authentication(monkeypatch, options):
    authenticate = AsyncMock(side_effect=AssertionError("Authentication must not start"))
    monkeypatch.setattr(cli.Secrets, "authenticate", authenticate)
    args = cli.parser().parse_args(["--token-stdin", "notebook", *options])
    with pytest.raises(LabError) as caught:
        asyncio.run(cli.run(args))
    assert caught.value.code == 2
    authenticate.assert_not_awaited()


def run_creation_cli(workflow, monkeypatch, *, wait=False):
    app = cli.Application(workflow.service, None)

    async def run_without_authentication(args):
        await app.dispatch(args)

    monkeypatch.setattr(cli, "run", run_without_authentication)
    monkeypatch.setattr(cli, "protect_process", lambda: None)
    values = inputs().model_dump()
    flags = ["lab", "--cluster", "research-example", "--json", "notebook", "create"]
    if wait:
        flags.append("--wait")
    for key, value in values.items():
        if value is not None:
            flags.extend(["--" + key.replace("_", "-"), str(value)])
    monkeypatch.setattr(cli.sys, "argv", flags)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    return caught.value.code


def test_json_runtime_failure_retains_accepted_notebook_identity(workflow, monkeypatch, capsys):
    workflow.api.pod_state = "Failed"
    assert run_creation_cli(workflow, monkeypatch, wait=True) == 7
    output = json.loads(capsys.readouterr().out)
    assert output["target"]["name"] == inputs().name
    assert output["target"]["namespace"] == "research"
    assert output["data"]["uid"] == "synthetic-server-uid"
    assert workflow.repository.operation(output["data"]["operation_id"])["state"] == "RuntimeFailed"
    assert len(workflow.api.posts) == 1


def test_json_unknown_creation_reports_unknown_state(workflow, monkeypatch, capsys):
    workflow.api.next_error = LabError("Synthetic interrupted response", 5)
    workflow.api.accept_before_error = True
    assert run_creation_cli(workflow, monkeypatch) == 5
    output = json.loads(capsys.readouterr().out)
    receipt = workflow.repository.operation(output["data"]["operation_id"])
    assert receipt["state"] == "Unknown"
    assert output["data"]["state"] == "Unknown"
    assert output["target"]["name"] == inputs().name
    assert len(workflow.api.posts) == 1


def test_json_local_recording_failure_reports_accepted_uid(workflow, monkeypatch, capsys):
    def full_store(*args, **kwargs):
        raise LabError("Synthetic full local store", 8)

    monkeypatch.setattr(workflow.repository, "remember", full_store)
    assert run_creation_cli(workflow, monkeypatch) == 8
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["state"] == "Accepted"
    assert output["data"]["uid"] == "synthetic-server-uid"
    assert output["target"]["name"] == inputs().name
    assert len(workflow.api.posts) == 1


async def test_kubernetes_reads_delayed_json_chunks_through_eof():
    async def response(request):
        assert request.headers["Authorization"] == "Bearer synthetic-workflow-token"
        stream = web.StreamResponse(headers={"Content-Type": "application/json"})
        await stream.prepare(request)
        await stream.write(b'{"items":[')
        await asyncio.sleep(0.03)
        await stream.write(b'{"metadata":{"name":"example"}}]}')
        await stream.write_eof()
        return stream

    app = web.Application()
    app.router.add_get("/chunked", response)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    session = SimpleNamespace(
        endpoint=f"http://127.0.0.1:{port}",
        token=SecretStr("synthetic-workflow-token"),
        ssl_context=False,
        tls_name=None,
        check=lambda: None,
        check_health=AsyncMock(),
    )
    try:
        result = await Kubernetes(session).request("GET", "/chunked")
        assert result == {"items": [{"metadata": {"name": "example"}}]}
        session.check_health.assert_not_awaited()
    finally:
        await runner.cleanup()


async def test_machine_success_is_collected_until_session_cleanup(workflow, capsys):
    app = cli.Application(workflow.service, None)
    values = inputs().model_dump()
    flags = ["--cluster", "research-example", "--json", "notebook", "create"]
    for key, value in values.items():
        if value is not None:
            flags.extend(["--" + key.replace("_", "-"), str(value)])
    await app.dispatch(cli.parser().parse_args(flags))
    assert capsys.readouterr().out == ""
    assert app.result["status"] == "Accepted"
    assert app.result["data"]["uid"] == "synthetic-server-uid"


def test_unexpected_failure_is_one_redacted_json_result(monkeypatch, capsys):
    async def fail(args):
        raise RuntimeError("synthetic-secret-must-not-appear")

    monkeypatch.setattr(cli, "run", fail)
    monkeypatch.setattr(cli, "protect_process", lambda: None)
    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["lab", "--json", "--cluster", "example", "notebook", "list", "--namespace", "research"],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 8
    output = capsys.readouterr().out
    assert "synthetic-secret" not in output
    assert json.loads(output)["status"] == "error"


async def test_existing_notebook_reads_and_reconciliation_do_not_require_profile(workflow):
    receipt = workflow.service.prepare(inputs(), workflow.service.profile())
    created = await workflow.service.submit(receipt)
    (workflow.service.profiles_dir / f"{CLUSTER_ID}.yaml").unlink()
    assert await workflow.service.list_notebooks("research") == [created]
    assert (await workflow.service.status("research", inputs().name))["state"] == "Running"
    assert await workflow.service.reconcile(receipt["operation_id"]) == created
    assert await workflow.service.retry(receipt["operation_id"]) == created
    assert len(workflow.api.posts) == 1


async def test_absent_retry_requires_profile_and_never_posts_when_missing(workflow):
    receipt = workflow.service.prepare(inputs(), workflow.service.profile())
    (workflow.service.profiles_dir / f"{CLUSTER_ID}.yaml").unlink()
    with pytest.raises(LabError, match="Profile not found"):
        await workflow.service.retry(receipt["operation_id"])
    assert workflow.api.posts == []
