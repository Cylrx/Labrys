"""Profile discovery exposes unavailable YAML while excluding existing registrations."""

from unittest.mock import AsyncMock

import pytest
import yaml
from fixtures import profile_data

from lab import policy, registration
from lab.errors import LabError


def write_profile(directory, name):
    path = directory / name
    path.write_text(yaml.safe_dump(profile_data()))
    return path


def test_catalog_groups_available_first_and_never_hides_bad_yaml(tmp_path):
    for name in ("z-ready.yaml", "a-ready.yaml", "registered.yaml", "template.yaml", "wrong.yml"):
        write_profile(tmp_path, name)
    (tmp_path / "broken.yaml").write_text("not: [valid yaml")
    (tmp_path / "odd.yaml").mkdir()
    (tmp_path / "notes.txt").write_text("not a profile")
    entries = policy.unregistered_profiles(tmp_path, {"registered"})
    assert [(entry.path.name, entry.status) for entry in entries] == [
        ("a-ready.yaml", "Available"),
        ("z-ready.yaml", "Available"),
        ("broken.yaml", "Unavailable"),
        ("odd.yaml", "Unavailable"),
        ("wrong.yml", "Unavailable"),
    ]
    assert all(entry.error for entry in entries[2:])


def test_unreadable_profile_has_a_reason_without_disappearing(tmp_path, monkeypatch):
    write_profile(tmp_path, "private.yaml")
    monkeypatch.setattr(policy.os, "open", lambda *args: (_ for _ in ()).throw(PermissionError()))
    entries = policy.unregistered_profiles(tmp_path, set())
    assert entries[0].status == "Unavailable"
    assert "Permission denied" in entries[0].error


def test_directory_read_failure_is_not_an_empty_catalog(tmp_path):
    with pytest.raises(LabError, match="Cannot read the profiles directory"):
        policy.unregistered_profiles(tmp_path / "missing", set())


async def test_picker_refresh_rechecks_index_and_repaired_files(tmp_path):
    write_profile(tmp_path, "registered.yaml")
    broken = tmp_path / "repair.yaml"
    broken.write_text("invalid YAML")
    root = {
        "schema_version": 1,
        "data_key_ref": "op://vault/key/password",
        "clusters": [{"id": "registered", "config_ref": "op://vault/old/notesPlain"}],
    }
    from types import SimpleNamespace

    secrets = SimpleNamespace(read=AsyncMock(side_effect=lambda _: yaml.safe_dump(root)))
    calls = []

    async def work(awaitable, title):
        return await awaitable

    async def table(title, description, choices, actions, **kwargs):
        calls.append(choices)
        if len(calls) == 1:
            assert choices == [("profile:repair.yaml", "repair.yaml", "Unavailable")]
            return "profile:repair.yaml"
        assert choices == [("profile:repair.yaml", "repair", "Available")]
        return "profile:repair.yaml"

    async def details(title, description, rows, actions):
        assert title == "Profile unavailable"
        assert dict(rows)["Reason"]
        write_profile(tmp_path, "repair.yaml")
        return "back"

    screens = SimpleNamespace(work=work, table=table, details=details)
    selected = await registration.select_profile(
        secrets, "op://vault/index/notesPlain", tmp_path, screens
    )
    assert selected == "repair"
    assert secrets.read.await_count == 2


async def test_refresh_can_remove_a_newly_registered_profile_and_back_cancels(tmp_path):
    from types import SimpleNamespace

    write_profile(tmp_path, "refresh.yaml")
    root = {"schema_version": 1, "data_key_ref": "op://vault/key/password", "clusters": []}
    secrets = SimpleNamespace(read=AsyncMock(side_effect=lambda _: yaml.safe_dump(root)))
    pages = []

    async def work(awaitable, title):
        return await awaitable

    async def table(title, description, choices, actions, **kwargs):
        pages.append(choices)
        if len(pages) == 1:
            assert choices[0][0] == "profile:refresh.yaml"
            root["clusters"] = [{"id": "refresh", "config_ref": "op://vault/new/notesPlain"}]
            return "refresh"
        assert choices == []
        return "back"

    screens = SimpleNamespace(work=work, table=table)
    assert (
        await registration.select_profile(secrets, "op://vault/index/notesPlain", tmp_path, screens)
        is None
    )
    assert len(pages) == 2
