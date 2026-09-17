"""Synthetic native setup guidance and stable reference identity contracts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from cryptography.fernet import Fernet

from lab import setup
from lab.errors import LabError
from lab.storage import Paths, read_bootstrap, write_bootstrap


class GuidedSecrets:
    key_ref = "op://vault-id/key-id/password"
    cluster_ref = "op://vault-id/cluster-id/notesPlain"
    root_ref = "op://vault-id/index-id/notesPlain"

    def __init__(self, key):
        self.key = key
        self.root = None
        self.cluster_text = None
        self.reads = []

    async def read(self, reference):
        self.reads.append(reference)
        if reference == self.key_ref:
            return self.key.decode()
        if reference == self.cluster_ref:
            assert self.cluster_text is not None
            return self.cluster_text
        assert reference == self.root_ref
        assert self.root is not None
        return self.root


class GuidedScreens:
    def __init__(self, secrets, events):
        self.secrets, self.events = secrets, events
        self.mode = "new"

    async def menu(self, title, description, sections, **kwargs):
        self.events.append(("menu", title, description, sections, kwargs))
        return self.mode if title == "Set up lab" else "generate"

    async def form(self, title, fields, **kwargs):
        self.events.append(("form", title, fields, kwargs))
        titles = {
            "Create the encryption key item": "My encryption key",
            "Connection index · Step 2 of 2": "My connection index",
        }
        if title == "Import kubeconfig":
            return {"path": "/synthetic/kubeconfig"}
        if title == "Local profiles directory":
            return {"profiles_dir": str(self.directory)}
        return {"title": titles[title]}

    async def choose(self, title, choices, **kwargs):
        return "direct"

    async def details(self, title, description, rows, actions, **kwargs):
        self.events.append(("details", title, description, rows, actions, kwargs))
        if title == "Save the connection index Secure Note":
            self.secrets.root = dict(rows)[""]
        if title == "Copy the cluster connection":
            self.secrets.cluster_text = dict(rows)[""]
        return actions[0][0]

    async def work(self, awaitable, title, **kwargs):
        self.events.append(("work", title))
        return await awaitable


@pytest.fixture
def guided(monkeypatch, tmp_path):
    events = []
    secrets = GuidedSecrets(Fernet.generate_key())
    paths = Paths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    screens = GuidedScreens(secrets, events)
    screens.directory = tmp_path
    monkeypatch.setattr(setup, "terminal_required", lambda: None)
    monkeypatch.setattr(setup, "clear_terminal", lambda: events.append(("clear",)))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(setup.Fernet, "generate_key", lambda: secrets.key)
    monkeypatch.setattr(
        setup,
        "select_reference",
        AsyncMock(side_effect=[secrets.key_ref, secrets.root_ref]),
    )
    return SimpleNamespace(events=events, secrets=secrets, paths=paths, screens=screens)


async def test_generate_guide_explains_purpose_before_explicit_key_reveal(guided):
    await setup.initialize(guided.secrets, guided.paths, guided.screens)
    events = guided.events
    shown = next(
        i
        for i, event in enumerate(events)
        if event[:2] == ("details", "Copy the entire encryption key")
    )
    earlier = str(events[:shown])
    assert "encrypts local history and presets" in earlier
    assert "1Password account password and Service Account Token" in earlier
    assert "Reveal the key to copy" in earlier
    assert guided.secrets.key.decode() not in earlier
    assert events[shown][3] == [("", guided.secrets.key.decode())]
    assert "reads the Password field back" in events[shown][2]
    assert events[shown + 1] == ("clear",)
    assert sum(event[0] == "clear" for event in events) == 2
    assert guided.secrets.reads.count(guided.secrets.key_ref) == 3
    assert read_bootstrap(guided.paths)["index_ref"] == guided.secrets.root_ref


async def test_titles_are_editable_guidance_and_never_become_identity(guided):
    await setup.initialize(guided.secrets, guided.paths, guided.screens)
    title_inputs = [
        event[2][0].value
        for event in guided.events
        if event[0] == "form" and event[2][0].name == "title"
    ]
    assert title_inputs == [
        "lab · Local data encryption key",
        "lab · Connection index",
    ]
    guides = [dict(event[3]) for event in guided.events if event[0] == "details"]
    titled = [guide for guide in guides if "Title" in guide]
    assert [guide["Title"] for guide in titled] == [
        "My encryption key",
        "My connection index",
    ]
    assert titled[0]["Category"] == "Password"
    assert titled[0]["Field"] == "password (Password)"
    assert "local history and presets" in titled[0]["Notes"]
    assert guided.secrets.cluster_text is None
    root = yaml.safe_load(guided.secrets.root)
    assert root["data_key_ref"] == guided.secrets.key_ref
    assert root["clusters"] == []
    assert "My encryption key" not in guided.secrets.root


async def test_mismatched_generated_key_stops_before_bootstrap(guided, monkeypatch):
    other_key = setup.base64.urlsafe_b64encode(b"a" * 32)
    assert other_key != guided.secrets.key
    monkeypatch.setattr(setup.Fernet, "generate_key", lambda: other_key)
    with pytest.raises(LabError, match="does not match the generated key"):
        await setup.initialize(guided.secrets, guided.paths, guided.screens)
    assert not guided.paths.bootstrap_path.exists()
    assert ("clear",) in guided.events


async def test_recovery_explains_key_and_does_not_offer_generation(guided, monkeypatch):
    await setup.initialize(guided.secrets, guided.paths, guided.screens)
    guided.events.clear()
    write_bootstrap(guided.paths, "op://vault-id/old-index/notesPlain", guided.screens.directory)
    guided.screens.mode = "existing"
    monkeypatch.setattr(setup, "select_reference", AsyncMock(return_value=guided.secrets.root_ref))
    await setup.initialize(guided.secrets, guided.paths, guided.screens)
    menu = guided.events[0]
    assert [value for value, _ in menu[3][0][1]] == ["existing"]
    assert "Keep the same key to recover" in menu[2]
    assert all(
        event[:2] != ("details", "Copy the entire encryption key") for event in guided.events
    )
    assert read_bootstrap(guided.paths)["index_ref"] == guided.secrets.root_ref


async def test_cancelled_key_reveal_clears_terminal_without_committing(guided, monkeypatch):
    original = guided.screens.details

    async def details(title, *args, **kwargs):
        if title == "Copy the entire encryption key":
            raise LabError("Cancelled.", 130)
        return await original(title, *args, **kwargs)

    monkeypatch.setattr(guided.screens, "details", details)
    monkeypatch.setattr(
        guided.screens,
        "menu",
        AsyncMock(side_effect=["new", "generate", LabError("Cancelled.", 130)]),
    )
    with pytest.raises(LabError) as cancelled:
        await setup.initialize(guided.secrets, guided.paths, guided.screens)
    assert cancelled.value.code == 130
    assert guided.events[-1] == ("clear",)
    assert not guided.paths.bootstrap_path.exists()
    assert guided.secrets.reads == []


async def test_new_index_checks_entire_document(guided, monkeypatch):
    original = guided.screens.details

    async def details(title, *args, **kwargs):
        result = await original(title, *args, **kwargs)
        if title == "Save the connection index Secure Note":
            root = yaml.safe_load(guided.secrets.root)
            root["session"]["max_age_seconds"] = 60
            guided.secrets.root = yaml.safe_dump(root)
        return result

    monkeypatch.setattr(guided.screens, "details", details)
    with pytest.raises(LabError, match="complete index"):
        await setup.initialize(guided.secrets, guided.paths, guided.screens)
    assert not guided.paths.bootstrap_path.exists()


async def test_invalid_profiles_directory_preserves_bootstrap(guided, monkeypatch):
    await setup.initialize(guided.secrets, guided.paths, guided.screens)
    previous = guided.paths.bootstrap_path.read_bytes()
    guided.screens.mode = "existing"
    guided.screens.directory /= "missing"
    monkeypatch.setattr(setup, "select_reference", AsyncMock(return_value=guided.secrets.root_ref))
    with pytest.raises(LabError, match="existing readable profiles directory"):
        await setup.initialize(guided.secrets, guided.paths, guided.screens)
    assert guided.paths.bootstrap_path.read_bytes() == previous
