"""Fictional input builders shared by local tests."""

from pathlib import Path

import yaml

from lab.config import parse_cluster
from lab.manifest import NotebookInput
from lab.policy import parse_profile

CLUSTER_ID = "research-example"
OPERATION_ID = "06356e07-d3fa-4ef9-971f-9df835bfecef"
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def cluster_data():
    return yaml.safe_load((EXAMPLES / "connection.yaml").read_text())


def cluster(data=None):
    return parse_cluster(yaml.safe_dump(cluster_data() if data is None else data), CLUSTER_ID)


def profile_data():
    return yaml.safe_load((Path(__file__).parent / "profile.yaml").read_text())


def profile(data=None):
    return parse_profile(yaml.safe_dump(profile_data() if data is None else data))


def profiles_dir(tmp_path, data=None):
    directory = tmp_path / "profiles"
    directory.mkdir(exist_ok=True)
    (directory / f"{CLUSTER_ID}.yaml").write_text(
        yaml.safe_dump(profile_data() if data is None else data)
    )
    return directory


def inputs(**changes):
    return NotebookInput(
        **{
            "name": "example-notebook",
            "owner": "example.researcher",
            "namespace": "research",
            "image": "registry.example.invalid/team/python:example",
            "gpus": 0,
            "cpu": "2",
            "memory": "4Gi",
            "storage_source": "/shared/projects/example",
            "mount_path": "/work",
            "workdir": "/work",
            **changes,
        }
    )
