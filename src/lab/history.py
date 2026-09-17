"""Scoped field history, explicit presets, and durable operation receipts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from lab.errors import LabError
from lab.storage import EncryptedStore, Paths

EDITABLE_FIELDS = frozenset(
    {
        "image",
        "owner",
        "gpu_type",
        "gpus",
        "cpu",
        "memory",
        "node",
        "storage_source",
        "mount_path",
        "workdir",
    }
)
HISTORY_FIELDS = EDITABLE_FIELDS | {"name", "namespace"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Value(_Model):
    value: str
    last_used_at: str


class _FieldHistory(_Model):
    cluster_id: str
    namespace: str | None
    field: str
    values: list[_Value] = Field(max_length=100)


class _Seen(_Model):
    cluster_id: str
    namespace: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    first_presented_at: str


class _Receipt(_Model):
    operation_id: str
    cluster_id: str
    namespace: str
    name: str
    operation: Literal["create"]
    inputs: dict
    manifest: dict
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal[
        "Prepared", "Sending", "Accepted", "Ready", "RuntimeFailed", "Rejected", "Unknown"
    ]
    created_at: str
    updated_at: str
    uid: str | None = None


class _Preset(_Model):
    id: str
    name: str = Field(min_length=1)
    cluster_id: str
    namespace: str
    editable_fields: dict
    created_at: str
    updated_at: str

    @field_validator("id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        UUID(value)
        return value


class _History(_Model):
    field_history: list[_FieldHistory]
    seen_configurations: list[_Seen]
    operations: dict[str, _Receipt]


class _Presets(_Model):
    presets: list[_Preset]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fields(fields: dict, *, instance: bool = False) -> dict:
    allowed = HISTORY_FIELDS if instance else EDITABLE_FIELDS
    if not isinstance(fields, dict) or fields.keys() - allowed:
        raise ValueError("Unsupported creation fields")
    for key, value in fields.items():
        if key == "gpus":
            if type(value) is not int or value < 0:
                raise ValueError("GPU count must be nonnegative")
        elif value is not None and not isinstance(value, str):
            raise ValueError("Creation values must be strings")
    return fields


def _distinct(values: list) -> None:
    if len(values) != len(set(values)):
        raise ValueError("Duplicate state identity")


def _manifest(manifest: dict) -> None:
    # The receipt only retains the operational projection supported by the renderer.
    def check(value: object, schema: object) -> None:
        if schema is str:
            if not isinstance(value, str):
                raise ValueError("Expected manifest string")
        elif schema is bool:
            if type(value) is not bool:
                raise ValueError("Expected manifest boolean")
        elif schema == "labels":
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError("Expected manifest string map")
        elif isinstance(schema, list):
            if not isinstance(value, list):
                raise ValueError("Expected manifest list")
            for entry in value:
                check(entry, schema[0])
        elif isinstance(schema, dict):
            if not isinstance(value, dict) or value.keys() - schema.keys():
                raise ValueError("Unsupported manifest fields")
            for key, entry in value.items():
                check(entry, schema[key])

    mount = {"name": str, "mountPath": str, "readOnly": bool}
    container = {
        "name": str,
        "image": str,
        "imagePullPolicy": str,
        "workingDir": str,
        "command": [str],
        "args": [str],
        "resources": {"requests": "labels", "limits": "labels"},
        "volumeMounts": [mount],
    }
    volume = {
        "name": str,
        "hostPath": {"path": str, "type": str},
        "emptyDir": {"medium": str, "sizeLimit": str},
    }
    metadata = {"name": str, "namespace": str, "labels": "labels", "annotations": "labels"}
    pod = {
        "containers": [container],
        "nodeSelector": "labels",
        "volumes": [volume],
        "imagePullSecrets": [{"name": str}],
        "restartPolicy": str,
    }
    check(
        manifest,
        {
            "apiVersion": str,
            "kind": str,
            "metadata": metadata,
            "spec": {"template": {"metadata": metadata, "spec": pod}},
        },
    )


def validate_data(kind: str, data: dict) -> None:
    """Validate complete decrypted state without exposing its contents in errors."""
    try:
        if kind == "history":
            state = _History.model_validate(data)
            _distinct(
                [(item.cluster_id, item.namespace, item.field) for item in state.field_history]
            )
            _distinct(
                [
                    (item.cluster_id, item.namespace, item.fingerprint)
                    for item in state.seen_configurations
                ]
            )
            for item in state.field_history:
                if item.field not in HISTORY_FIELDS or (item.field == "namespace") != (
                    item.namespace is None
                ):
                    raise ValueError("Invalid history scope")
                _distinct([entry.value for entry in item.values])
            for operation_id, receipt in state.operations.items():
                UUID(operation_id)
                if operation_id != receipt.operation_id:
                    raise ValueError("Receipt identity mismatch")
                _fields(receipt.inputs, instance=True)
                _manifest(receipt.manifest)
        else:
            presets = _Presets.model_validate(data)
            _distinct([item.id for item in presets.presets])
            _distinct([(item.cluster_id, item.namespace, item.name) for item in presets.presets])
            for preset in presets.presets:
                _fields(preset.editable_fields)
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise LabError(
            "Encrypted local state has invalid or unsupported data; preserve it for recovery.",
            code=8,
        ) from None


class Repository:
    """Persist user data scoped by stable cluster identity and namespace.

    :param paths: Platform-specific state locations.
    :param key: Verified Fernet key shared by the two encrypted stores.
    """

    def __init__(self, paths: Paths, key: bytes):
        self.history = EncryptedStore(paths.history_path, key, "history")
        self.preset_store = EncryptedStore(paths.presets_path, key, "presets")

    def verify(self) -> None:
        """Authenticate and validate all existing local encrypted documents."""
        self.history.read()
        self.preset_store.read()

    def record_operation(self, receipt: dict) -> None:
        """Write a new operation receipt durably before remote submission."""

        def change(data: dict) -> None:
            operation_id = receipt.get("operation_id")
            if operation_id in data["operations"]:
                raise LabError("An operation with this identity already exists.", code=8)
            data["operations"][operation_id] = deepcopy(receipt)

        self.history.update(change)

    def operation(self, operation_id: str) -> dict:
        """Return a receipt; interrupted Sending is observed as Unknown."""
        receipt = self.history.read()["operations"].get(operation_id)
        if receipt is None:
            raise LabError("No saved operation matches this identity.", code=8)
        if receipt["state"] == "Sending":
            receipt["state"] = "Unknown"
        return receipt

    def update_operation(self, operation_id: str, **updates: object) -> None:
        """Update receipt status and server identity without replacing its request."""
        if updates.keys() - {"state", "uid", "updated_at"}:
            raise LabError("Only operation state, UID and update time can change.", code=8)

        def change(data: dict) -> None:
            if operation_id not in data["operations"]:
                raise LabError("No saved operation matches this identity.", code=8)
            data["operations"][operation_id].update({"updated_at": _now(), **updates})

        self.history.update(change)

    def remember(self, cluster_id: str, namespace: str, fields: dict) -> None:
        """Record accepted creation values, retaining 100 MRU values per field."""
        try:
            _fields(fields, instance=True)
        except ValueError:
            raise LabError("Cannot save unsupported creation fields.", code=8) from None

        def change(data: dict) -> None:
            now = _now()
            for field, value in {**fields, "namespace": namespace}.items():
                if value is None or value == "":
                    continue
                scope = None if field == "namespace" else namespace
                entry = next(
                    (
                        item
                        for item in data["field_history"]
                        if (item["cluster_id"], item["namespace"], item["field"])
                        == (cluster_id, scope, field)
                    ),
                    None,
                )
                if entry is None:
                    entry = {
                        "cluster_id": cluster_id,
                        "namespace": scope,
                        "field": field,
                        "values": [],
                    }
                    data["field_history"].append(entry)
                values = [item for item in entry["values"] if item["value"] != str(value)]
                entry["values"] = [{"value": str(value), "last_used_at": now}, *values][:100]

        self.history.update(change)

    def candidates(self, cluster_id: str, namespace: str, field: str) -> list[str]:
        """Return the most recently used values first for the requested scope."""
        scope = None if field == "namespace" else namespace
        for item in self.history.read()["field_history"]:
            if (item["cluster_id"], item["namespace"], item["field"]) == (cluster_id, scope, field):
                return [entry["value"] for entry in item["values"]]
        return []

    def fingerprint(self, cluster_id: str, namespace: str, fields: dict) -> str:
        """Hash normalized editable inputs, excluding the instance name.

        The caller supplies quantity-normalized field values.
        """
        editable = {key: value for key, value in fields.items() if key not in {"name", "namespace"}}
        try:
            _fields(editable)
        except ValueError:
            raise LabError("Cannot fingerprint unsupported creation fields.", code=8) from None
        canonical = json.dumps(
            {"cluster_id": cluster_id, "namespace": namespace, "editable_fields": editable},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def mark_seen(self, cluster_id: str, namespace: str, fields: dict) -> bool:
        """Mark a configuration when its prompt is shown; return whether it is new."""
        fingerprint = self.fingerprint(cluster_id, namespace, fields)
        fresh = False

        def change(data: dict) -> None:
            nonlocal fresh
            if any(item["fingerprint"] == fingerprint for item in data["seen_configurations"]):
                return
            data["seen_configurations"].append(
                {
                    "cluster_id": cluster_id,
                    "namespace": namespace,
                    "fingerprint": fingerprint,
                    "first_presented_at": _now(),
                }
            )
            fresh = True

        self.history.update(change)
        return fresh

    def presets(self, cluster_id: str, namespace: str) -> list[dict]:
        """List saved templates for exactly one cluster and namespace."""
        return [
            item
            for item in self.preset_store.read()["presets"]
            if (item["cluster_id"], item["namespace"]) == (cluster_id, namespace)
        ]

    def save_preset(
        self, cluster_id: str, namespace: str, name: str, fields: dict, preset_id: str | None = None
    ) -> dict:
        """Save a new template, or explicitly update a scoped existing identity."""
        editable = {key: value for key, value in fields.items() if key not in {"name", "namespace"}}
        self.fingerprint(cluster_id, namespace, editable)
        saved = {}

        def change(data: dict) -> None:
            now = _now()
            existing = next((item for item in data["presets"] if item["id"] == preset_id), None)
            if preset_id is not None and (
                existing is None
                or (existing["cluster_id"], existing["namespace"]) != (cluster_id, namespace)
            ):
                raise LabError("No preset with this identity exists in the selected scope.", code=8)
            if any(
                (item["cluster_id"], item["namespace"], item["name"])
                == (cluster_id, namespace, name)
                and item["id"] != preset_id
                for item in data["presets"]
            ):
                raise LabError(
                    "A preset with this name already exists in the selected scope.", code=8
                )
            saved.update(
                {
                    "id": preset_id or str(uuid4()),
                    "name": name,
                    "cluster_id": cluster_id,
                    "namespace": namespace,
                    "editable_fields": deepcopy(editable),
                    "created_at": existing["created_at"] if existing else now,
                    "updated_at": now,
                }
            )
            if existing is None:
                data["presets"].append(saved)
            else:
                existing.update(saved)

        self.preset_store.update(change)
        return deepcopy(saved)
