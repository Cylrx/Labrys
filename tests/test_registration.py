"""Registration ordering, identity, concurrency and read-only verification contracts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from lab import registration
from lab.errors import LabError

ID = "research-example"
OTHER = "research-other"
ROOT = "op://vault/index/notesPlain"
CONNECTION = "op://vault/connection/notesPlain"


@pytest.fixture
def flow(monkeypatch, tmp_path):
    root = {
        "schema_version": 1,
        "data_key_ref": "op://vault/key/password",
        "session": {"max_age_seconds": 900},
        "clusters": [],
    }
    state = SimpleNamespace(root=yaml.safe_dump(root), note="", shown=[], probes=[])
    kube = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "cluster", "cluster": {"server": "https://example.invalid"}}],
        "users": [{"name": "user", "user": {"token": "fictional-token"}}],
        "contexts": [{"name": "context", "context": {"cluster": "cluster", "user": "user"}}],
    }
    note = yaml.safe_dump(
        {
            "kubernetes": {"context": "context", "kubeconfig": yaml.safe_dump(kube)},
            "transport": {"mode": "direct"},
        }
    )

    async def read(reference):
        return state.root if reference == ROOT else state.note

    async def details(title, description, rows, actions, **kwargs):
        state.shown.append(title)
        if title == "Copy the cluster connection":
            state.note = kwargs["copy_text"]
        if title == "Update the connection index":
            state.root = kwargs["copy_text"]
        return actions[0][0]

    async def work(awaitable, title):
        return await awaitable

    screens = SimpleNamespace(
        form=AsyncMock(return_value={"title": "Cluster"}),
        details=details,
        work=work,
    )
    secrets = SimpleNamespace(read=AsyncMock(side_effect=read))
    session = AsyncMock()
    session.__aenter__.return_value = session
    factory = Mock(return_value=session)
    api = SimpleNamespace(request=AsyncMock(return_value={"gitVersion": "fictional"}))
    monkeypatch.setattr(registration, "terminal_required", lambda: None)
    monkeypatch.setattr(registration, "load_profile", Mock())
    monkeypatch.setattr(registration, "connection_source", AsyncMock(return_value=(note, None)))
    monkeypatch.setattr(registration, "select_reference", AsyncMock(return_value=CONNECTION))
    monkeypatch.setattr(registration, "clear_terminal", lambda: None)
    monkeypatch.setattr(registration, "Session", factory)
    monkeypatch.setattr(registration, "Kubernetes", Mock(return_value=api))
    monkeypatch.setenv("TERM", "xterm-256color")
    return SimpleNamespace(
        state=state,
        screens=screens,
        secrets=secrets,
        session=session,
        factory=factory,
        api=api,
        bootstrap={"index_ref": ROOT, "profiles_dir": str(tmp_path)},
    )


async def run(flow):
    await registration.add_cluster(
        flow.secrets, flow.bootstrap, ID, flow.screens, authorized_at=123.0
    )


async def test_verifies_read_only_and_appends_latest_index(flow):

    async def probe(*args):
        latest = yaml.safe_load(flow.state.root)
        latest["data_key_ref"] = "op://vault/replaced-key/password"
        latest["session"]["max_age_seconds"] = 600
        latest["clusters"].append({"id": OTHER, "config_ref": "op://vault/other/notesPlain"})
        flow.state.root = yaml.safe_dump(latest)
        return {"gitVersion": "fictional"}

    flow.api.request.side_effect = probe
    await run(flow)
    root = yaml.safe_load(flow.state.root)
    assert root["data_key_ref"] == "op://vault/replaced-key/password"
    assert root["session"]["max_age_seconds"] == 600
    assert [entry["id"] for entry in root["clusters"]] == [OTHER, ID]
    flow.api.request.assert_awaited_once_with("GET", "/version")
    flow.session.__aexit__.assert_awaited_once()
    assert flow.factory.call_args.kwargs["authorized_at"] == 123.0


async def test_invalid_profile_precedes_connection_import(flow, monkeypatch):
    monkeypatch.setattr(
        registration, "load_profile", Mock(side_effect=LabError("Missing profile", 4))
    )
    with pytest.raises(LabError, match="Missing profile"):
        await run(flow)
    registration.connection_source.assert_not_awaited()
    flow.secrets.read.assert_not_awaited()


async def test_concurrent_duplicate_stops_before_index_display(flow, capsys):
    async def probe(*args):
        root = yaml.safe_load(flow.state.root)
        entry = {"id": OTHER, "config_ref": "op://vault/other/notesPlain"}
        entry["id"] = ID
        root["clusters"] = [entry]
        flow.state.root = yaml.safe_dump(root)

    flow.api.request.side_effect = probe
    with pytest.raises(LabError, match="already"):
        await run(flow)
    assert "Update the connection index" not in flow.state.shown
    assert "may already exist" in capsys.readouterr().err


async def test_failed_probe_closes_session_and_does_not_offer_index(flow, capsys):
    flow.api.request.side_effect = LabError("Access denied", 6)
    with pytest.raises(LabError, match="Access denied"):
        await run(flow)
    flow.session.__aexit__.assert_awaited_once()
    assert "Update the connection index" not in flow.state.shown
    assert "unbound" in capsys.readouterr().err


async def test_exact_note_readback_required_before_probe(flow):
    original = flow.screens.details

    async def changed(title, *args, **kwargs):
        result = await original(title, *args, **kwargs)
        if title == "Copy the cluster connection":
            flow.state.note = flow.state.note.replace("fictional-token", "another-token")
        return result

    flow.screens.details = changed
    with pytest.raises(LabError, match="exactly match"):
        await run(flow)
    flow.factory.assert_not_called()


async def test_exact_full_index_readback_required(flow):
    original = flow.screens.details

    async def changed(title, *args, **kwargs):
        result = await original(title, *args, **kwargs)
        if title == "Index verification incomplete":
            return "cancel"
        if title == "Update the connection index":
            root = yaml.safe_load(flow.state.root)
            root["session"]["max_age_seconds"] += 1
            flow.state.root = yaml.safe_dump(root)
        return result

    flow.screens.details = changed
    with pytest.raises(LabError, match="not verified") as error:
        await run(flow)
    assert error.value.code == 130
    assert "Cluster registered" not in flow.state.shown


async def test_cancel_after_connection_reveal_warns_without_binding(flow, capsys):
    original = flow.screens.details

    async def cancel(title, *args, **kwargs):
        result = await original(title, *args, **kwargs)
        if title == "Copy the cluster connection":
            raise LabError("Cancelled", 130)
        return result

    flow.screens.details = cancel
    with pytest.raises(LabError) as error:
        await run(flow)
    assert error.value.code == 130
    assert flow.state.note
    assert yaml.safe_load(flow.state.root)["clusters"] == []
    assert "unbound" in capsys.readouterr().err
    flow.factory.assert_not_called()


async def test_readback_compares_all_values_without_requiring_yaml_formatting(flow):
    original = flow.screens.details

    async def reformat(title, *args, **kwargs):
        result = await original(title, *args, **kwargs)
        if title == "Copy the cluster connection":
            flow.state.note = "# Saved connection\n" + flow.state.note + "\n"
        if title == "Update the connection index":
            flow.state.root = yaml.safe_dump(yaml.safe_load(flow.state.root), sort_keys=True)
        return result

    flow.screens.details = reformat
    await run(flow)
    assert "Cluster registered" in flow.state.shown


def register_entries(flow):
    root = yaml.safe_load(flow.state.root)
    root["clusters"] = [
        {"id": ID, "config_ref": CONNECTION},
        {"id": OTHER, "config_ref": "op://vault/other/notesPlain"},
    ]
    flow.state.root = yaml.safe_dump(root)
    flow.state.note = "existing-note-contents"
    flow.screens.confirm = AsyncMock(return_value=True)
    flow.screens.choose = AsyncMock(return_value=ID)
    return root


async def test_remove_only_changes_the_selected_index_entry(flow):
    from pathlib import Path

    before = register_entries(flow)
    path = Path(flow.bootstrap["profiles_dir"]) / f"{ID}.yaml"
    path.write_text("profile-must-remain")
    await registration.remove_cluster(flow.secrets, flow.bootstrap, screens=flow.screens)
    after = yaml.safe_load(flow.state.root)
    assert after == {**before, "clusters": before["clusters"][1:]}
    assert path.read_text() == "profile-must-remain"
    assert flow.state.note == "existing-note-contents"
    assert {call.args[0] for call in flow.secrets.read.await_args_list} == {ROOT}
    flow.factory.assert_not_called()
    assert "Cluster registration removed" in flow.state.shown


async def test_remove_cancellation_keeps_index_and_never_shows_copy_page(flow):
    register_entries(flow)
    before = flow.state.root
    flow.screens.confirm.return_value = False
    await registration.remove_cluster(flow.secrets, flow.bootstrap, ID, flow.screens)
    assert flow.state.root == before
    assert "Update the connection index" not in flow.state.shown
    assert "Cluster registration removed" not in flow.state.shown
    flow.factory.assert_not_called()


@pytest.mark.parametrize("change", ["replace", "remove"])
async def test_remove_rechecks_the_selected_registration_after_confirmation(flow, change):
    register_entries(flow)

    async def confirm(*args):
        root = yaml.safe_load(flow.state.root)
        if change == "replace":
            root["clusters"][0]["config_ref"] = "op://vault/replacement/notesPlain"
        else:
            root["clusters"].pop(0)
        flow.state.root = yaml.safe_dump(root)
        return True

    flow.screens.confirm.side_effect = confirm
    with pytest.raises(LabError, match="registration changed"):
        await registration.remove_cluster(flow.secrets, flow.bootstrap, ID, flow.screens)
    assert "Update the connection index" not in flow.state.shown


async def test_remove_preserves_unrelated_latest_index_changes(flow):
    register_entries(flow)

    async def confirm(*args):
        root = yaml.safe_load(flow.state.root)
        root["clusters"].append({"id": "added", "config_ref": "op://vault/added/notesPlain"})
        root["session"]["max_age_seconds"] = 600
        root["data_key_ref"] = "op://vault/current-key/password"
        flow.state.root = yaml.safe_dump(root)
        return True

    flow.screens.confirm.side_effect = confirm
    await registration.remove_cluster(flow.secrets, flow.bootstrap, ID, flow.screens)
    root = yaml.safe_load(flow.state.root)
    assert [entry["id"] for entry in root["clusters"]] == [OTHER, "added"]
    assert root["session"]["max_age_seconds"] == 600
    assert root["data_key_ref"] == "op://vault/current-key/password"


async def test_remove_requires_verified_manual_save(flow, capsys):
    register_entries(flow)

    async def details(title, description, rows, actions, **kwargs):
        flow.state.shown.append(title)
        if title == "Index verification incomplete":
            return "cancel"
        return "saved"

    flow.screens.details = details
    with pytest.raises(LabError, match="not verified") as error:
        await registration.remove_cluster(flow.secrets, flow.bootstrap, ID, flow.screens)
    assert error.value.code == 130
    assert "Cluster registration removed" not in flow.state.shown
    assert "Removal was not verified" in capsys.readouterr().err


async def test_remove_cancelled_after_manual_save_does_not_claim_unchanged_index(flow, capsys):
    register_entries(flow)
    original = flow.screens.details

    async def details(title, *args, **kwargs):
        result = await original(title, *args, **kwargs)
        if title == "Update the connection index":
            raise LabError("Cancelled.", 130)
        return result

    flow.screens.details = details
    with pytest.raises(LabError) as error:
        await registration.remove_cluster(flow.secrets, flow.bootstrap, ID, flow.screens)
    assert error.value.code == 130
    assert "Cluster registration removed" not in flow.state.shown
    assert "Check the index in 1Password" in capsys.readouterr().err


async def test_remove_empty_index_and_unknown_name_never_start_mutation(flow):
    await registration.remove_cluster(flow.secrets, flow.bootstrap, screens=flow.screens)
    assert flow.state.shown == ["No registered clusters"]
    with pytest.raises(LabError, match="not in the selected root index"):
        await registration.remove_cluster(flow.secrets, flow.bootstrap, "unknown", flow.screens)
    assert "Update the connection index" not in flow.state.shown
    flow.factory.assert_not_called()


async def test_add_without_name_uses_profile_picker_then_revalidates(flow, monkeypatch):
    picker = AsyncMock(return_value=ID)
    monkeypatch.setattr(registration, "select_profile", picker)
    await registration.add_cluster(flow.secrets, flow.bootstrap, screens=flow.screens)
    picker.assert_awaited_once()
    registration.load_profile.assert_called_once()
    assert yaml.safe_load(flow.state.root)["clusters"] == [{"id": ID, "config_ref": CONNECTION}]


async def test_back_from_profile_picker_does_not_import_or_change_index(flow, monkeypatch):
    monkeypatch.setattr(registration, "select_profile", AsyncMock(return_value=None))
    original = flow.state.root
    await registration.add_cluster(flow.secrets, flow.bootstrap, screens=flow.screens)
    registration.connection_source.assert_not_awaited()
    flow.factory.assert_not_called()
    assert flow.state.root == original


async def test_removing_last_registration_leaves_a_valid_empty_index(flow):
    root = register_entries(flow)
    root["clusters"] = root["clusters"][:1]
    flow.state.root = yaml.safe_dump(root)
    await registration.remove_cluster(flow.secrets, flow.bootstrap, ID, flow.screens)
    assert registration.parse_index(flow.state.root).clusters == ()


async def test_reusing_existing_note_skips_creation_and_credential_reveal(flow, monkeypatch):
    text, _ = registration.connection_source.return_value
    flow.state.note = text
    registration.connection_source.return_value = (text, CONNECTION)
    monkeypatch.setattr(
        registration, "save_connection", AsyncMock(side_effect=AssertionError("duplicate note"))
    )
    await run(flow)
    assert "Save the cluster connection" not in flow.state.shown
    assert "Copy the cluster connection" not in flow.state.shown
    assert "Cluster registered" in flow.state.shown
    flow.screens.form.assert_not_awaited()
    root = yaml.safe_load(flow.state.root)
    assert root["clusters"] == [{"id": ID, "config_ref": CONNECTION}]
    flow.api.request.assert_awaited_once_with("GET", "/version")


async def test_existing_note_change_after_confirmation_stops_before_connection(flow):
    text, _ = registration.connection_source.return_value
    registration.connection_source.return_value = (text, CONNECTION)
    flow.state.note = text.replace("fictional-token", "changed-token")
    with pytest.raises(LabError, match="exactly match"):
        await run(flow)
    flow.factory.assert_not_called()
    assert "Update the connection index" not in flow.state.shown
