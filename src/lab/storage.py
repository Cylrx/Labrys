"""Private, versioned local documents with serialized atomic updates."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from platformdirs import user_config_path, user_data_path, user_state_path

from lab.errors import LabError

MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_CIPHERTEXT_BYTES = 4 * ((MAX_DOCUMENT_BYTES + 128) // 3 + 1)
LOCK_TIMEOUT = 5.0


@dataclass(frozen=True)
class Paths:
    """Locate the application's platform-specific private directories."""

    config: Path = field(default_factory=lambda: user_config_path("lab", appauthor=False))
    data: Path = field(default_factory=lambda: user_data_path("lab", appauthor=False))
    state: Path = field(default_factory=lambda: user_state_path("lab", appauthor=False))

    @property
    def bootstrap_path(self) -> Path:
        return self.config / "bootstrap.json"

    @property
    def history_path(self) -> Path:
        return self.state / "history.enc"

    @property
    def presets_path(self) -> Path:
        return self.data / "presets.enc"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _decode(raw: bytes) -> dict:
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise LabError("Local document exceeds the 16 MiB limit.", code=8)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise LabError(
            "Local document contains invalid JSON; preserve it for recovery.", code=8
        ) from None
    if not isinstance(value, dict):
        raise LabError("Local document must be a JSON object.", code=8)
    return value


def _reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON number")


def _encode(value: dict) -> bytes:
    try:
        raw = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise LabError("Local state contains unsupported values.", code=8) from None
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise LabError("Local document exceeds the 16 MiB limit; nothing was saved.", code=8)
    return raw


def _check_file(fd: int, limit: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise LabError("Local state must be a regular file owned by the current user.", code=8)
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise LabError("Local state requires mode 0600 and a single filesystem link.", code=8)
    if info.st_size > limit:
        raise LabError("Local state exceeds its size limit.", code=8)


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.absolute().parts[1:]:
            if part == "..":
                raise LabError("Local state paths cannot contain parent traversal.", code=8)
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            except FileNotFoundError:
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise LabError("The local lab directory must be user-owned with mode 0700.", code=8)
        yield descriptor
    except OSError:
        raise LabError(
            "Cannot access private local state; check ownership, permissions and symlinks.", code=8
        ) from None
    finally:
        os.close(descriptor)


@contextmanager
def _locked(path: Path) -> Iterator[int]:
    with _directory(path.parent) as directory:
        name = path.name + ".lock"
        try:
            lock = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
        except FileExistsError:
            lock = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory)
        try:
            _check_file(lock, 0)
            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LabError(
                            "Local state is busy; retry after the other lab process finishes.",
                            code=8,
                        ) from None
                    time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            yield directory
        finally:
            os.close(lock)


def _read(directory: int, name: str, limit: int) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        _check_file(fd, limit)
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > limit:
            raise LabError("Local state exceeds its size limit.", code=8)
        return value
    finally:
        os.close(fd)


def _replace(directory: int, name: str, raw: bytes) -> None:
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        try:
            os.fsync(directory)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
                raise
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def _bootstrap(value: dict) -> dict:
    if (
        set(value) != {"schema_version", "index_ref", "profiles_dir"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 2
        or not isinstance(value["index_ref"], str)
    ):
        raise LabError(
            "Unsupported local configuration. Back up and move the old lab state aside, "
            "then run lab init. No migration is supported.",
            code=8,
        )
    reference = value["index_ref"]
    parts = reference.removeprefix("op://").split("/")
    if (
        not reference.startswith("op://")
        or len(parts) != 3
        or not all(parts)
        or any(c.isspace() for c in reference)
    ):
        raise LabError("Bootstrap requires a valid 1Password index reference.", code=8)
    directory = value["profiles_dir"]
    if (
        not isinstance(directory, str)
        or not Path(directory).is_absolute()
        or ".." in Path(directory).parts
        or any(ord(char) < 32 for char in directory)
    ):
        raise LabError("Bootstrap requires an absolute profiles directory.", code=8)
    return value


def read_bootstrap(paths: Paths) -> dict | None:
    """Read the sole local configuration pointer, or return ``None`` if absent."""
    with _locked(paths.bootstrap_path) as directory:
        raw = _read(directory, paths.bootstrap_path.name, 16384)
        return _bootstrap(_decode(raw)) if raw is not None else None


def write_bootstrap(paths: Paths, index_ref: str, profiles_dir: Path) -> None:
    """Atomically commit a root reference after the caller verifies its data key."""
    value = _bootstrap(
        {"schema_version": 2, "index_ref": index_ref, "profiles_dir": str(profiles_dir)}
    )
    with _locked(paths.bootstrap_path) as directory:
        _read(directory, paths.bootstrap_path.name, 16384)
        _replace(directory, paths.bootstrap_path.name, _encode(value))


class EncryptedStore:
    """Serialize bounded, authenticated read-modify-write transactions.

    :param path: Ciphertext destination under a private directory.
    :param key: A Fernet data key verified by the configuration workflow.
    :param kind: Either ``history`` or ``presets``.
    """

    def __init__(self, path: Path, key: bytes, kind: str):
        if kind not in {"history", "presets"}:
            raise ValueError("Unknown encrypted store kind")
        self.path = path
        self.kind = kind
        try:
            self.cipher = Fernet(key)
        except (ValueError, TypeError):
            raise LabError("The selected data key is not a valid Fernet key.", code=8) from None

    def _document(self, raw: bytes | None) -> dict:
        if raw is None:
            data: dict = (
                {"presets": []}
                if self.kind == "presets"
                else {"field_history": [], "seen_configurations": [], "operations": {}}
            )
            return {"schema_version": 1, "kind": self.kind, "revision": 0, "data": data}
        try:
            document = _decode(self.cipher.decrypt(raw))
        except InvalidToken:
            raise LabError(
                "Cannot decrypt local state: wrong key or damaged file. "
                "Select the original key or restore the encrypted backup; "
                "ciphertext was preserved.",
                code=8,
            ) from None
        if (
            set(document) != {"schema_version", "kind", "revision", "data"}
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
            or document["kind"] != self.kind
            or type(document["revision"]) is not int
            or document["revision"] < 0
        ):
            raise LabError(
                "Encrypted state has an unsupported schema or wrong document kind.", code=8
            )
        self._validate(document["data"])
        return document

    def _validate(self, data: dict) -> None:
        from lab.history import validate_data

        validate_data(self.kind, data)

    def _load(self, directory: int) -> tuple[dict, bytes | None]:
        raw = _read(directory, self.path.name, MAX_CIPHERTEXT_BYTES)
        backup = _read(directory, self.path.name + ".bak", MAX_CIPHERTEXT_BYTES)
        if backup is not None:
            self._document(backup)
            if raw is None:
                raise LabError(
                    "The primary state file is missing; "
                    "restore its encrypted backup before continuing.",
                    code=8,
                )
        return self._document(raw), raw

    def read(self) -> dict:
        """Return validated data while preserving malformed or unreadable files."""
        with _locked(self.path) as directory:
            document, _ = self._load(directory)
            return document["data"]

    def update(self, change: Callable[[dict], None]) -> dict:
        """Apply ``change`` to freshly read data and commit one new revision."""
        with _locked(self.path) as directory:
            document, previous = self._load(directory)
            change(document["data"])
            self._validate(document["data"])
            document["revision"] += 1
            ciphertext = self.cipher.encrypt(_encode(document))
            if previous is not None:
                _replace(directory, self.path.name + ".bak", previous)
            _replace(directory, self.path.name, ciphertext)
            return document["data"]
