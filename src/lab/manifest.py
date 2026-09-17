"""Compile Notebook manifests from explicit inputs and namespace policy."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, Field

from lab.config import (
    CPU,
    OPERATION_LABEL,
    DNSName,
    Image,
    LabelValue,
    Memory,
    Model,
    NamespaceName,
    NamespaceRule,
    Path,
    image_parts,
    nonempty,
    quantity,
)
from lab.errors import LabError
from lab.policy import Profile


class NotebookInput(Model):
    name: DNSName
    namespace: NamespaceName
    owner: Annotated[LabelValue, AfterValidator(nonempty)] | None = None
    image: Image
    gpu_type: Annotated[LabelValue, AfterValidator(nonempty)] | None = None
    gpus: int = Field(default=0, ge=0, le=2**31 - 1)
    cpu: CPU | None = None
    memory: Memory | None = None
    node: DNSName | None = None
    storage_source: Path
    mount_path: Path
    workdir: Path


def normalized_inputs(profile: Profile, inputs: NotebookInput) -> NotebookInput:
    """Derive only policy-required quantities and normalize equivalent units."""
    policy = profile.namespace(inputs.namespace)
    owner = inputs.owner
    if not owner:
        raise LabError("Enter Owner in the creation form or use --owner.", code=4)
    cpu = quantity(inputs.cpu, cpu=True) if inputs.cpu is not None else None
    memory = quantity(inputs.memory) if inputs.memory is not None else None
    ratio = policy.resources.per_gpu
    if inputs.gpus and ratio is not None:
        required_cpu = quantity(ratio.cpu, cpu=True) * inputs.gpus
        required_memory = quantity(ratio.memory) * inputs.gpus
        if (
            cpu is not None
            and cpu != required_cpu
            or memory is not None
            and memory != required_memory
        ):
            raise LabError(
                f"GPU policy requires CPU {required_cpu}m and memory {required_memory} bytes",
                code=4,
            )
        cpu, memory = required_cpu, required_memory
    if cpu is None or memory is None:
        raise LabError(
            "CPU and memory must be entered; the namespace has no applicable derivation rule",
            code=4,
        )
    if max(cpu, memory) > 2**63 - 1:
        raise LabError("Derived resources exceed supported quantity bounds", code=4)
    if inputs.gpus and inputs.gpu_type is None:
        raise LabError("A GPU type is required when GPU count is positive", code=4)
    source = PurePosixPath(inputs.storage_source)
    if not any(
        source.is_relative_to(PurePosixPath(root)) for root in policy.storage.allowed_source_roots
    ):
        raise LabError("Storage source is outside the configured allowed roots", code=4)
    if policy.storage.require_same_mount_path and inputs.storage_source != inputs.mount_path:
        raise LabError("Storage policy requires source and mount path to be identical", code=4)
    if any(m.mount_path == inputs.mount_path for m in policy.storage.supplemental_mounts):
        raise LabError("Data mount target conflicts with a supplemental mount", code=4)
    return NotebookInput(
        **{**inputs.model_dump(), "owner": owner, "cpu": f"{cpu}m", "memory": str(memory)}
    )


def _selectors(*mappings: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for mapping in mappings:
        for key, value in mapping.items():
            if key in result and result[key] != value:
                raise LabError(
                    "Placement selectors conflict; "
                    "review namespace, GPU type and node requirements",
                    code=4,
                )
            result[key] = value
    return result


def pull_secrets(policy: NamespaceRule, image: str) -> list[str]:
    """Resolve namespace-local Secret names using segment-aware precedence."""
    registry, repository = image_parts(image)
    matches = [
        rule
        for rule in policy.registry_rules
        if rule.registry == registry
        and (
            rule.repository_prefix is None
            or repository == rule.repository_prefix
            or repository.startswith(rule.repository_prefix + "/")
        )
    ]
    if not matches:
        if policy.unmatched_registry == "reject":
            raise LabError("Image has no matching registry rule in this namespace", code=4)
        return []
    longest = max(len(rule.repository_prefix or "") for rule in matches)
    candidates = [
        tuple(dict.fromkeys(rule.pull_secret_names))
        for rule in matches
        if len(rule.repository_prefix or "") == longest
    ]
    if len(set(candidates)) != 1:
        raise LabError("Registry rules tie with conflicting pull Secret names", code=4)
    return list(candidates[0])


def compile_notebook(profile: Profile, inputs: NotebookInput, operation_id: str) -> dict:
    """Render the supported Kubeflow v1 Notebook policy projection."""
    inputs = normalized_inputs(profile, inputs)
    policy = profile.namespace(inputs.namespace)
    try:
        operation_id = str(UUID(operation_id))
    except (ValueError, TypeError, AttributeError):
        raise LabError("Operation ID must be a UUID", code=4) from None
    selectors = policy.placement.cpu_required_selector
    resources = {"cpu": inputs.cpu, "memory": inputs.memory}
    if inputs.gpus:
        assert inputs.gpu_type is not None
        selectors = policy.placement.gpu_required_selector
        gpu = next((gpu for gpu in policy.gpu_types if gpu.value == inputs.gpu_type), None)
        if gpu is not None:
            resource_name, gpu_selector = gpu.resource_name, gpu.selector
        elif policy.gpu_defaults is not None:
            resource_name = policy.gpu_defaults.resource_name
            gpu_selector = {policy.gpu_defaults.type_label: inputs.gpu_type}
        else:
            raise LabError(
                "Custom GPU type requires an operator-configured gpu_defaults mapping", code=4
            )
        selectors = _selectors(selectors, gpu_selector)
        resources[resource_name] = str(inputs.gpus)
    selectors = _selectors(
        selectors, {"kubernetes.io/hostname": inputs.node} if inputs.node else {}
    )
    assert inputs.owner is not None
    owner_key = profile.owner_label_key
    labels = {owner_key: inputs.owner, OPERATION_LABEL: operation_id}
    volumes = [{"name": "data", "hostPath": {"path": inputs.storage_source, "type": "Directory"}}]
    mounts = [
        {"name": "data", "mountPath": inputs.mount_path, "readOnly": policy.storage.mount_read_only}
    ]
    for mount in policy.storage.supplemental_mounts:
        empty_dir = {"sizeLimit": str(quantity(mount.size_limit))}
        if mount.medium == "memory":
            empty_dir["medium"] = "Memory"
        volumes.append({"name": mount.name, "emptyDir": empty_dir})
        mounts.append(
            {"name": mount.name, "mountPath": mount.mount_path, "readOnly": mount.read_only}
        )
    container = {
        "name": "main",
        "image": inputs.image,
        "workingDir": inputs.workdir,
        "resources": {"requests": resources.copy(), "limits": resources.copy()},
        "volumeMounts": mounts,
    }
    startup = policy.runtime.exact_images.get(inputs.image, policy.runtime.default)
    if startup.mode == "command_override":
        assert startup.command is not None and startup.args is not None
        container["command"] = list(startup.command)
        container["args"] = list(startup.args)
    pod = {"containers": [container], "volumes": volumes}
    if selectors:
        pod["nodeSelector"] = selectors
    secrets = pull_secrets(policy, inputs.image)
    if secrets:
        pod["imagePullSecrets"] = [{"name": name} for name in secrets]
    return {
        "apiVersion": "kubeflow.org/v1",
        "kind": "Notebook",
        "metadata": {"name": inputs.name, "namespace": inputs.namespace, "labels": labels.copy()},
        "spec": {"template": {"metadata": {"labels": labels.copy()}, "spec": pod}},
    }
