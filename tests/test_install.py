"""Validate installer assembly without elevation or a system installation."""

import argparse
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "lab_install", Path(__file__).resolve().parents[1] / "scripts/install.py"
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def test_installer_requires_administrator_before_any_work(monkeypatch):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    with pytest.raises(installer.InstallError, match="administrator"):
        installer.install(argparse.Namespace())


def test_build_command_uses_locked_isolated_copies(tmp_path):
    command = installer.build_command(Path("/trusted/uv"), tmp_path)
    assert command[:4] == ["/trusted/uv", "--no-config", "--no-cache", "sync"]
    for flag in ("--locked", "--no-editable", "--no-managed-python", "--no-python-downloads"):
        assert flag in command
    assert command[command.index("--link-mode") + 1] == "copy"
    assert command[command.index("--python") + 1] == str(tmp_path / "runtime/bin/python3")


def test_wrapper_uses_fixed_release_and_isolated_python():
    value = installer.wrapper(Path("/opt/lab/releases/0.1/env/bin/python"), "lab.credential")
    assert value == (
        '#!/bin/sh\nexec /opt/lab/releases/0.1/env/bin/python -I -m lab.credential "$@"\n'
    )


def test_copy_relocates_internal_links(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "python3.12").write_bytes(b"runtime")
    (source / "python3").symlink_to(source / "python3.12")
    destination = tmp_path / "copied"
    installer.copy_tree(source, destination)
    assert (destination / "python3").resolve() == destination / "python3.12"
    assert not Path(os.readlink(destination / "python3")).is_absolute()


def test_runtime_external_symlinks_are_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    external = tmp_path / "cache-python"
    external.write_bytes(b"external")
    (source / "python3").symlink_to(external)
    with pytest.raises(installer.InstallError, match="External"):
        installer.copy_tree(source, tmp_path / "copied")
    assert not (tmp_path / "copied").exists()


def test_release_writable_files_and_hardlinks_are_rejected(tmp_path):
    program = tmp_path / "program"
    program.write_bytes(b"code")
    program.chmod(0o666)
    with pytest.raises(installer.InstallError, match="Unprotected"):
        installer.inspect_tree(tmp_path, owner=os.getuid())
    program.chmod(0o644)
    os.link(program, tmp_path / "other")
    with pytest.raises(installer.InstallError, match="hard links"):
        installer.inspect_tree(tmp_path, owner=os.getuid())


def test_sealing_removes_user_and_group_write_and_preserves_executability(tmp_path, monkeypatch):
    program = tmp_path / "program"
    program.write_bytes(b"executable")
    program.chmod(0o777)
    data = tmp_path / "data"
    data.write_bytes(b"content")
    data.chmod(0o666)
    ownership = []
    monkeypatch.setattr(installer.os, "chown", lambda *args, **kwargs: ownership.append(args))
    installer.seal_tree(tmp_path)
    assert program.stat().st_mode & 0o777 == 0o755
    assert data.stat().st_mode & 0o777 == 0o644
    assert all(arguments[1:] == (0, 0) for arguments in ownership)


def test_protected_path_rejects_ordinary_user_installation(tmp_path):
    installer.protected(Path("/"))
    with pytest.raises(installer.InstallError, match="Unprotected"):
        installer.protected(tmp_path)


def test_atomic_selection_uses_relative_target_and_retains_old_on_failure(tmp_path, monkeypatch):
    installer.select_release(tmp_path, "one")
    assert os.readlink(tmp_path / "current") == "releases/one"

    def fail(*args, **kwargs):
        raise OSError("simulated interrupted switch")

    monkeypatch.setattr(installer.os, "replace", fail)
    with pytest.raises(OSError):
        installer.select_release(tmp_path, "two")
    assert os.readlink(tmp_path / "current") == "releases/one"
    assert not list(tmp_path.glob(".current-*"))


@pytest.fixture
def assembly(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        (source / name).write_text("fixture")
    (source / "src").mkdir()
    (source / "src/module.py").write_text("fixture")
    runtime = tmp_path / "supplied-runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "lib").mkdir()
    (runtime / "lib/standard-library").write_text("complete-runtime-fixture")
    for path in (runtime / "bin/python3", tmp_path / "uv", tmp_path / "kubectl"):
        path.write_text("fixture")
        path.chmod(0o755)
    args = argparse.Namespace(
        prefix=tmp_path / "installation",
        version="test-1",
        source=source,
        python_runtime=runtime,
        uv=tmp_path / "uv",
        ssh=None,
        code=None,
        kubectl=tmp_path / "kubectl",
    )
    original_inspect = installer.inspect_tree
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer, "protected", lambda path: None)
    monkeypatch.setattr(installer, "seal_tree", original_inspect)
    monkeypatch.setattr(installer, "inspect_tree", lambda path, owner=None: original_inspect(path))
    return args


def test_assembly_manifest_and_launchers_use_final_release_paths(assembly, monkeypatch):
    calls = []
    release = assembly.prefix / "releases/test-1"

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == str(assembly.uv):
            binaries = release / "env/bin"
            binaries.mkdir(parents=True)
            (binaries / "python").symlink_to(release / "runtime/bin/python3")

    monkeypatch.setattr(installer.subprocess, "run", run)
    launcher = installer.install(assembly)
    assert launcher.resolve() == release / "env/bin/lab"
    assert (release / "runtime/lib/standard-library").is_file()
    assert not (release / ".incomplete").exists()
    manifest = json.loads((release / "env/share/lab/tools.json").read_text())
    assert manifest["python"]["path"] == str(release / "env/bin/python")
    assert manifest["credential"]["path"] == str(release / "env/bin/lab-credential")
    assert manifest["kubectl"]["path"] == str(release / "bin/kubectl")
    assert manifest["ssh"] is None and manifest["code"] is None
    assert calls[0][1]["env"]["UV_PROJECT_ENVIRONMENT"] == str(release / "env")
    assert "PYTHONPATH" not in calls[0][1]["env"]
    assert calls[1][0][1:3] == ["-I", "-c"]
    assert " -I -m lab.credential " in (release / "env/bin/lab-credential").read_text()


def test_build_failure_never_selects_incomplete_release(assembly, monkeypatch):
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(installer.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(assembly)
    assert not (assembly.prefix / "current").is_symlink()
    assert (assembly.prefix / "releases/test-1/.incomplete").is_file()
