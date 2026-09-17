"""All connection sources share inert kubeconfig validation and route selection."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from fixtures import cluster_data

from lab import connections
from lab.config import MAX_DOCUMENT_BYTES, parse_cluster_note
from lab.errors import LabError


async def work(awaitable, title):
    return await awaitable


@pytest.mark.parametrize("source", ["paste", "file"])
async def test_file_and_paste_build_the_same_connection(tmp_path, source):
    raw = cluster_data()["kubernetes"]["kubeconfig"]
    path = tmp_path / "kubeconfig"
    path.write_text(raw)
    screens = SimpleNamespace(
        menu=AsyncMock(return_value=source),
        form=AsyncMock(return_value={"path": str(path)}),
        paste=AsyncMock(return_value=raw),
        choose=AsyncMock(return_value="direct"),
    )
    secrets = SimpleNamespace(read=AsyncMock())
    text, reference = await connections.connection_source(secrets, screens)
    note = parse_cluster_note(text)
    assert reference is None
    assert note.kubernetes.kubeconfig.get_secret_value() == raw
    assert note.transport.mode == "direct"
    secrets.read.assert_not_awaited()
    assert path.read_text() == raw
    if source == "paste":
        screens.form.assert_not_awaited()
        assert screens.paste.call_args.kwargs["max_bytes"] == MAX_DOCUMENT_BYTES
    else:
        screens.paste.assert_not_awaited()


async def test_existing_note_is_read_and_validated_without_import_or_reveal(monkeypatch):
    note = yaml.safe_dump(cluster_data())
    reference = "op://vault/existing/notesPlain"
    secrets = SimpleNamespace(read=AsyncMock(return_value=note))
    screens = SimpleNamespace(menu=AsyncMock(return_value="existing"), work=work)
    monkeypatch.setattr(connections, "select_reference", AsyncMock(return_value=reference))
    monkeypatch.setattr(
        connections, "read_kubeconfig", Mock(side_effect=AssertionError("file read"))
    )
    assert await connections.connection_source(secrets, screens) == (note, reference)
    connections.read_kubeconfig.assert_not_called()
    secrets.read.assert_awaited_once_with(reference)


async def test_wrong_existing_item_is_rejected_without_echoing_contents(monkeypatch):
    secrets = SimpleNamespace(read=AsyncMock(return_value="unexpected: PRIVATE_SECRET"))
    screens = SimpleNamespace(menu=AsyncMock(return_value="existing"), work=work)
    monkeypatch.setattr(
        connections, "select_reference", AsyncMock(return_value="op://v/i/notesPlain")
    )
    with pytest.raises(LabError) as error:
        await connections.connection_source(secrets, screens)
    assert "PRIVATE_SECRET" not in str(error.value)


async def test_pasted_kubeconfig_selects_context_and_ssh_route():
    data = cluster_data()
    kube = yaml.safe_load(data["kubernetes"]["kubeconfig"])
    kube["contexts"].append({"name": "second", "context": dict(kube["contexts"][0]["context"])})
    raw = yaml.safe_dump(kube)
    screens = SimpleNamespace(
        menu=AsyncMock(return_value="paste"),
        paste=AsyncMock(return_value=raw),
        choose=AsyncMock(side_effect=["second", "ssh"]),
        form=AsyncMock(return_value={"target": "existing-vm"}),
    )
    text, reference = await connections.connection_source(SimpleNamespace(), screens)
    note = parse_cluster_note(text)
    assert note.kubernetes.context == "second"
    assert note.transport.ssh_target == "existing-vm"
    assert reference is None


async def test_paste_cancellation_does_not_read_files_or_choose_route(monkeypatch):
    screens = SimpleNamespace(
        menu=AsyncMock(return_value="paste"),
        paste=AsyncMock(side_effect=LabError("Cancelled.", 130)),
        choose=AsyncMock(),
    )
    monkeypatch.setattr(connections, "read_kubeconfig", Mock())
    with pytest.raises(LabError) as error:
        await connections.connection_source(SimpleNamespace(), screens)
    assert error.value.code == 130
    screens.choose.assert_not_awaited()
    connections.read_kubeconfig.assert_not_called()
