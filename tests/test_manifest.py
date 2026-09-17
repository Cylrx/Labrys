"""Resource, placement and storage policy compilation contracts."""

import json

import pytest
from fixtures import OPERATION_ID, inputs, profile, profile_data
from pydantic import ValidationError

from lab.config import image_parts
from lab.errors import LabError
from lab.manifest import compile_notebook, normalized_inputs


def render(data=None, **changes):
    return compile_notebook(profile(data), inputs(**changes), OPERATION_ID)["spec"]["template"][
        "spec"
    ]


def test_cpu_uses_only_cpu_placement_and_preserves_image_defaults():
    pod = render(image="other.example.invalid/project/image:v1", gpu_type="Retained-Inactive")
    container = pod["containers"][0]
    assert pod["nodeSelector"] == {"example.invalid/pool": "cpu"}
    assert container["resources"]["requests"] == {"cpu": "2000m", "memory": "4294967296"}
    assert container["resources"]["requests"] == container["resources"]["limits"]
    assert "command" not in container and "args" not in container
    assert "imagePullSecrets" not in pod


def test_gpu_ratio_derives_only_missing_resources_and_honors_custom_type():
    pod = render(gpus=3, gpu_type="Typed-Unknown-GPU", cpu=None, memory=None)
    assert pod["containers"][0]["resources"]["limits"] == {
        "cpu": "24000m",
        "memory": str(3 * 64 * 1024**3),
        "nvidia.com/gpu": "3",
    }
    assert pod["nodeSelector"] == {
        "example.invalid/pool": "gpu",
        "example.invalid/gpu-model": "Typed-Unknown-GPU",
    }
    with pytest.raises(LabError, match="GPU policy requires"):
        render(gpus=3, gpu_type="Example-GPU", cpu="1", memory=None)


def test_missing_cpu_memory_never_uses_portable_defaults():
    with pytest.raises(LabError, match="must be entered"):
        render(cpu=None, memory=None)


def test_unit_equivalent_inputs_have_identical_normalization():
    first = normalized_inputs(profile(), inputs(cpu="2", memory="1Gi"))
    second = normalized_inputs(profile(), inputs(cpu="2000m", memory="1024Mi"))
    assert first == second


def test_required_selectors_cannot_be_overridden_by_explicit_node():
    data = profile_data()
    data["namespace_rules"]["research"]["placement"]["cpu_required_selector"][
        "kubernetes.io/hostname"
    ] = "required-node"
    with pytest.raises(LabError, match="conflict"):
        render(data, node="another-node")
    assert (
        render(data, node="required-node")["nodeSelector"]["kubernetes.io/hostname"]
        == "required-node"
    )


@pytest.mark.parametrize("source", ["/shared/projects-other/data", "/shared/project", "/etc"])
def test_storage_allowlist_matches_path_components(source):
    with pytest.raises(LabError, match="allowed roots"):
        render(storage_source=source)


def test_storage_paths_are_distinct_and_host_directories_are_never_created():
    pod = render(
        storage_source="/shared/projects/example", mount_path="/data", workdir="/data/subdir"
    )
    assert pod["volumes"] == [
        {"name": "data", "hostPath": {"path": "/shared/projects/example", "type": "Directory"}}
    ]
    assert pod["containers"][0]["workingDir"] == "/data/subdir"
    assert pod["containers"][0]["volumeMounts"][0]["mountPath"] == "/data"
    with pytest.raises(ValidationError):
        inputs(storage_source="/shared/projects/../private")


def test_supplemental_mounts_are_explicit_and_duplicate_targets_fail():
    data = profile_data()
    data["namespace_rules"]["research"]["storage"]["supplemental_mounts"] = [
        {
            "name": "shared-memory",
            "mount_path": "/dev/shm",
            "medium": "memory",
            "size_limit": "1Gi",
            "read_only": True,
        }
    ]
    pod = render(data)
    assert pod["volumes"][1]["emptyDir"] == {"medium": "Memory", "sizeLimit": "1073741824"}
    assert pod["containers"][0]["volumeMounts"][1]["readOnly"] is True
    with pytest.raises(LabError, match="conflict"):
        render(data, mount_path="/dev/shm")


def test_registry_rules_match_segments_and_longest_prefix():
    data = profile_data()
    policy = data["namespace_rules"]["research"]
    policy["registry_rules"].extend(
        [
            {
                "registry": "registry.example.invalid",
                "repository_prefix": "team/private",
                "pull_secret_names": ["private-reader", "private-reader"],
            },
            {"registry": "registry.example.invalid", "pull_secret_names": ["registry-reader"]},
        ]
    )
    assert render(data, image="registry.example.invalid/team/private/image:v1")[
        "imagePullSecrets"
    ] == [{"name": "private-reader"}]
    assert render(data, image="registry.example.invalid/team-other/image:v1")[
        "imagePullSecrets"
    ] == [{"name": "registry-reader"}]
    assert "imagePullSecrets" not in render(
        data, image="registry.example.invalid.evil.invalid/team/image:v1"
    )
    policy["unmatched_registry"] = "reject"
    with pytest.raises(LabError, match="no matching registry rule"):
        render(data, image="other.example.invalid/team/image:v1")


def test_conflicting_registry_ties_are_rejected():
    data = profile_data()
    data["namespace_rules"]["research"]["registry_rules"].append(
        {
            "registry": "registry.example.invalid",
            "repository_prefix": "team",
            "pull_secret_names": ["conflicting-secret"],
        }
    )
    with pytest.raises(LabError, match="tie"):
        render(data)


def test_manifest_contains_only_supported_operational_projection():
    result = compile_notebook(profile(), inputs(), OPERATION_ID)
    encoded = json.dumps(result)
    assert "FICTIONAL_NOT_A_CREDENTIAL" not in encoded and "kubeconfig" not in encoded
    assert result["metadata"]["labels"]["lab.operations/id"] == OPERATION_ID
    assert render()["containers"][0]["command"] == ["sleep", "infinity"]


def test_unknown_namespace_requires_explicit_policy():
    with pytest.raises(LabError, match="No policy"):
        render(namespace="unknown")


@pytest.mark.parametrize(
    "image",
    [
        "https://example.invalid/image.tar",
        "/shared/images/image.tar",
        "repo/image:bad tag",
        "UPPERCASE/image",
        "repo/image@sha256:" + "a" * 32,
    ],
)
def test_non_image_sources_are_rejected(image):
    with pytest.raises(ValidationError):
        inputs(image=image)


def test_implicit_registry_and_digest_forms():
    assert image_parts("python:3.12") == ("docker.io", "library/python")
    assert image_parts("REGISTRY.example.invalid:5000/team/image@sha256:" + "a" * 64) == (
        "registry.example.invalid:5000",
        "team/image",
    )
