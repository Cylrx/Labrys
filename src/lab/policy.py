"""Load private creation profiles by their registered cluster identity."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, field_validator

from lab.config import (
    MAX_DOCUMENT_BYTES,
    OPERATION_LABEL,
    LabelKey,
    Model,
    NamespaceName,
    NamespaceRule,
    cluster_name,
    safe_document,
    validate_model,
)
from lab.errors import LabError


class Profile(Model):
    owner_label_key: LabelKey
    namespace_rules: dict[NamespaceName, NamespaceRule] = Field(min_length=1)

    @field_validator("owner_label_key")
    @classmethod
    def owner_key(cls, value: str) -> str:
        if value == OPERATION_LABEL:
            raise ValueError("Owner label conflicts with operation identity")
        return value

    def namespace(self, name: str) -> NamespaceRule:
        """Resolve an explicitly configured namespace without fallback."""
        try:
            return self.namespace_rules[name]
        except KeyError:
            raise LabError(
                "No policy is configured for this namespace in the cluster profile.", 4
            ) from None


def parse_profile(text: str) -> Profile:
    """Validate the same profile format used by the CLI and preparation Skill."""
    return validate_model(Profile, safe_document(text))


def profile_path(directory: Path, cluster_id: str) -> Path:
    """Bind one readable cluster name to one file under an explicit absolute directory."""
    try:
        identity = cluster_name(cluster_id)
    except (ValueError, TypeError, AttributeError):
        raise LabError(
            "Cluster name must use 1–63 lowercase letters, digits or hyphens, "
            "start and end with a letter or digit, and cannot be template.",
            4,
        ) from None
    if not directory.is_absolute():
        raise LabError("The profiles directory must be an absolute path.", 4)
    return directory / f"{identity}.yaml"


def load_profile(directory: Path, cluster_id: str) -> Profile:
    """Read a bounded regular profile file; never search another location."""
    path = profile_path(directory, cluster_id)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise LabError(f"Profile is not a regular file: {path}", 4)
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise LabError(f"Profile exceeds the 1 MiB size limit: {path}", 4)
        text = raw.decode("utf-8")
    except FileNotFoundError:
        raise LabError(
            f"Profile not found: {path}. "
            "Prepare it before adding this cluster or creating a Notebook.",
            4,
        ) from None
    except PermissionError:
        raise LabError(f"Permission denied when reading profile: {path}", 4) from None
    except UnicodeError:
        raise LabError(f"Profile is not valid UTF-8: {path}", 4) from None
    except OSError:
        raise LabError(f"Cannot read profile as a regular file: {path}", 4) from None
    return parse_profile(text)


@dataclass(frozen=True)
class ProfileEntry:
    path: Path
    error: str | None = None

    @property
    def status(self) -> str:
        return "Available" if self.error is None else "Unavailable"

    @property
    def label(self) -> str:
        return self.path.stem if self.error is None else self.path.name


def unregistered_profiles(directory: Path, registered: set[str]) -> list[ProfileEntry]:
    """Inspect unregistered YAML entries without hiding unreadable or malformed profiles."""
    try:
        paths = list(directory.iterdir())
    except OSError:
        raise LabError(
            "Cannot read the profiles directory. Check its path and permissions.", 4
        ) from None
    entries = []
    excluded = {"template.yaml", *(f"{name}.yaml" for name in registered)}
    for path in paths:
        if path.name in excluded or path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        error = None
        try:
            if path.suffix != ".yaml":
                raise LabError("Profile filenames must end in .yaml.", 4)
            load_profile(directory, path.stem)
        except LabError as failure:
            error = str(failure)
        entries.append(ProfileEntry(path, error))
    return sorted(entries, key=lambda entry: (entry.error is not None, entry.path.name))
