"""Exercise simulated Notebook journeys through the same callbacks as the UI."""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
model_module = importlib.import_module("scripts.demo.model")
view_module = importlib.import_module("scripts.demo.notebooks")
SAMPLE, DemoError, Model = model_module.SAMPLE, model_module.DemoError, model_module.Model
CREATION_FIELDS, NotebookViews, parse_command = (
    view_module.CREATION_FIELDS,
    view_module.NotebookViews,
    view_module.parse_command,
)


class RecordingUI:
    """Record the active screen and cancel scheduled work on navigation."""

    def __init__(self):
        self.pending = None

    def _screen(self, kind, title, description):
        self.kind, self.title, self.description = kind, title, description
        self.pending = None

    def menu(self, title, description, sections, back=None, on_command=None, help_text=""):
        self._screen("menu", title, description)
        self.actions = [action for _, actions in sections for action in actions]
        self.back, self.on_command = back, on_command

    def table(
        self, title, description, choices, states, actions, back=None, on_command=None, **kwargs
    ):
        self._screen("menu", title, description)
        self.states = states
        self.actions = [(label, callback) for _, label, callback in choices] + actions
        self.back, self.on_command = back, on_command

    def form(self, title, description, fields, submit_label, on_submit, back, help_text=""):
        self._screen("form", title, description)
        self.fields, self.submit, self.back = fields, on_submit, back

    def details(self, title, description, rows, actions, back=None, help_text="", disabled=None):
        self._screen("details", title, description)
        self.rows, self.actions, self.back = rows, actions, back
        self.disabled = disabled or {}

    def confirm(self, title, description, rows, on_confirm, back):
        self._screen("confirm", title, description)
        self.rows, self.accept, self.back = rows, on_confirm, back

    def schedule(self, delay_seconds, callback):
        self.pending = callback

    def choose(self, label):
        next(callback for text, callback in self.actions if text == label)()


@pytest.fixture
def session():
    clock = [0.0]
    model = Model(clock=lambda: clock[0])
    model.authorize("demo-east")
    ui = RecordingUI()

    def home():
        ui.menu("Home", "", [])

    return NotebookViews(ui, model, home), ui, model, clock


def test_creation_has_all_fields_and_does_not_accept_history(session):
    views, ui, _, _ = session
    views.create("research")
    assert [field.name for field in ui.fields] == list(CREATION_FIELDS)
    fields = {field.name: field for field in ui.fields}
    assert fields["namespace"].value == "research"
    assert fields["name"].value == ""
    assert fields["name"].candidates == ["demo-training"]
    assert fields["gpu_type"].candidates
    assert all(fields[name].numeric for name in ("gpus", "cpu", "memory"))


def test_confirmation_precedes_creation_and_ready_precedes_preset(session):
    views, ui, model, clock = session
    views.create("research")
    ui.submit({**SAMPLE, "name": "new-notebook"})
    assert ui.title == "Create this Notebook?"
    assert model.operations() == []
    ui.choose("Back to fields")
    assert ui.kind == "form"
    assert next(field.value for field in ui.fields if field.name == "name") == "new-notebook"
    ui.submit({**SAMPLE, "name": "new-notebook"})
    ui.choose("Create Notebook")
    assert ui.title == "Waiting for Notebook"
    assert not model.presets("research")
    clock[0] = 1.0
    ui.pending()
    assert ui.title == "Save this configuration?"
    ui.choose("Save as a new preset")
    ui.submit({"name": "Research GPU"})
    saved = model.presets("research")[0]
    assert "name" not in saved["editable_fields"]
    ui.choose("Create from this preset")
    fields = {field.name: field for field in ui.fields}
    assert fields["name"].value == ""
    assert fields["image"].value == SAMPLE["image"]


def test_stopping_watch_leaves_notebook_and_cancels_ui_poll(session):
    views, ui, model, clock = session
    views.create("research", yes=True)
    ui.submit({**SAMPLE, "name": "background-notebook"})
    assert ui.pending
    ui.choose("Stop watching")
    assert ui.pending is None
    clock[0] = 2
    assert model.get("research", "background-notebook")["state"] == "Ready"


