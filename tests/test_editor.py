"""The editor receives one explicitly selected grant and public kubeconfig."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lab import editor
from lab.errors import LabError
from lab.tools import Toolchain


@pytest.fixture
def editor_session(tmp_path, monkeypatch):
    code, kubectl = tmp_path / "code", tmp_path / "kubectl"
    for path in (code, kubectl):
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
    config = tmp_path / "public-config"
    config.write_text(json.dumps({"current-context": "unique-grant-context"}))
    grant = SimpleNamespace(
        config_path=config, environment={"LAB_GRANT_CAPABILITY": "synthetic-cap"}
    )
    session = SimpleNamespace(create_grant=AsyncMock(return_value=grant), revoke_grant=AsyncMock())
    service = SimpleNamespace(api=SimpleNamespace(session=session))
    notebook = {"metadata": {"name": "nb", "namespace": "research"}}
    pod = {
        "metadata": {"name": "pod"},
        "spec": {
            "containers": [
                {"name": "main", "image": "example.invalid/image:v1", "workingDir": "/work"}
            ]
        },
    }
    monkeypatch.setattr(editor, "target", AsyncMock(return_value=(pod, "main")))
    monkeypatch.setattr(editor, "prerequisites", AsyncMock())
    monkeypatch.setattr(
        editor,
        "_output",
        AsyncMock(
            side_effect=[
                editor.SUPPORTED_EDITOR + "\n",
                "ms-vscode-remote.remote-containers@" + editor.SUPPORTED_CONTAINERS,
            ]
        ),
    )
    return service, notebook, Toolchain(code, code, None, kubectl, code), grant


async def test_direct_window_uses_selected_config_and_client(editor_session, monkeypatch):
    service, notebook, tools, grant = editor_session
    monkeypatch.setenv("KUBECONFIG", "/unrelated/config")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "synthetic-personal-token")
    process = SimpleNamespace(wait=AsyncMock(return_value=0), returncode=0)
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(editor.asyncio, "create_subprocess_exec", launch)
    await editor.open_editor(service, notebook, tools)
    args, kwargs = launch.call_args
    assert args[1:3] == ("--new-window", "--folder-uri")
    assert "--user-data-dir" not in args
    assert kwargs["env"]["KUBECONFIG"] == str(grant.config_path)
    assert kwargs["env"]["PATH"].split(":")[0] == str(tools.kubectl.parent)
    assert kwargs["env"]["LAB_GRANT_CAPABILITY"] == "synthetic-cap"
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in kwargs["env"]
    service.api.session.revoke_grant.assert_not_awaited()


async def test_failed_launch_revokes_only_its_grant(editor_session, monkeypatch):
    service, notebook, tools, grant = editor_session
    process = SimpleNamespace(wait=AsyncMock(return_value=1), returncode=1)
    monkeypatch.setattr(editor.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(LabError):
        await editor.open_editor(service, notebook, tools)
    service.api.session.revoke_grant.assert_awaited_once_with(grant)


async def test_cli_target_refuses_stop_request_before_selecting_ready_pod(monkeypatch):
    discover = AsyncMock()
    monkeypatch.setattr(editor, "owned_pods", discover)
    notebook = {"metadata": {"annotations": {"kubeflow-resource-stopped": "now"}}}
    with pytest.raises(LabError, match="stopping or stopped"):
        await editor.target(SimpleNamespace(api=None), notebook)
    discover.assert_not_called()
