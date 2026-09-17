"""Scope, receipt, and explicit template behavior."""

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from lab.errors import LabError
from lab.history import Repository
from lab.storage import Paths


@pytest.fixture
def repository(tmp_path):
    return Repository(
        Paths(tmp_path / "config", tmp_path / "data", tmp_path / "state"), Fernet.generate_key()
    )


def test_history_scopes_and_mru_bound(repository):
    for number in range(102):
        repository.remember("cluster", "research", {"image": f"image:{number}"})
    repository.remember("cluster", "research", {"image": "image:9"})
    values = repository.candidates("cluster", "research", "image")
    assert len(values) == 100
    assert values[:2] == ["image:9", "image:101"]
    assert "image:0" not in values
    assert repository.candidates("other", "research", "image") == []
    assert repository.candidates("cluster", "other", "image") == []
    assert repository.candidates("cluster", "other", "namespace") == ["research"]


def test_seen_identity_excludes_instance_name(repository):
    first = {"name": "first", "image": "image:one", "cpu": "1"}
    second = {**first, "name": "second"}
    assert repository.mark_seen("cluster", "research", first)
    assert not repository.mark_seen("cluster", "research", second)
    assert repository.mark_seen("cluster", "other", second)
    assert repository.mark_seen("other", "research", second)


def test_presets_require_explicit_update_and_drop_instance_name(repository):
    fields = {"name": "instance", "namespace": "research", "image": "image:one"}
    preset = repository.save_preset("cluster", "research", "template", fields)
    assert preset["editable_fields"] == {"image": "image:one"}
    fields["image"] = "image:two"
    assert repository.presets("cluster", "research")[0]["editable_fields"]["image"] == "image:one"
    with pytest.raises(LabError, match="already exists"):
        repository.save_preset("cluster", "research", "template", fields)
    updated = repository.save_preset("cluster", "research", "template", fields, preset["id"])
    assert updated["created_at"] == preset["created_at"]
    assert updated["editable_fields"]["image"] == "image:two"
    with pytest.raises(LabError, match="selected scope"):
        repository.save_preset("other", "research", "template", fields, preset["id"])


def test_receipt_lifecycle_and_immutable_request(repository):
    operation_id = str(uuid4())
    receipt = {
        "operation_id": operation_id,
        "cluster_id": "cluster",
        "namespace": "research",
        "name": "example",
        "operation": "create",
        "inputs": {"image": "example:1"},
        "manifest": {"apiVersion": "kubeflow.org/v1", "kind": "Notebook"},
        "request_digest": "a" * 64,
        "state": "Prepared",
        "created_at": "2026-09-14T00:00:00Z",
        "updated_at": "2026-09-14T00:00:00Z",
    }
    repository.record_operation(receipt)
    with pytest.raises(LabError, match="already exists"):
        repository.record_operation(receipt)
    repository.update_operation(operation_id, state="Sending")
    assert repository.operation(operation_id)["state"] == "Unknown"
    repository.update_operation(operation_id, state="Accepted", uid="server-uid")
    assert repository.operation(operation_id)["uid"] == "server-uid"
    with pytest.raises(LabError, match="Only operation"):
        repository.update_operation(operation_id, inputs={"image": "changed"})


def test_credentials_cannot_be_fields_or_manifest_environment(repository):
    with pytest.raises(LabError):
        repository.remember("cluster", "research", {"token": "forbidden"})
    with pytest.raises(LabError):
        repository.save_preset(
            "cluster", "research", "bad", {"environment": {"SECRET": "forbidden"}}
        )
    operation_id = str(uuid4())
    receipt = {
        "operation_id": operation_id,
        "cluster_id": "cluster",
        "namespace": "research",
        "name": "example",
        "operation": "create",
        "inputs": {},
        "manifest": {"spec": {"template": {"spec": {"containers": [{"env": []}]}}}},
        "request_digest": "a" * 64,
        "state": "Prepared",
        "created_at": "now",
        "updated_at": "now",
    }
    with pytest.raises(LabError):
        repository.record_operation(receipt)
    assert repository.history.read()["operations"] == {}
