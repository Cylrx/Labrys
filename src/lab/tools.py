"""Local executable selection and credential-safe subprocess environments."""

import ctypes
import hashlib
import json
import os
import resource
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from lab.errors import LabError


def protect_process() -> None:
    """Disable core dumps and Linux process dumpability before receiving credentials."""
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if sys.platform.startswith("linux"):
            library = ctypes.CDLL(None, use_errno=True)
            prctl = library.prctl
            prctl.argtypes = [
                ctypes.c_int,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
            ]
            prctl.restype = ctypes.c_int
            if prctl(4, 0, 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), "Process protection failed")
    except (OSError, ValueError, AttributeError):
        raise LabError("Required process dump protection is unavailable on this host.", 9) from None


@dataclass(frozen=True)
class Toolchain:
    python: Path
    credential: Path
    ssh: Path | None
    kubectl: Path | None
    code: Path | None

    @classmethod
    def load(cls) -> "Toolchain":
        prefix = Path(sys.prefix)
        manifest = prefix / "share/lab/tools.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text())
                values: dict[str, Path | None] = {}
                for name in ("python", "credential", "ssh", "kubectl", "code"):
                    entry = data[name]
                    if entry is None:
                        values[name] = None
                        continue
                    path = Path(entry["path"])
                    if name in {"python", "credential", "kubectl"}:
                        for ancestor in (path, *path.parents):
                            mode = ancestor.stat()
                            if mode.st_uid != 0 or mode.st_mode & 0o022:
                                raise ValueError
                        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                            raise ValueError
                    values[name] = path
                python, credential = values["python"], values["credential"]
                if python is None or credential is None:
                    raise ValueError
                return cls(python, credential, values["ssh"], values["kubectl"], values["code"])
            except (OSError, ValueError, KeyError, TypeError):
                raise LabError(
                    "Protected installation verification failed. Reinstall the release.", 9
                ) from None

        def executable(name: str) -> Path | None:
            value = shutil.which(name)
            return Path(value).absolute() if value else None

        return cls(
            Path(sys.executable),
            prefix / "bin/lab-credential",
            executable("ssh"),
            executable("kubectl"),
            executable("code"),
        )

    def require(self, name: str) -> Path:
        path = getattr(self, name)
        if path is None or not path.is_file() or not os.access(path, os.X_OK):
            raise LabError(
                f"The local {name} executable is unavailable. Configure the installation.", 9
            )
        return path


def client_environment(additions: dict[str, str] | None = None) -> dict[str, str]:
    """Pass desktop/session context without ambient credential-provider settings."""
    names = {
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "TERM",
        "COLORTERM",
        "LANG",
        "SHELL",
        "TMPDIR",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "SSH_AUTH_SOCK",
        "DBUS_SESSION_BUS_ADDRESS",
    }
    result = {
        key: value for key, value in os.environ.items() if key in names or key.startswith("LC_")
    }
    if additions:
        result.update(additions)
    return result
