"""Native setup navigation and cancellation use synthetic secrets only."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lab import connections, setup
from lab.errors import LabError
from lab.storage import Paths


async def run_work(awaitable, title, **kwargs):
    return await awaitable


async def test_item_picker_uses_stable_ids_with_arbitrary_titles():
    secrets = SimpleNamespace(
        vaults=AsyncMock(return_value=[SimpleNamespace(id="vault-id", title="Any vault")]),
        items=AsyncMock(return_value=[SimpleNamespace(id="item-id", title="Renamed [key]")]),
    )
    screens = SimpleNamespace(
        menu=AsyncMock(return_value="select"),
        choose=AsyncMock(side_effect=["vault-id", "item-id"]),
        work=run_work,
    )
    assert await setup.select_reference(secrets, "Encryption key", "password", screens) == (
        "op://vault-id/item-id/password"
    )
    assert screens.choose.call_args_list[-1].args[1] == [("item-id", "Renamed [key]")]
    secrets.items.assert_awaited_once_with("vault-id")


async def test_reference_paste_validates_complete_path_without_reading_secrets():
    secrets = SimpleNamespace(read=AsyncMock())
    screens = SimpleNamespace(
        menu=AsyncMock(return_value="paste"),
        form=AsyncMock(
            side_effect=[
                {"reference": "op://incomplete"},
                {"reference": "op://vault/item/password"},
            ]
        ),
        details=AsyncMock(return_value="retry"),
    )
    assert await setup.select_reference(secrets, "Data key", "password", screens) == (
        "op://vault/item/password"
    )
    screens.details.assert_awaited_once()
    secrets.read.assert_not_awaited()


async def test_escape_from_items_returns_to_vaults():
    secrets = SimpleNamespace(
        vaults=AsyncMock(return_value=[SimpleNamespace(id="vault", title="Vault")]),
        items=AsyncMock(return_value=[SimpleNamespace(id="item", title="Item")]),
    )
    screens = SimpleNamespace(
        menu=AsyncMock(return_value="select"),
        work=run_work,
        choose=AsyncMock(side_effect=["vault", LabError("Cancelled.", 130), "vault", "item"]),
    )
    assert await setup.select_reference(secrets, "Data key", "password", screens) == (
        "op://vault/item/password"
    )
    assert secrets.items.await_count == 2
    assert screens.menu.await_count == 1


@pytest.mark.parametrize("state", ["history.enc", "presets.enc", "history.enc.bak"])
async def test_ciphertext_alone_hides_fresh_setup(tmp_path, monkeypatch, state):
    paths = Paths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    target = paths.data if state.startswith("presets") else paths.state
    target.mkdir(mode=0o700)
    (target / state).write_bytes(b"preserved synthetic ciphertext")
    screens = SimpleNamespace(menu=AsyncMock(side_effect=LabError("Cancelled.", 130)))
    monkeypatch.setattr(setup, "terminal_required", lambda: None)
    with pytest.raises(LabError) as cancelled:
        await setup.initialize(SimpleNamespace(), paths, screens)
    assert cancelled.value.code == 130
    sections = screens.menu.call_args.args[2]
    assert [value for value, _ in sections[0][1]] == ["existing"]
    assert (target / state).read_bytes() == b"preserved synthetic ciphertext"
    assert not paths.bootstrap_path.exists()


async def test_import_assembles_exact_connection_without_reading_nested_file_references(tmp_path):
    import yaml
    from fixtures import cluster_data

    kubeconfig = cluster_data()["kubernetes"]["kubeconfig"]
    path = tmp_path / "kubeconfig"
    path.write_text(kubeconfig)
    screens = SimpleNamespace(
        form=AsyncMock(side_effect=[{"target": "test-vm"}]),
        choose=AsyncMock(return_value="ssh"),
    )
    text = await connections.connection_note(screens, connections.read_kubeconfig(str(path)))
    value = yaml.safe_load(text)
    assert set(value) == {"kubernetes", "transport"}
    assert value["kubernetes"] == {"context": "fictional", "kubeconfig": kubeconfig}
    assert value["transport"] == {"mode": "ssh", "ssh_target": "test-vm"}
    assert path.read_text() == kubeconfig
    assert "kubeconfig: |" in text
    assert "trust:" not in text


@pytest.mark.parametrize("kind", ["directory", "oversized", "invalid_utf8"])
def test_import_rejects_unreadable_or_unbounded_source(tmp_path, kind):
    path = tmp_path / "kubeconfig"
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"x" * (1024 * 1024 + 1) if kind == "oversized" else b"\xff")
    with pytest.raises(LabError, match="readable UTF-8"):
        connections.read_kubeconfig(str(path))
