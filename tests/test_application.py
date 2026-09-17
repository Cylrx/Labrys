"""Cross-module contracts exercised without personal credentials or a real cluster."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lab.cli import parser, validate_arguments
from lab.editor import remote_uri
from lab.errors import LabError
from lab.inputs import Field, Input
from lab.notebooks import Notebooks
from lab.setup import data_key
from lab.tools import client_environment


@pytest.mark.parametrize("position", [0, 1, 2])
def test_common_flags_survive_subparsers(position):
    segments = [[], ["notebook"], ["list", "--namespace", "research"]]
    segments[position] += ["--cluster", "west", "--token-stdin", "--json", "-y"]
    args = parser().parse_args([value for segment in segments for value in segment])
    assert (args.cluster, args.token_stdin, args.json, args.yes) == ("west", True, True, True)


@pytest.mark.parametrize(
    "command",
    [
        "notebook status --namespace research",
        "notebook list",
        "notebook retry",
        "notebook status x --operation-id 00000000-0000-4000-8000-000000000001",
        "notebook open x --namespace research --json",
        "notebook create --name x --namespace research --timeout nan",
    ],
)
def test_incomplete_commands_fail_before_authentication(command):
    with pytest.raises(LabError):
        validate_arguments(parser().parse_args(command.split()))


@pytest.mark.asyncio
async def test_suggestion_is_not_a_buffer_default():
    control = Input(Field("image", "Image", candidates=["registry.invalid/a:v1"]), lambda: None)
    assert control.area.text == ""
    assert control.candidates.get_suggestion(None, control.area.buffer.document).text.endswith("v1")
    control.area.buffer.insert_text("custom")
    assert control.area.text == "custom"
    assert control.candidates.get_suggestion(None, control.area.buffer.document) is None


def test_client_environment_drops_ambient_auth(monkeypatch):
    for name in [
        "OP_SERVICE_ACCOUNT_TOKEN",
        "OP_SESSION_personal",
        "KUBECONFIG",
        "LAB_GRANT_CAPABILITY",
    ]:
        monkeypatch.setenv(name, "synthetic-sensitive")
    assert "synthetic-sensitive" not in json.dumps(client_environment())


def test_remote_uri_preserves_exact_target():
    from urllib.parse import unquote, urlsplit

    uri = remote_uri("unique", "research", "pod", "main", "registry.invalid/image:v1", "/data/a b")
    parsed = urlsplit(uri)
    metadata = json.loads(bytes.fromhex(parsed.netloc.split("+", 1)[1]))
    assert metadata["context"] == "unique"
    assert metadata["podname"] == "pod"
    assert unquote(parsed.path) == "/data/a b"


def test_bad_key_is_redacted():
    with pytest.raises(LabError) as error:
        data_key("private-but-not-a-key")
    assert "private-but" not in str(error.value)


@pytest.mark.asyncio
async def test_stop_and_delete_keep_object_identity():
    api = SimpleNamespace(request=AsyncMock(return_value={}))
    service = Notebooks(api, None, None, None)
    notebook = {
        "metadata": {
            "name": "sample",
            "namespace": "research",
            "uid": "uid-1",
            "resourceVersion": "revision-4",
            "annotations": {"other": "keep"},
        }
    }
    await service.change(notebook, "stop")
    patch = api.request.call_args.kwargs["body"]
    assert patch[:2] == [
        {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "revision-4"},
    ]
    assert patch[2]["value"]["other"] == "keep"
    await service.change(notebook, "delete")
    assert api.request.call_args.kwargs["body"]["preconditions"] == {"uid": "uid-1"}


@pytest.mark.asyncio
async def test_expired_authorization_does_not_start_work(monkeypatch):
    from lab import cli

    monkeypatch.setattr(cli.clock, "now", lambda: 20)
    work = AsyncMock()
    with pytest.raises(LabError, match="expired"):
        await cli.until_deadline(10, work())
    work.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_secret_source_refuses_new_reads(monkeypatch):
    from lab import secrets

    client = SimpleNamespace(secrets=SimpleNamespace(resolve_all=AsyncMock()))
    source = secrets.Secrets(client)
    source.deadline = 10
    monkeypatch.setattr(secrets.clock, "now", lambda: 20)
    with pytest.raises(LabError, match="expired"):
        await source.read("op://vault/item/notesPlain")
    client.secrets.resolve_all.assert_not_awaited()


def test_parser_never_echoes_rejected_secret_values():
    with pytest.raises(LabError) as error:
        parser().parse_args(["--token", "synthetic-secret-never-echo"])
    assert "synthetic-secret" not in str(error.value)
