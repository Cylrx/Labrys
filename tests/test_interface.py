"""Interactive contracts against durable local history and a fictional API."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from cryptography.fernet import Fernet
from fixtures import CLUSTER_ID, cluster, inputs, profile_data, profiles_dir

from lab import interface
from lab.errors import LabError
from lab.history import Repository
from lab.interface import FIELDS, Interactive
from lab.kubernetes import ApiError
from lab.notebooks import Notebooks
from lab.storage import Paths


class Screens:
    def __init__(self):
        self.answers = {}
        self.events = []
        self.cancel_work = None

    def answer(self, title, default, *args):
        self.events.append((title, args))
        values = self.answers.get(title, [])
        value = values.pop(0) if values else default
        if isinstance(value, BaseException):
            raise value
        return value

    async def form(self, title, fields, **kwargs):
        return self.answer(title, {item.name: item.value for item in fields}, fields)

    async def choose(self, title, choices, **kwargs):
        return self.answer(title, choices[0][0], choices)

    async def menu(self, title, description, sections, **kwargs):
        return self.answer(title, "back", sections)

    async def status_list(self, title, description, choices, load, actions):
        return self.answer(title, "back", choices, actions)

    async def details(self, title, description, rows, actions, **kwargs):
        return self.answer(
            title, "create" if title == "Create this Notebook?" else "back", rows, actions
        )

    async def confirm(self, title, description, rows):
        return self.answer(title, True, rows)

    async def work(self, awaitable, title, **kwargs):
        self.events.append((title, kwargs))
        if self.cancel_work == title:
            awaitable.close()
            raise LabError("Stopped watching. The Notebook remains.", 130)
        return await awaitable


class API:
    def __init__(self):
        self.session = SimpleNamespace(
            state="connected", remaining=1200, check=lambda: None, reconnect=AsyncMock()
        )
        self.notebook = None
        self.posts = []
        self.changes = []
        self.next_error = None
        self.pods = None
        self.nodes = []

    async def request(self, method, path, *, body=None):
        if method == "POST":
            self.posts.append(deepcopy(body))
            self.notebook = deepcopy(body)
            self.notebook["metadata"].update(uid="server-uid", resourceVersion="3")
            if self.next_error:
                error, self.next_error = self.next_error, None
                raise error
            return deepcopy(self.notebook)
        if self.notebook is None:
            raise ApiError(404, method)
        if method != "GET":
            self.changes.append((method, deepcopy(body)))
        return deepcopy(self.notebook)

    async def collection(self, path, **kwargs):
        if path.endswith("/nodes"):
            return self.nodes
        if path.endswith("/statefulsets"):
            return []
        if path.endswith("/notebooks"):
            return [deepcopy(self.notebook)] if self.notebook else []
        assert path.endswith("/pods")
        if self.pods is not None:
            return self.pods
        return [pod("owned", "server-uid")]


def pod(name, owner, containers=("main",), ready=True):
    return {
        "metadata": {"name": name, "ownerReferences": [{"uid": owner}]},
        "spec": {"containers": [{"name": name} for name in containers]},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


@pytest.fixture
def app(tmp_path):
    repository = Repository(
        Paths(tmp_path / "config", tmp_path / "data", tmp_path / "state"), Fernet.generate_key()
    )
    api = API()
    return Interactive(
        Notebooks(api, cluster(), repository, profiles_dir(tmp_path)), None, Screens()
    )


def create_command(**changes):
    values = inputs(**changes).model_dump()
    return "create " + " ".join(
        "--" + key.replace("_", "-") + " " + str(value)
        for key, value in values.items()
        if value is not None
    )


async def test_creation_uses_real_policy_and_all_editable_fields(app):
    await app.dispatch(create_command())
    assert len(app.service.api.posts) == 1
    manifest = app.service.api.posts[0]
    operation_id = manifest["metadata"]["labels"]["lab.operations/id"]
    assert app.service.receipt(operation_id)["state"] == "Ready"
    assert app.result["data"]["uid"] == "server-uid"
    assert app.result["data"]["operation_id"] == operation_id
    definition = next(args[0] for title, args in app.ui.events if title == "Create Notebook")
    assert [item.name for item in definition] == [name for name, _, _ in FIELDS]
    assert {item.name for item in definition if item.numeric} == {"cpu", "memory", "gpus"}
    preview = next(args[0] for title, args in app.ui.events if title == "Create this Notebook?")
    preview = yaml.safe_load(dict(preview)["Manifest"])
    assert preview["spec"] == manifest["spec"] or (
        preview["spec"]["template"]["spec"] == manifest["spec"]["template"]["spec"]
    )
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"]["cpu"] == "2000m"
    assert app.service.history.presets(CLUSTER_ID, "research") == []


async def test_discovery_and_creation_share_private_profile(app):
    values = inputs().model_dump()
    policy = app.service.profile().namespace(values["namespace"])
    app.service.api.nodes = [
        {
            "metadata": {
                "labels": {
                    **policy.placement.cpu_required_selector,
                    "kubernetes.io/hostname": "example-node",
                }
            }
        },
        {"metadata": {"labels": {"kubernetes.io/hostname": "unrelated"}}},
    ]
    candidates, _ = await app.discovery(values["namespace"], values)
    assert candidates["node"] == ["example-node"]
    app.ui.answers["Save this configuration?"] = ["new"]
    app.ui.answers["Save preset"] = [{"name": "Owned"}]
    await app.create(SimpleNamespace(**values, preset=None, yes=False, timeout=300))
    assert len(app.service.api.posts) == 1
    preset = app.service.history.presets(CLUSTER_ID, values["namespace"])[0]
    assert preset["editable_fields"]["owner"] == values["owner"]
    assert app.service.history.candidates(CLUSTER_ID, values["namespace"], "owner") == [
        values["owner"]
    ]


async def test_missing_profile_blocks_creation_before_discovery(app):
    (app.service.profiles_dir / f"{CLUSTER_ID}.yaml").unlink()
    with pytest.raises(LabError, match="Profile not found"):
        await app.dispatch(create_command())
    assert not app.service.api.posts
    assert not app.ui.events


async def test_confirmation_precedes_preparation(app):
    app.ui.answers["Create this Notebook?"] = ["cancel"]
    await app.dispatch(create_command())
    assert app.service.api.posts == []
    assert app.service.history.history.read()["operations"] == {}


async def test_namespace_history_precedes_scoped_form_and_discovery_is_open(app):
    history = app.service.history
    history.remember(CLUSTER_ID, "research", {"image": inputs().image, "node": "past-node"})
    history.remember(CLUSTER_ID, "other", {"image": "foreign/image:v2", "node": "foreign-node"})
    app.ui.answers["Namespace"] = [{"namespace": "research"}]
    app.ui.answers["Create Notebook"] = [inputs().model_dump()]
    await app.dispatch("create")
    titles = [title for title, _ in app.ui.events]
    assert titles.index("Namespace") < titles.index("Create Notebook")
    definitions = next(args[0] for title, args in app.ui.events if title == "Create Notebook")
    by_name = {item.name: item for item in definitions}
    assert by_name["image"].candidates == [inputs().image]
    assert by_name["node"].candidates == ["past-node"]
    assert by_name["image"].value == ""
    assert "foreign-node" not in str(definitions)


async def test_preset_offer_is_ready_only_and_seen_fingerprint_omits_name(app):
    app.ui.answers["Save this configuration?"] = ["new"]
    app.ui.answers["Save preset"] = [{"name": "Reusable"}]
    await app.dispatch(create_command())
    preset = app.service.history.presets(CLUSTER_ID, "research")[0]
    assert "name" not in preset["editable_fields"]
    app.ui.answers["Creation template"] = [""]
    await app.dispatch(create_command(name="second"))
    assert sum(title == "Save this configuration?" for title, _ in app.ui.events) == 1
    receipt = app.service.prepare(inputs(name="not-ready"), app.service.profile())
    with pytest.raises(LabError, match="must become Ready"):
        await app.save_preset(receipt, explicit=True)


async def test_original_preset_updates_only_on_explicit_choice(app):
    original = app.service.history.save_preset(
        CLUSTER_ID, "research", "Original", inputs().model_dump()
    )
    app.ui.answers["Save this configuration?"] = ["update"]
    await app.dispatch(create_command(cpu="3") + " --preset " + original["id"])
    saved = app.service.history.presets(CLUSTER_ID, "research")
    assert len(saved) == 1
    assert saved[0]["id"] == original["id"]
    assert saved[0]["editable_fields"]["cpu"] == "3000m"


async def test_cancelled_watch_preserves_receipt_and_uid_without_saving(app):
    app.ui.cancel_work = "Waiting for Ready"
    with pytest.raises(LabError) as caught:
        await app.dispatch(create_command())
    assert caught.value.code == 130
    assert caught.value.data["uid"] == "server-uid"
    assert caught.value.data["state"] == "Accepted"
    operation_id = caught.value.data["operation_id"]
    assert app.service.receipt(operation_id)["state"] == "Accepted"
    assert app.service.api.changes == []
    assert all(title != "Save this configuration?" for title, _ in app.ui.events)


async def test_parent_cancellation_during_wait_keeps_operation_context(app, monkeypatch):
    waiting = asyncio.Event()

    async def wait(*args):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.service, "wait", wait)
    task = asyncio.create_task(app.dispatch(create_command()))
    await asyncio.wait_for(waiting.wait(), timeout=2)
    task.cancel()
    with pytest.raises(LabError) as caught:
        await task
    assert caught.value.data["uid"] == "server-uid"
    assert app.service.receipt(caught.value.data["operation_id"])["state"] == "Accepted"


async def test_unknown_submission_and_retry_keep_the_same_identity(app):
    app.service.api.next_error = LabError("Synthetic interrupted response", 5)
    with pytest.raises(LabError) as caught:
        await app.dispatch(create_command())
    operation_id = caught.value.data["operation_id"]
    assert caught.value.data["state"] == "Unknown"
    await app.dispatch("retry --operation-id " + operation_id)
    assert len(app.service.api.posts) == 1
    assert app.result["data"]["operation_id"] == operation_id
    assert app.result["data"]["uid"] == "server-uid"
    assert any(title == "Retry this operation?" for title, _ in app.ui.events)


async def test_retry_policy_failure_keeps_receipt_context(app):
    receipt = app.service.prepare(inputs(), app.service.profile())
    changed = profile_data()
    changed["namespace_rules"]["research"]["storage"]["mount_read_only"] = True
    profiles_dir(app.service.profiles_dir.parent, changed)
    with pytest.raises(LabError, match="policy changed") as caught:
        await app.dispatch("retry --operation-id " + receipt["operation_id"])
    assert caught.value.data["operation_id"] == receipt["operation_id"]
    assert app.service.api.posts == []


async def test_access_chooses_only_ready_owned_containers_and_calls_real_adapter(app, monkeypatch):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)
    (app.service.profiles_dir / f"{CLUSTER_ID}.yaml").unlink()
    app.service.api.pods = [
        pod("foreign", "foreign-uid"),
        pod("pending", "server-uid", ready=False),
        pod("owned", "server-uid", ("main", "sidecar")),
    ]
    app.ui.answers["Choose container"] = ["1"]
    shell = AsyncMock(return_value=0)
    monkeypatch.setattr(interface, "open_shell", shell)
    await app.dispatch("shell example-notebook --namespace research")
    assert shell.call_args.args[-2:] == ("owned", "sidecar")
    choices = next(args[0] for title, args in app.ui.events if title == "Choose container")
    assert choices == [("0", "owned / main"), ("1", "owned / sidecar")]
    assert all(title != "Opening shell" for title, _ in app.ui.events)
    shell.reset_mock()
    with pytest.raises(LabError, match="No matching Ready"):
        await app.dispatch("shell example-notebook --namespace research --pod foreign")
    shell.assert_not_awaited()


@pytest.mark.parametrize("action", ["start", "stop", "delete"])
async def test_changes_require_confirmation_and_keep_uid(app, action):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)
    app.ui.answers[action.title() + " Notebook?"] = [False]
    await app.dispatch(action + " example-notebook --namespace research")
    assert app.service.api.changes == []
    assert app.result["data"]["uid"] == "server-uid"


@pytest.mark.parametrize(
    "command",
    [
        "list --json",
        "list --cluster elsewhere",
        "create --timeout nan",
        "list --token-stdin",
        "retry",
        "--version",
    ],
)
async def test_invalid_commands_fail_before_api_or_ui(app, command):
    with pytest.raises(LabError):
        await app.dispatch(command)
    assert app.service.api.posts == []
    assert app.ui.events == []


async def test_grouped_session_menu_and_back_navigation(app):
    app.ui.answers["Session menu"] = ["create", "status", "exit"]
    app.ui.answers["Namespace"] = [LabError("Cancelled", 130)]
    await app.run()
    sections = next(args[0] for title, args in app.ui.events if title == "Session menu")
    assert [heading for heading, _ in sections] == ["NOTEBOOKS", "MANAGE", "SESSION"]
    assert any(title == "Session status" for title, _ in app.ui.events)
    assert app.service.api.posts == []


async def test_blank_gpu_count_does_not_silently_create_a_cpu_notebook(app):
    values = inputs().model_dump()
    values["gpus"] = ""
    app.ui.answers["Create Notebook"] = [values]
    with pytest.raises(LabError, match="GPU count explicitly"):
        await app.dispatch(create_command())
    assert app.service.api.posts == []


async def test_namespace_edit_reloads_scoped_history_and_drops_original_preset(app):
    data = profile_data()
    data["namespace_rules"]["second"] = deepcopy(data["namespace_rules"]["research"])
    profiles_dir(app.service.profiles_dir.parent, data)
    original = app.service.history.save_preset(
        CLUSTER_ID, "research", "Original", inputs().model_dump()
    )
    app.service.history.remember(CLUSTER_ID, "second", {"node": "second-node"})
    app.ui.answers["Create Notebook"] = [inputs(namespace="second").model_dump()]
    app.ui.answers["Save this configuration?"] = ["skip"]
    await app.dispatch(create_command() + " --preset " + original["id"])
    forms = [args[0] for title, args in app.ui.events if title == "Create Notebook"]
    assert len(forms) == 2
    assert next(item for item in forms[-1] if item.name == "node").candidates == ["second-node"]
    choices = next(args[0] for title, args in app.ui.events if title == "Save this configuration?")
    assert all(identity != "update" for identity, _ in choices)
    assert app.result["target"]["namespace"] == "second"


async def test_stop_sends_uid_and_version_guards_and_delete_sends_uid_precondition(app):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)
    await app.dispatch("stop example-notebook --namespace research")
    method, patch = app.service.api.changes[-1]
    assert method == "PATCH"
    assert patch[0] == {"op": "test", "path": "/metadata/uid", "value": "server-uid"}
    assert patch[1]["value"] == "3"
    await app.dispatch("delete example-notebook --namespace research")
    method, body = app.service.api.changes[-1]
    assert method == "DELETE"
    assert body["preconditions"] == {"uid": "server-uid"}
    assert app.result["data"]["uid"] == "server-uid"


async def test_editor_uses_actual_adapter_and_keeps_authorization(app, monkeypatch):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)
    editor = AsyncMock()
    monkeypatch.setattr(interface, "open_editor", editor)
    await app.dispatch("open example-notebook --namespace research --pod owned --container main")
    assert editor.call_args.args[-2:] == ("owned", "main")
    assert app.service.api.session.state == "connected"
    assert any(title == "VS Code window requested" for title, _ in app.ui.events)


async def test_editor_only_menu_preserves_session_actions(app):
    app.ui.answers["Session menu"] = ["command:list --namespace research", "exit"]
    await app.run(editor_only=True)
    sections = next(args[0] for title, args in app.ui.events if title == "Session menu")
    assert [heading for heading, _ in sections] == ["SESSION"]
    assert any(title == "Unable to complete action" for title, _ in app.ui.events)
    assert app.service.api.posts == []


async def test_session_cancellation_on_failure_page_preserves_known_identity(app, monkeypatch):
    app.ui.answers["Session menu"] = ["command:" + create_command()]
    app.ui.cancel_work = "Waiting for Ready"
    showing_error = asyncio.Event()
    original_details = app.ui.details

    async def details(title, *args, **kwargs):
        if title == "Operation interrupted":
            showing_error.set()
            await asyncio.Event().wait()
        return await original_details(title, *args, **kwargs)

    monkeypatch.setattr(app.ui, "details", details)
    task = asyncio.create_task(app.run())
    await asyncio.wait_for(showing_error.wait(), 2)
    task.cancel()
    with pytest.raises(LabError) as caught:
        await task
    assert caught.value.data["uid"] == "server-uid"
    assert app.service.receipt(caught.value.data["operation_id"])["state"] == "Accepted"


async def test_status_escape_returns_to_browser_without_interruption_page(app):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)
    app.ui.answers["Notebooks"] = ["server-uid", "back"]
    app.ui.answers["Notebook status"] = [LabError("Cancelled", 130)]
    await app.dispatch("list --namespace research")
    assert sum(title == "Notebooks" for title, _ in app.ui.events) == 2
    assert all(title != "Operation interrupted" for title, _ in app.ui.events)


async def test_preset_escape_preserves_ready_receipt_without_error_page(app):
    app.ui.answers["Save this configuration?"] = [LabError("Cancelled", 130)]
    await app.dispatch(create_command())
    assert app.result["data"]["state"] == "Ready"
    assert any(title == "Operation receipt" for title, _ in app.ui.events)
    assert all(title != "Operation interrupted" for title, _ in app.ui.events)


async def test_explicit_yes_skips_create_confirmation(app):
    await app.dispatch(create_command() + " --yes")
    assert len(app.service.api.posts) == 1
    assert "Create this Notebook?" not in [title for title, _ in app.ui.events]


@pytest.mark.parametrize("action", ["start", "stop", "delete"])
async def test_explicit_yes_authorizes_only_the_requested_change(app, action):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)
    if action == "start":
        app.service.api.notebook["metadata"]["annotations"] = {
            "kubeflow-resource-stopped": "synthetic-time"
        }
    app.ui.answers["Notebook status"] = ["stop", "back"]
    app.ui.answers["Stop Notebook?"] = [False]
    await app.dispatch(action + " example-notebook --namespace research --yes")
    assert len(app.service.api.changes) == 1
    assert app.result["data"]["uid"] == "server-uid"
    if action != "delete":
        assert "Stop Notebook?" in [title for title, _ in app.ui.events]


async def test_explicit_yes_authorizes_only_first_retry(app):
    receipt = app.service.prepare(inputs(), app.service.profile())
    app.ui.answers["Operation receipt"] = ["retry", "back"]
    app.ui.answers["Retry this operation?"] = [False]
    await app.dispatch("retry --operation-id " + receipt["operation_id"] + " --yes")
    assert len(app.service.api.posts) == 1
    assert [title for title, _ in app.ui.events].count("Retry this operation?") == 1


@pytest.mark.parametrize("flag", ["-h", "--help"])
async def test_help_preserves_authorized_session(app, flag):
    app.ui.answers["Session menu"] = ["command:list " + flag, "status", "exit"]
    await app.run()
    titles = [title for title, _ in app.ui.events]
    assert "Notebook commands" in titles
    assert "Session status" in titles
    assert titles.count("Session menu") == 3


@pytest.mark.parametrize("replacement", ["uid", "operation"])
async def test_receipt_navigation_rejects_replacement_before_action(app, replacement):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)
    if replacement == "uid":
        app.service.api.notebook["metadata"]["uid"] = "replacement-uid"
    else:
        app.service.api.notebook["metadata"]["labels"]["lab.operations/id"] = (
            "replacement-operation"
        )
    app.ui.answers["Operation receipt"] = ["status", "back"]
    app.ui.answers["Notebook status"] = ["delete", "back"]
    with pytest.raises(LabError, match="different Notebook"):
        await app.operation(receipt["operation_id"])
    assert app.service.api.changes == []
    assert app.outcome["data"]["uid"] == "server-uid"
    assert app.outcome["data"]["operation_id"] == receipt["operation_id"]


async def test_cancelled_delete_shows_identity_instead_of_silent_menu(app, monkeypatch):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)
    original = app.service.change

    async def change(notebook, action):
        await original(notebook, action)
        raise LabError("Interrupted response; inspect before retrying.", 130)

    monkeypatch.setattr(app.service, "change", change)
    app.ui.answers["Session menu"] = [
        "command:delete example-notebook --namespace research",
        "exit",
    ]
    await app.run()
    assert len(app.service.api.changes) == 1
    rows = next(args[0] for title, args in app.ui.events if title == "Operation interrupted")
    assert dict(rows)["Uid"] == "server-uid"
    assert dict(rows)["Action"] == "delete"


async def test_real_screens_drive_create_and_return_to_session(app):
    from test_ui_screens import click, terminal

    async with terminal(width=100, height=45) as (screens, keys, output):
        app.screens = screens
        task = asyncio.create_task(app.run())

        async def page(title):
            async def shown():
                while not (
                    hasattr(screens, "page")
                    and screens.page.title == title
                    and screens.page.app
                    and screens.page.app.is_running
                    and screens.page.app.renderer._last_screen
                ):
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(shown(), 2)

        try:
            await page("Session menu")
            keys.send_text(create_command() + "\r")
            await page("Create Notebook")
            await click(screens, keys, "Review manifest")
            await page("Create this Notebook?")
            assert app.service.api.posts == []
            await click(screens, keys, "Back to fields")
            await page("Create Notebook")
            assert screens.page.inputs["name"].area.text == inputs().name
            assert app.service.api.posts == []
            await click(screens, keys, "Review manifest")
            await page("Create this Notebook?")
            keys.send_text("\x1b")
            await page("Create Notebook")
            assert screens.page.inputs["image"].area.text == inputs().image
            await click(screens, keys, "Review manifest")
            await page("Create this Notebook?")
            await click(screens, keys, "Create Notebook")
            await page("Save this configuration?")
            await click(screens, keys, "Do not save")
            await page("Operation receipt")
            await click(screens, keys, "Session menu")
            await page("Session menu")
            await click(screens, keys, "Disconnect")
            await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert len(app.service.api.posts) == 1
        receipt = app.service.history.history.read()["operations"].popitem()[1]
        assert receipt["state"] == "Ready"
        assert receipt["uid"] == "server-uid"
        assert not output.mouse_enabled


@pytest.mark.parametrize("back", ["back", LabError("Cancelled", 130)])
async def test_review_returns_to_populated_fields_before_any_write(app, back):
    original = app.service.history.save_preset(
        CLUSTER_ID, "research", "Original", inputs().model_dump()
    )
    form = app.ui.form
    seen = []

    async def edit(title, fields, **kwargs):
        values = await form(title, fields, **kwargs)
        if title == "Create Notebook":
            assert not app.service.api.posts
            assert app.service.history.history.read()["operations"] == {}
            seen.append(values.copy())
            if len(seen) == 1:
                values.update(name="revised", cpu="3")
            else:
                assert values == seen[0] | {"name": "revised", "cpu": "3"}
                values["name"] = "final-name"
        return values

    app.ui.form = edit
    app.ui.answers["Create this Notebook?"] = [back, "create"]
    app.ui.answers["Save this configuration?"] = ["update"]
    await app.dispatch(create_command() + " --preset " + original["id"])
    assert len(seen) == 2
    assert len(app.service.api.posts) == 1
    assert app.service.api.posts[0]["metadata"]["name"] == "final-name"
    presets = app.service.history.presets(CLUSTER_ID, "research")
    assert len(presets) == 1
    assert presets[0]["id"] == original["id"]
    assert presets[0]["editable_fields"]["cpu"] == "3000m"


async def test_recovery_open_retains_editor_only_session(monkeypatch):
    from test_editor_recovery import NOTEBOOK

    from lab import editor_recovery

    service = SimpleNamespace(
        get=AsyncMock(return_value=NOTEBOOK), cluster=SimpleNamespace(cluster_id="fixture")
    )
    screens = Screens()
    screens.answers["VS Code Server"] = ["open"]
    selected = editor_recovery.Target(
        "cluster",
        "research",
        "fixture",
        "notebook-uid",
        "fixture-0",
        "pod",
        "notebook",
        "container",
        0,
        "image",
    )
    monkeypatch.setattr(
        editor_recovery,
        "restart",
        AsyncMock(return_value=editor_recovery.Result("OriginalProcessExited", selected)),
    )
    monkeypatch.setattr(editor_recovery, "revalidate", AsyncMock())
    monkeypatch.setattr(interface, "open_editor", AsyncMock())
    native = Interactive(service, None, screens)
    native.target = AsyncMock(return_value=("fixture-0", "notebook"))
    native._run = AsyncMock()
    await native.notebook("research", "fixture", "editor-restart")
    editor_recovery.revalidate.assert_awaited_once_with(service, NOTEBOOK, selected)
    interface.open_editor.assert_awaited_once()
    native._run.assert_awaited_once_with(editor_only=True)


async def test_stop_progress_remains_visible_until_owned_pod_disappears(app, monkeypatch):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)
    app.service.api.notebook["metadata"]["annotations"] = {"kubeflow-resource-stopped": "now"}
    live = pod("owned", "server-uid")
    terminating = deepcopy(live)
    terminating["metadata"]["deletionTimestamp"] = "now"
    observations = [[live], [terminating], []]
    original_status = app.service.status
    pages = []

    async def status(namespace, name):
        app.service.api.pods = observations.pop(0)
        return await original_status(namespace, name)

    async def details(title, description, rows, actions, **kwargs):
        pages.append((dict(rows), actions, kwargs["disabled"]))
        return "status" if len(pages) < 3 else "back"

    monkeypatch.setattr(app.service, "status", status)
    monkeypatch.setattr(app.ui, "details", details)
    await app.notebook("research", "example-notebook")
    assert [rows["State"] for rows, _, _ in pages] == ["Stopping…", "Stopping…", "Stopped"]
    assert pages[0][0]["Pod"] == "owned · Ready"
    assert pages[1][0]["Pod"] == "owned · Terminating"
    assert pages[2][0]["Pod"] == "None — no running instance"
    assert pages[2][1][0] == ("start", "Start")
    assert all("Startup resources" in rows and "Resources" not in rows for rows, _, _ in pages)
    assert all({"shell", "open", "editor-restart", "stop"} <= d.keys() for _, _, d in pages)
    assert app.result["data"]["state"] == "Stopped"
    assert app.result["data"]["stopped"] is True


async def test_failed_pod_observation_does_not_report_stopped(app, monkeypatch):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)
    app.service.api.notebook["metadata"]["annotations"] = {"kubeflow-resource-stopped": "now"}
    monkeypatch.setattr(app.service.api, "collection", AsyncMock(side_effect=ApiError(403, "GET")))
    with pytest.raises(ApiError):
        await app.notebook("research", "example-notebook")
    assert not any(title == "Notebook status" for title, _ in app.ui.events)


async def test_stop_request_blocks_connection_even_before_pod_termination(app):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)
    app.service.api.notebook["metadata"]["annotations"] = {"kubeflow-resource-stopped": "now"}
    with pytest.raises(LabError, match="stopping or stopped"):
        await app.notebook("research", "example-notebook", action="shell")


async def test_browse_keeps_selected_notebook_uid_across_background_lookup(app, monkeypatch):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)

    async def select(title, description, choices, load, actions):
        assert choices == [("server-uid", "example-notebook")]
        assert await load() == {"server-uid": "Running"}
        app.service.api.notebook["metadata"]["uid"] = "replacement-uid"
        return "server-uid"

    monkeypatch.setattr(app.ui, "status_list", select)
    with pytest.raises(LabError, match="different Notebook"):
        await app.browse("research")
    assert app.service.api.changes == []


async def test_profile_edit_during_confirmation_cannot_change_reviewed_manifest(app):
    original_details = app.ui.details

    async def edit_profile_after_review(title, *args, **kwargs):
        if title == "Create this Notebook?":
            changed = profile_data()
            changed["namespace_rules"]["research"]["storage"]["mount_read_only"] = True
            profiles_dir(app.service.profiles_dir.parent, changed)
        return await original_details(title, *args, **kwargs)

    app.ui.details = edit_profile_after_review
    await app.dispatch(create_command())
    container = app.service.api.posts[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["volumeMounts"][0]["readOnly"] is False
    assert app.service.profile().namespace("research").storage.mount_read_only is True


@pytest.mark.parametrize("action", ["start", "stop", "delete"])
async def test_existing_notebook_changes_do_not_require_profile(app, action):
    receipt = app.service.prepare(inputs(), app.service.profile())
    await app.service.submit(receipt)
    (app.service.profiles_dir / f"{CLUSTER_ID}.yaml").unlink()
    await app.dispatch(action + " example-notebook --namespace research --yes")
    assert app.result["data"]["uid"] == "server-uid"