@pytest.mark.parametrize("action", ["start", "stop", "delete"])
def test_lifecycle_cancel_keeps_instance_unchanged(session, action):
    views, ui, model, _ = session
    before = model.get("research", "demo-training")
    getattr(views, action)("research", "demo-training")
    assert ui.kind == "confirm"
    assert ("UID", before["uid"]) in ui.rows
    ui.back()
    assert model.get("research", "demo-training")["state"] == before["state"]


def test_typed_yes_changes_same_model_and_namespace(session):
    views, ui, model, _ = session
    views.dispatch("lab --cluster demo-east notebook stop demo-training --namespace research --yes")
    assert model.get("research", "demo-training")["state"] == "Stopping"
    views.dispatch("start demo-training --namespace research -y")
    assert model.get("research", "demo-training")["state"] == "Pending"
    views.dispatch("delete demo-training --namespace research --yes --json")
    assert ui.title == "Simulated JSON result"
    assert '"simulated": true' in ui.rows[0][1]
    assert not model.list_notebooks("research")
    assert len(model.list_notebooks("sandbox")) == 1


def test_unknown_outcome_exposes_receipt_and_retries_identity(session):
    views, ui, model, clock = session
    model.scenario = "unknown"
    views.create("research", yes=True)
    ui.submit({**SAMPLE, "name": "unknown-notebook"})
    receipt = model.operations()[0]
    assert receipt["state"] == "Unknown"
    assert ui.title == "Operation receipt"
    ui.choose("Retry this operation")
    assert ui.kind == "confirm"
    clock[0] = 2
    ui.accept()
    reconciled = model.operation(receipt["operation_id"])
    assert reconciled["state"] == "Ready"
    assert len(model.list_notebooks("research")) == 2


def test_rejected_create_retains_reviewable_receipt(session):
    views, ui, model, _ = session
    model.scenario = "denied"
    views.create("research", yes=True)
    ui.submit({**SAMPLE, "name": "rejected-notebook"})
    assert ui.title == "Operation receipt"
    assert ("State", "Rejected") in ui.rows
    assert model.operations()[0]["state"] == "Rejected"


def test_shell_uses_fixture_commands_and_editor_tracks_connection(session):
    views, ui, model, _ = session
    views.shell("research", "demo-training")
    ui.on_command("pwd")
    assert "$ pwd\n/workspace" in ui.description
    ui.on_command("rm -rf /")
    assert "No command was executed" in ui.description
    ui.on_command("exit")
    assert ui.title == "Notebook status"
    views.open("research", "demo-training")
    assert ui.title == "VS Code window · simulated"
    assert model.status()["clients"][0]["state"] == "connected"
    model.disconnect()
    assert model.status()["clients"][0]["state"] == "disconnected"


def test_multiple_access_targets_are_selected_explicitly(session):
    views, ui, model, _ = session
    model.scenario = "ambiguous"
    views.shell("research", "demo-training")
    assert ui.title == "Choose container"
    ui.choose("demo-training-0 / sidecar")
    assert "sidecar" in ui.description
    with pytest.raises(ValueError, match="No matching"):
        views.open("research", "demo-training", pod="other-pod")


@pytest.mark.parametrize(
    "command",
    [
        "retry",
        "status thing --namespace research --operation-id receipt",
        "shell thing --json",
        "create --timeout nan",
        "create --timeout 0",
        "list --token-stdin",
        "create --service-account forbidden",
        "delete one two",
    ],
)
def test_command_errors_stay_inside_the_ui(command):
    with pytest.raises(ValueError):
        parse_command(command)


def test_presets_cannot_be_saved_before_ready(session):
    views, _, model, _ = session
    receipt = model.create({**SAMPLE, "name": "pending-notebook"})
    with pytest.raises(ValueError, match="Ready"):
        views.save_preset(receipt["operation_id"])


