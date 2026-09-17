"""Behavioral checks for the isolated, synthetic lab environment."""

import ast
import importlib.util
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "demo" / "model.py"
SPEC = importlib.util.spec_from_file_location("demo_model_under_test", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
Model, DemoError, quantity = MODULE.Model, MODULE.DemoError, MODULE.quantity


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def model(clock):
    value = Model(fresh=True, clock=clock)
    value.authorize("demo-east")
    return value


@pytest.fixture
def fields():
    return {**MODULE.SAMPLE, "name": "my-notebook"}


def test_quantity_equivalence_and_exactness():
    assert quantity("1", cpu=True) == quantity("1000m", cpu=True) == 1000
    assert quantity("1Gi") == quantity("1024Mi") == 1073741824
    assert quantity("1e3", cpu=True) == 1000000
    for value in ("0", "-1", "1e100", "0.1m", "nan", "999999999999999999999999"):
        with pytest.raises(DemoError):
            quantity(value, cpu=True)


def test_explicit_cpu_form_and_gpu_derivation(model, fields):
    gpu = model.normalize({**fields, "cpu": "", "memory": ""})
    assert gpu["cpu"] == "8000m"
    assert gpu["memory"] == str(32 * 1024**3)
    assert model.normalize({**fields, "cpu": "8", "memory": "32Gi"}) == gpu
    for override in (
        {"gpus": ""},
        {"gpus": -1},
        {"gpus": 1.5},
        {"gpus": True},
        {"cpu": "4"},
        {"gpu_type": ""},
    ):
        with pytest.raises(DemoError):
            model.normalize({**fields, **override})
    with pytest.raises(DemoError, match="CPU and memory"):
        model.normalize({**fields, "gpus": 0, "cpu": "", "memory": ""})
    assert model.normalize({**fields, "gpus": 0, "cpu": "500m", "memory": "1Gi"})["cpu"] == "500m"


def test_creation_validation_keeps_storage_scoped(model, fields):
    for override in (
        {"name": "Bad Name"},
        {"storage_source": "/real/data"},
        {"storage_source": "/demo/data/../secret"},
        {"mount_path": "/dev/shm"},
        {"workdir": "relative"},
        {"namespace": "default"},
        {"image": "https://real.invalid/x"},
    ):
        with pytest.raises(DemoError):
            model.normalize({**fields, **override})
    assert model.preview(fields)[-1] == ("Effect", "In-memory simulation only")


def test_cold_history_and_presets_stay_empty_until_explicit_actions(model, fields):
    assert model.list_notebooks("research") == []
    assert model.candidates("research", "image") == []
    assert model.presets("research") == []
    model.create(fields)
    assert model.candidates("research", "image") == [fields["image"]]
    assert model.candidates("sandbox", "image") == []
    assert model.candidates("", "namespace") == ["research"]
    assert model.presets("research") == []


def test_notebook_lifecycle_and_receipt_identity(model, fields, clock):
    receipt = model.create(fields)
    assert receipt["state"] == "Accepted"
    assert model.get("research", fields["name"])["state"] == "Pending"
    clock.advance(0.6)
    assert model.poll(receipt["id"])["state"] == "Ready"
    assert model.change("research", fields["name"], "start")["status"] == "Start already requested"
    model.change("research", fields["name"], "stop")
    stopping = model.get("research", fields["name"])
    assert stopping["stopped"] is True
    assert stopping["pods"][0]["state"] == "Terminating"
    clock.advance(0.6)
    assert model.get("research", fields["name"])["pods"] == []
    assert model.change("research", fields["name"], "stop")["status"] == "Stop already requested"
    assert model.change("research", fields["name"], "start")["status"] == "Starting"
    clock.advance(0.6)
    assert model.get("research", fields["name"])["uid"] == receipt["uid"]
    model.change("research", fields["name"], "delete")
    assert model.list_notebooks("research") == []
    assert model.operation(receipt["id"])["id"] == receipt["id"]


def test_unknown_retry_reconciles_the_same_object(clock, fields):
    model = Model(fresh=True, scenario="unknown", clock=clock)
    model.authorize("demo-east")
    receipt = model.create(fields)
    assert receipt["state"] == "Unknown" and receipt["uid"] is None
    uid = model.get("research", fields["name"])["uid"]
    clock.advance(1)
    assert model.poll(receipt["id"])["state"] == "Unknown"
    recovered = model.retry(receipt["id"])
    assert recovered["id"] == receipt["id"]
    assert recovered["uid"] == uid
    assert recovered["state"] == "Ready"
    assert len(model.list_notebooks("research")) == 1
    assert len(model.operations()) == 1


def test_duplicate_receipt_cannot_adopt_another_operation(model, fields):
    original = model.create(fields)
    with pytest.raises(DemoError, match="different operation") as raised:
        model.create(fields)
    rejected_id = raised.value.data["operation_id"]
    assert rejected_id != original["id"]
    with pytest.raises(DemoError, match="different operation"):
        model.retry(rejected_id)
    assert model.get("research", fields["name"])["uid"] == original["uid"]


def test_reconciliation_observes_existing_objects_without_recreating(clock, fields):
    model = Model(fresh=True, scenario="unknown", clock=clock)
    model.authorize("demo-east")
    receipt = model.create(fields)
    clock.advance(1)
    assert model.reconcile(receipt["id"])["state"] == "Ready"
    model.change("research", fields["name"], "delete")
    with pytest.raises(DemoError, match="not found"):
        model.reconcile(receipt["id"])
    assert model.list_notebooks("research") == []
    assert model.operation(receipt["id"])["state"] == "Ready"


def test_scoped_presets_and_normalized_prompt_deduplication(model, fields):
    assert model.mark_seen(fields) is True
    assert model.mark_seen({**fields, "name": "renamed", "cpu": "8", "memory": "32Gi"}) is False
    saved = model.save_preset("research", "Training", fields)
    assert "name" not in saved["fields"] and "namespace" not in saved["fields"]
    assert saved["fields"]["cpu"] == "8000m"
    with pytest.raises(DemoError, match="already exists"):
        model.save_preset("research", "Training", fields)
    updated = model.save_preset("research", "Training v2", fields, saved["id"])
    assert updated["id"] == saved["id"]
    with pytest.raises(DemoError, match="selected scope"):
        model.save_preset("sandbox", "Training", fields, saved["id"])
    model.authorize("demo-west")
    assert model.presets("research") == []
    assert model.mark_seen(fields) is True


def test_expiry_and_explicit_disconnect_isolate_editor_sessions(model, fields, clock):
    model.create(fields)
    clock.advance(1)
    old = model.open_editor("research", fields["name"])
    model.interrupt()
    assert model.state == "disconnected"
    assert model.status()["clients"][0]["state"] == "disconnected"
    model.reconnect()
    assert model.status()["clients"][0]["state"] == "connected"
    model.disconnect()
    with pytest.raises(DemoError, match="Authorization has ended"):
        model.reconnect()
    model.authorize("demo-east")
    with pytest.raises(DemoError, match="Already connected"):
        model.reconnect()
    model.interrupt()
    model.reconnect()
    assert model.status()["clients"][0]["state"] == "disconnected"
    new = model.open_editor("research", fields["name"])
    assert new["client_id"] != old["client_id"]
    clock.advance(model.ttl + 1)
    assert model.status()["state"] == "unauthorized"
    assert model.status()["ttl_remaining"] == 0
    assert all(client["state"] == "disconnected" for client in model.status()["clients"])
    model.authorize("demo-east")
    assert model.get("research", fields["name"])["state"] == "Ready"


@pytest.mark.parametrize(
    "scenario,expected", [("pending", "Accepted"), ("failed", "RuntimeFailed")]
)
def test_runtime_scenarios(clock, fields, scenario, expected):
    model = Model(fresh=True, scenario=scenario, clock=clock)
    model.authorize("demo-east")
    receipt = model.create(fields)
    clock.advance(10)
    assert model.poll(receipt["id"])["state"] == expected


def test_offline_and_denied_scenarios(clock, fields):
    offline = Model(scenario="offline", clock=clock)
    assert offline.authorize("demo-east")["state"] == "disconnected"
    assert offline.status()["authorized"]
    with pytest.raises(DemoError, match="disconnected"):
        offline.list_notebooks("research")
    deadline = offline.status()["deadline"]
    assert offline.reconnect()["state"] == "connected"
    assert offline.status()["deadline"] == deadline
    denied = Model(scenario="denied", clock=clock)
    denied.authorize("demo-east")
    with pytest.raises(DemoError, match="RBAC") as raised:
        denied.create(fields)
    assert denied.operation(raised.value.data["id"])["state"] == "Rejected"


def test_ambiguous_targets_and_literal_shell_commands(clock):
    model = Model(scenario="ambiguous", clock=clock)
    model.authorize("demo-east")
    assert len(model.targets("research", "demo-training")) == 3
    with pytest.raises(DemoError, match="Several"):
        model.open_editor("research", "demo-training")
    selected = {"pod": "demo-training-0", "container": "main"}
    assert model.open_editor("research", "demo-training", **selected)["pod"] == selected["pod"]
    assert model.shell("research", "demo-training", "pwd", **selected) == "/workspace"
    assert "No command was executed" in model.shell(
        "research", "demo-training", "pwd; touch /tmp/unsafe", **selected
    )
    assert "SIMULATED" in model.shell("research", "demo-training", "nvidia-smi", **selected)


def test_returned_records_cannot_mutate_model_state(model, fields):
    receipt = model.create(fields)
    receipt["inputs"]["name"] = "changed"
    listed = model.list_notebooks("research")
    listed[0]["fields"]["image"] = "changed"
    assert model.operation(receipt["id"])["inputs"]["name"] == fields["name"]
    assert model.get("research", fields["name"])["fields"]["image"] == fields["image"]


def test_backend_imports_only_pure_standard_library_modules():
    tree = ast.parse(SOURCE.read_text())
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert modules <= {"__future__", "copy", "decimal", "pathlib", "re", "time"}
    forbidden = {"open", "exec", "eval", "compile", "__import__"}
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden
        for node in ast.walk(tree)
    )
