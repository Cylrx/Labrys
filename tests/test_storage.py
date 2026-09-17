"""Persistence integrity and concurrent update checks."""

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet

from lab.errors import LabError
from lab.storage import EncryptedStore, Paths, read_bootstrap, write_bootstrap


@pytest.fixture
def store(tmp_path):
    return EncryptedStore(tmp_path / "state" / "history.enc", Fernet.generate_key(), "history")


def _append(data, value):
    data["seen_configurations"].append(
        {
            "cluster_id": "cluster",
            "namespace": "research",
            "fingerprint": f"{value:064x}",
            "first_presented_at": "2026-09-14T00:00:00Z",
        }
    )


def test_empty_first_use_and_private_encrypted_files(store):
    assert store.read() == {"field_history": [], "seen_configurations": [], "operations": {}}
    assert not store.path.exists()
    store.update(lambda data: _append(data, 1))
    assert b"research" not in store.path.read_bytes()
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(store.cipher.decrypt(store.path.read_bytes()))["revision"] == 1


def test_atomic_updates_keep_previous_ciphertext(store):
    store.update(lambda data: _append(data, 1))
    previous = store.path.read_bytes()
    store.update(lambda data: _append(data, 2))
    assert store.path.with_suffix(".enc.bak").read_bytes() == previous
    assert len(store.read()["seen_configurations"]) == 2
    assert not list(store.path.parent.glob("*.tmp"))


def test_concurrent_updates_merge_under_lock(store):
    with ThreadPoolExecutor(max_workers=8) as workers:
        futures = [
            workers.submit(store.update, lambda data, i=i: _append(data, i)) for i in range(24)
        ]
        for future in futures:
            future.result()
    assert len(store.read()["seen_configurations"]) == 24
    assert json.loads(store.cipher.decrypt(store.path.read_bytes()))["revision"] == 24


def test_wrong_key_and_corruption_never_overwrite(store):
    store.update(lambda data: _append(data, 1))
    original = store.path.read_bytes()
    wrong = EncryptedStore(store.path, Fernet.generate_key(), "history")
    with pytest.raises(LabError, match="wrong key or damaged"):
        wrong.update(lambda data: _append(data, 2))
    assert store.path.read_bytes() == original
    store.path.write_bytes(b"damaged")
    with pytest.raises(LabError, match="wrong key or damaged"):
        store.update(lambda data: _append(data, 2))
    assert store.path.read_bytes() == b"damaged"


@pytest.mark.parametrize(
    "plaintext",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":true,"kind":"history","revision":0,"data":{}}',
        b'{"schema_version":1,"kind":"presets","revision":0,"data":{"presets":[]}}',
        b'{"schema_version":1,"kind":"history","revision":0,"data":{"operations":{},"field_history":[],"seen_configurations":[],"token":"secret"}}',
    ],
)
def test_authenticated_malformed_state_is_rejected(store, plaintext):
    store.read()
    original = store.cipher.encrypt(plaintext)
    store.path.write_bytes(original)
    store.path.chmod(0o600)
    with pytest.raises(LabError):
        store.update(lambda data: None)
    assert store.path.read_bytes() == original


def test_missing_primary_with_backup_requires_recovery(store):
    store.update(lambda data: _append(data, 1))
    store.update(lambda data: _append(data, 2))
    store.path.unlink()
    with pytest.raises(LabError, match="restore its encrypted backup"):
        store.read()


def test_symlink_file_and_directory_rejected(store, tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    store.read()
    store.path.symlink_to(target)
    with pytest.raises(LabError):
        store.read()
    assert target.read_bytes() == b"untouched"
    linked = tmp_path / "linked"
    linked.symlink_to(store.path.parent, target_is_directory=True)
    with pytest.raises(LabError):
        EncryptedStore(linked / "other.enc", Fernet.generate_key(), "history").read()


def test_insecure_file_permissions_rejected(store):
    store.update(lambda data: None)
    store.path.chmod(0o644)
    with pytest.raises(LabError, match="0600"):
        store.read()


def test_unexpected_ownership_is_rejected(store, monkeypatch):
    store.update(lambda data: None)
    current_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: current_uid + 1)
    with pytest.raises(LabError, match="user-owned"):
        store.read()


def test_hardlinked_ciphertext_is_rejected(store):
    store.update(lambda data: None)
    original = store.path.read_bytes()
    os.link(store.path, store.path.with_suffix(".linked"))
    with pytest.raises(LabError, match="single filesystem link"):
        store.update(lambda data: _append(data, 1))
    assert store.path.read_bytes() == original


