"""Notebook lifecycle distinguishes requested state from observed runtime state."""

import pytest

from lab.notebook_status import notebook_state, pod_rows, resource_summary, status_actions


@pytest.mark.parametrize(
    "stopped,states,expected",
    [
        (True, ["Ready"], "Stopping"),
        (True, ["Terminating"], "Stopping"),
        (True, ["Failed"], "Stopping"),
        (True, ["Unknown"], "Stopping"),
        (True, [], "Stopped"),
        (False, [], "Starting"),
        (False, ["Pending"], "Starting"),
        (False, ["Running"], "Starting"),
        (False, ["Terminating"], "Starting"),
        (False, ["Ready"], "Running"),
        (False, ["Ready", "Terminating"], "Running"),
        (False, ["Ready", "Unknown"], "Running"),
        (False, ["Unknown"], "Unknown"),
        (False, ["Ready", "CrashLoopBackOff"], "Running"),
        (False, ["CrashLoopBackOff"], "Error"),
        (False, ["ImagePullBackOff"], "Error"),
        (False, ["Failed"], "Error"),
        (False, ["Succeeded"], "Error"),
    ],
)
def test_lifecycle(stopped, states, expected):
    assert notebook_state(stopped, [{"state": value} for value in states]) == expected


@pytest.mark.parametrize("state", ["Starting", "Stopping", "Stopped", "Error", "Unknown"])
def test_connections_require_running_state(state):
    actions, disabled = status_actions(state)
    assert {"shell", "open", "editor-restart"} <= disabled.keys()
    assert {"status", "back", "delete"}.isdisjoint(disabled)
    assert ("start" not in disabled) == (state == "Stopped")
    assert ("stop" in disabled) == (state in {"Stopped", "Stopping"})
    if state == "Stopped":
        assert actions[0] == ("start", "Start")


def test_running_actions_and_explicit_empty_instance():
    assert status_actions("Running")[1] == {"start": "Running is already requested"}
    assert pod_rows([]) == [("Pod", "None — no running instance")]
    assert pod_rows([{"name": "example-0", "state": "Terminating"}]) == [
        ("Pod", "example-0 · Terminating")
    ]


def test_startup_resources_use_readable_units_without_losing_other_requests():
    assert resource_summary({"cpu": "30000m", "memory": "283467841536", "nvidia.com/gpu": "1"}) == (
        "CPU 30 · Memory 264 GiB · GPU 1"
    )
    assert resource_summary({"cpu": "500m", "memory": "512Mi", "example.com/device": "2"}) == (
        "CPU 0.5 · Memory 512 MiB · example.com/device: 2"
    )
    assert resource_summary({}) == "Not specified"