def test_scope_mismatch_is_rejected(session):
    views, _, _, _ = session
    with pytest.raises(ValueError, match="End this session"):
        views.dispatch("list --namespace research --cluster demo-west")


def test_parser_errors_do_not_echo_unknown_values():
    with pytest.raises(ValueError) as error:
        parse_command("list --accidental-value sensitive-example")
    assert "sensitive-example" not in str(error.value)


def test_operation_status_reconciles_unknown_without_creating(session):
    views, ui, model, clock = session
    model.scenario = "unknown"
    receipt = model.create({**SAMPLE, "name": "lost-response"})
    before = len(model.list_notebooks("research"))
    clock[0] = 2
    views.status(operation_id=receipt["operation_id"])
    assert ("State", "Ready") in ui.rows
    assert len(model.list_notebooks("research")) == before


def test_invalid_form_values_are_not_submitted(session):
    views, ui, model, _ = session
    views.create("research")
    with pytest.raises(DemoError):
        ui.submit({**SAMPLE, "gpus": ""})
    assert ui.kind == "form"
    assert not model.operations()


def test_editor_restart_preview_has_explicit_impact_and_separate_open(session):
    views, ui, model, _ = session
    views.editor_restart("research", "demo-training")
    assert ui.kind == "confirm"
    assert "terminals may be interrupted" in ui.description
    ui.accept()
    assert "Original VS Code Server process exited" in ui.title
    assert "fixture" in ui.description.lower()
    ui.choose("Open in VS Code")
    assert "VS Code window" in ui.title


def test_editor_restart_command_preview(session):
    views, ui, _, _ = session
    views.dispatch("notebook editor-restart demo-training --namespace research --yes --json")
    assert ui.title == "Simulated JSON result"
    assert "OriginalProcessExited" in str(ui.rows)


def test_demo_stop_shows_transition_and_keeps_empty_pod_row(session):
    views, ui, model, clock = session
    model.change("research", "demo-training", "stop")
    views.status("research", "demo-training")
    assert ("State", "Stopping…") in ui.rows
    assert ("Pod", "demo-training-0 · Terminating") in ui.rows
    assert "Shell" in ui.disabled and "Start" in ui.disabled
    clock[0] += 1
    ui.choose("Refresh")
    assert ("State", "Stopped") in ui.rows
    assert ("Pod", "None — no running instance") in ui.rows
    assert "Shell" in ui.disabled and "Start" not in ui.disabled
    assert ui.actions[0][0] == "Start"


def test_demo_machine_status_uses_observed_lifecycle(session):
    import json

    views, ui, model, _ = session
    views.status("research", "demo-training", machine=True)
    data = json.loads(ui.rows[0][1])
    assert data["data"]["state"] == "Running"
    model.change("research", "demo-training", "stop")
    views.status("research", "demo-training", machine=True)
    data = json.loads(ui.rows[0][1])
    assert data["data"]["state"] == "Stopping"


def test_demo_list_has_distinct_status_cells_and_async_fill(session):
    views, ui, model, _ = session
    views.list("research")
    assert set(ui.states.values()) == {"Checking…"}
    assert ui.pending is not None
    ui.pending()
    expected = {n["uid"]: "Running" for n in model.list_notebooks("research")}
    assert ui.states == expected
    assert any(label == "demo-training" for label, _ in ui.actions)
    assert not any(" · " in label for label, _ in ui.actions)


def test_demo_lookup_failure_does_not_leave_checking_cells(session, monkeypatch):
    views, ui, model, _ = session
    views.list("research")

    def failed(*args):
        raise DemoError("Synthetic lookup failure", 4)

    monkeypatch.setattr(model, "get", failed)
    with pytest.raises(DemoError, match="lookup failure"):
        ui.pending()
    assert set(ui.states.values()) == {"Unknown"}


def test_demo_machine_list_uses_observed_states(session):
    import json

    views, ui, model, _ = session
    model.change("research", "demo-training", "stop")
    views.list("research", machine=True)
    result = json.loads(ui.rows[0][1])
    assert result["data"][0]["state"] == "Stopping"