def test_oversized_ciphertext_is_rejected_before_decryption(store, monkeypatch):
    import lab.storage as storage

    store.update(lambda data: None)
    original = store.path.read_bytes()
    monkeypatch.setattr(storage, "MAX_CIPHERTEXT_BYTES", 1)
    with pytest.raises(LabError, match="size limit"):
        store.read()
    assert store.path.read_bytes() == original


def test_corrupt_backup_is_preserved(store):
    store.update(lambda data: None)
    store.update(lambda data: _append(data, 1))
    original = store.path.read_bytes()
    backup = store.path.with_suffix(".enc.bak")
    backup.write_bytes(b"damaged backup")
    with pytest.raises(LabError, match="wrong key or damaged"):
        store.update(lambda data: _append(data, 2))
    assert store.path.read_bytes() == original
    assert backup.read_bytes() == b"damaged backup"


def test_fifo_rejected_without_blocking(store):
    store.read()
    os.mkfifo(store.path, 0o600)
    with pytest.raises(LabError, match="regular"):
        store.read()


def test_lock_timeout_is_bounded(store, monkeypatch):
    import fcntl

    import lab.storage as storage

    store.read()
    monkeypatch.setattr(storage, "LOCK_TIMEOUT", 0.04)
    with open(str(store.path) + ".lock", "r+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LabError, match="busy"):
            store.update(lambda data: None)


def test_oversized_update_preserves_original(store, monkeypatch):
    import lab.storage as storage

    store.update(lambda data: None)
    original = store.path.read_bytes()
    monkeypatch.setattr(storage, "MAX_DOCUMENT_BYTES", 250)
    with pytest.raises(LabError, match="16 MiB"):
        store.update(lambda data: _append(data, 1))
    assert store.path.read_bytes() == original


def test_failed_atomic_replace_preserves_primary(store, monkeypatch):
    store.update(lambda data: _append(data, 1))
    original = store.path.read_bytes()
    replace = os.replace

    def fail_primary(source, destination, **kwargs):
        if destination == store.path.name:
            raise OSError("simulated rename failure")
        replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail_primary)
    with pytest.raises(LabError):
        store.update(lambda data: _append(data, 2))
    assert store.path.read_bytes() == original
    assert not list(store.path.parent.glob("*.tmp"))


def test_bootstrap_stores_index_and_absolute_profiles_directory(tmp_path):
    paths = Paths(config=tmp_path / "config", data=tmp_path / "data", state=tmp_path / "state")
    assert read_bootstrap(paths) is None
    write_bootstrap(paths, "op://vault/item/notesPlain", tmp_path / "profiles")
    assert read_bootstrap(paths) == {
        "schema_version": 2,
        "index_ref": "op://vault/item/notesPlain",
        "profiles_dir": str(tmp_path / "profiles"),
    }
    assert paths.bootstrap_path.stat().st_mode & 0o777 == 0o600


def test_independent_first_writers_share_one_stable_lock_file(tmp_path):
    for round in range(20):
        path = tmp_path / str(round) / "history.enc"
        key = Fernet.generate_key()
        with ThreadPoolExecutor(max_workers=8) as workers:
            tasks = [
                workers.submit(
                    EncryptedStore(path, key, "history").update, lambda data, i=i: _append(data, i)
                )
                for i in range(24)
            ]
            for task in tasks:
                task.result()
        assert path.with_name(path.name + ".lock").stat().st_nlink == 1
        state = EncryptedStore(path, key, "history").read()
        assert len(state["seen_configurations"]) == 24


@pytest.mark.parametrize(
    "value",
    [
        {"schema_version": 1, "index_ref": "op://vault/item/notesPlain"},
        {
            "schema_version": 2,
            "index_ref": "op://vault/item/notesPlain",
            "profiles_dir": "relative",
        },
    ],
)
def test_unsupported_bootstrap_is_rejected_without_conversion_or_write(tmp_path, value):
    import json

    paths = Paths(config=tmp_path / "config")
    paths.config.mkdir(mode=0o700)
    raw = json.dumps(value).encode()
    paths.bootstrap_path.write_bytes(raw)
    paths.bootstrap_path.chmod(0o600)
    with pytest.raises(LabError):
        read_bootstrap(paths)
    assert paths.bootstrap_path.read_bytes() == raw
