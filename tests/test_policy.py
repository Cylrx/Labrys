"""Private creation profiles are selected only by registered cluster identity."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fixtures import CLUSTER_ID, EXAMPLES, OPERATION_ID, inputs, profile, profile_data, profiles_dir

from lab.config import MAX_DOCUMENT_BYTES, OPERATION_LABEL
from lab.errors import LabError
from lab.manifest import compile_notebook, normalized_inputs
from lab.policy import load_profile, parse_profile, profile_path


def test_profile_routes_by_exact_readable_name(tmp_path):
    directory = profiles_dir(tmp_path)
    alternate = profile_data()
    alternate["owner_label_key"] = "different-owner"
    (directory / "research-other.yaml").write_text(yaml.safe_dump(alternate))
    assert load_profile(directory, CLUSTER_ID).owner_label_key == "owner"
    assert load_profile(directory, "research-other").owner_label_key == "different-owner"
    assert profile_path(directory, CLUSTER_ID) == directory / f"{CLUSTER_ID}.yaml"


def test_missing_profile_never_searches_other_names_or_directories(tmp_path, monkeypatch):
    directory = tmp_path / "selected"
    directory.mkdir()
    (directory / "template.yaml").write_text(yaml.safe_dump(profile_data()))
    profiles_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(LabError, match="Profile not found") as caught:
        load_profile(directory, CLUSTER_ID)
    assert str(directory / f"{CLUSTER_ID}.yaml") in str(caught.value)


@pytest.mark.parametrize("identity", ["Research", "../template", "template", "", None])
def test_invalid_profile_identity_is_a_user_error(tmp_path, identity):
    with pytest.raises(LabError, match="Cluster name"):
        load_profile(tmp_path, identity)


def test_profile_directory_must_be_absolute():
    with pytest.raises(LabError, match="absolute"):
        profile_path(Path("profiles"), CLUSTER_ID)


@pytest.mark.parametrize("content", [b"\xff", b"x" * (MAX_DOCUMENT_BYTES + 1)])
def test_profile_file_must_be_bounded_utf8(tmp_path, content):
    profile_path(tmp_path, CLUSTER_ID).write_bytes(content)
    with pytest.raises(LabError, match="UTF-8|1 MiB"):
        load_profile(tmp_path, CLUSTER_ID)


def test_directory_is_not_a_profile(tmp_path):
    profile_path(tmp_path, CLUSTER_ID).mkdir()
    with pytest.raises(LabError, match="regular file"):
        load_profile(tmp_path, CLUSTER_ID)


def test_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    os.mkfifo(profile_path(tmp_path, CLUSTER_ID))
    script = """
import sys
from pathlib import Path
from lab.errors import LabError
from lab.policy import load_profile
try:
    load_profile(Path(sys.argv[1]), sys.argv[2])
except LabError as error:
    assert 'regular file' in str(error)
else:
    raise AssertionError('FIFO was accepted')
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path), CLUSTER_ID],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("text", ["", "{}", "owner_label_key: owner\nnamespace_rules: {}"])
def test_empty_profiles_are_rejected(text):
    with pytest.raises(LabError):
        parse_profile(text)


def test_owner_key_cannot_replace_operation_identity():
    data = profile_data()
    data["owner_label_key"] = OPERATION_LABEL
    with pytest.raises(LabError):
        profile(data)


def test_namespace_requires_explicit_policy():
    selected = profile()
    assert selected.namespace("research") == selected.namespace_rules["research"]
    with pytest.raises(LabError, match="No policy"):
        selected.namespace("other-namespace")


def test_owner_is_explicit_and_propagated_to_both_label_sets():
    with pytest.raises(LabError, match="Enter Owner"):
        normalized_inputs(profile(), inputs(owner=None))
    result = compile_notebook(profile(), inputs(owner="another.researcher"), OPERATION_ID)
    assert result["metadata"]["labels"] == result["spec"]["template"]["metadata"]["labels"]
    assert result["metadata"]["labels"]["owner"] == "another.researcher"


def test_profile_refresh_applies_runtime_changes(tmp_path):
    data = profile_data()
    directory = profiles_dir(tmp_path, data)
    previous = load_profile(directory, CLUSTER_ID)
    data["namespace_rules"]["research"]["runtime"]["exact_images"][inputs().image]["args"] = [
        "updated"
    ]
    profile_path(directory, CLUSTER_ID).write_text(yaml.safe_dump(data))
    refreshed = load_profile(directory, CLUSTER_ID)
    for selected, expected in [(previous, []), (refreshed, ["updated"])]:
        result = compile_notebook(selected, inputs(), OPERATION_ID)
        assert result["spec"]["template"]["spec"]["containers"][0]["args"] == expected


def test_public_template_is_a_valid_profile():
    template = EXAMPLES.parent / "profiles" / "template.yaml"
    assert parse_profile(template.read_text()).namespace_rules


@pytest.mark.parametrize("field", ["server", "cluster_id", "owner_label", "schema_version"])
def test_profile_rejects_connection_metadata_and_owner_defaults(field):
    data = profile_data()
    data[field] = "SECRET_VALUE"
    with pytest.raises(LabError) as caught:
        profile(data)
    assert "SECRET_VALUE" not in str(caught.value)
