"""Synthetic token handoff over real local Unix sockets; no external authorization."""

import asyncio
import json
import os
import socket
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lab import auth, cli
from lab.errors import LabError

ACTION = {"command": "notebook", "action": "list", "cluster": "fixture", "namespace": "research"}
SECRET = "synthetic-auth-marker-never-a-credential"


async def pending(monkeypatch, tmp_path, *, lifetime=3, action=None):
    paths = []
    original = auth.tempfile.TemporaryDirectory

    def temporary(**kwargs):
        result = original(prefix="lab-auth-", dir="/tmp")
        paths.append(Path(result.name).resolve() / "request.json")
        return result

    monkeypatch.setattr(auth.tempfile, "TemporaryDirectory", temporary)
    task = asyncio.create_task(auth.request_token(action or ACTION, lifetime=lifetime))
    for _ in range(100):
        if paths and paths[0].exists():
            return task, paths[0]
        await asyncio.sleep(0.005)
    raise AssertionError("Request was not created")


async def connect(path):
    manifest = json.loads(path.read_text())
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.setblocking(False)
    loop = asyncio.get_running_loop()
    await loop.sock_connect(connection, str(path.parent / "socket"))
    await loop.sock_sendall(
        connection, auth._encode({"version": 1, "id": manifest["id"], "type": "hello"})
    )
    await auth._receive(connection, loop.time() + 2)
    return connection, manifest


async def test_handoff_is_once_and_removes_metadata(monkeypatch, tmp_path, capsys):
    task, path = await pending(monkeypatch, tmp_path)
    assert SECRET not in path.read_text()
    monkeypatch.setattr(auth, "hidden_token", lambda deadline: SECRET)
    await asyncio.to_thread(auth.send_token, str(path))
    assert await task == SECRET
    assert not path.parent.exists()
    with pytest.raises(LabError, match="no token was sent"):
        auth.send_token(str(path))
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert not list(tmp_path.rglob("*"))


@pytest.mark.parametrize("mode", ["cancel", "timeout", "malformed", "wrong_uid"])
async def test_failed_request_cleanup(monkeypatch, tmp_path, mode):
    task, path = await pending(monkeypatch, tmp_path, lifetime=0.2 if mode == "timeout" else 3)
    if mode == "cancel":
        task.cancel()
    elif mode == "wrong_uid":
        monkeypatch.setattr(auth, "peer_uid", lambda connection: os.getuid() + 1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(path.parent / "socket"))
    elif mode == "malformed":
        connection, manifest = await connect(path)
        with connection:
            await asyncio.get_running_loop().sock_sendall(
                connection, b'{"token":"' + SECRET.encode() + b'"}\n'
            )
    with pytest.raises((LabError, asyncio.CancelledError)):
        await task
    assert not path.parent.exists()


@pytest.mark.parametrize(
    "change", ["symlink", "replace", "stale", "permissions", "key", "unknown_action"]
)
async def test_invalid_locator_never_prompts(monkeypatch, tmp_path, change):
    task, path = await pending(monkeypatch, tmp_path)
    prompt = []
    monkeypatch.setattr(auth, "hidden_token", lambda deadline: prompt.append(True))
    target = path
    if change == "symlink":
        target = tmp_path / "link"
        target.symlink_to(path)
    elif change == "replace":
        path.unlink()
        path.write_text("{}")
        path.chmod(0o600)
    elif change == "permissions":
        path.chmod(0o644)
    else:
        data = json.loads(path.read_text())
        if change == "stale":
            data["expires"] = time.time() - 1
        elif change == "key":
            data["action"]["\x1b[2J"] = "attack"
        else:
            data["action"]["action"] = "arbitrary"
        path.write_text(json.dumps(data))
    try:
        with pytest.raises(LabError):
            auth.send_token(str(target))
        assert not prompt
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_ack_loss_is_explicit_and_never_retransmits(monkeypatch, tmp_path, capsys):
    task, path = await pending(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "hidden_token", lambda deadline: SECRET)
    original = auth._read
    reads = 0

    def lost_ack(connection, deadline):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise OSError
        return original(connection, deadline)

    monkeypatch.setattr(auth, "_read", lost_ack)
    with pytest.raises(LabError, match="outcome unknown"):
        await asyncio.to_thread(auth.send_token, str(path))
    assert await task == SECRET
    assert reads == 2
    assert SECRET not in str(capsys.readouterr())


def test_protocol_duplicate_keys_and_nonfinite_values_refused():
    with pytest.raises(LabError):
        auth._decode(b'{"type":"hello","type":"token"}')
    value = {
        "version": 1,
        "id": "1" * 32,
        "expires": time.time() + 60,
        "action": {**ACTION, "timeout": float("nan")},
    }
    with pytest.raises(LabError):
        auth._validate_manifest(value)


def test_hidden_input_never_falls_back_to_stdin(monkeypatch):
    def no_tty(*args):
        raise OSError

    monkeypatch.setattr(auth.os, "open", no_tty)
    with pytest.raises(LabError, match="controlling terminal"):
        auth.hidden_token(time.monotonic() + 2)


def test_auth_bypasses_bootstrap_and_1password(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "send_token", lambda path: calls.append(path))
    monkeypatch.setattr(cli, "read_bootstrap", lambda *args: pytest.fail("bootstrap accessed"))
    authenticate = AsyncMock(side_effect=AssertionError("1Password accessed"))
    monkeypatch.setattr(cli.Secrets, "authenticate", authenticate)
    monkeypatch.setattr(cli.sys, "argv", ["lab", "auth", "--request", "/tmp/explicit/request.json"])
    cli.main()
    assert calls == ["/tmp/explicit/request.json"]
    authenticate.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        ["notebook", "list", "--request-auth"],
        [
            "notebook",
            "shell",
            "bad\x1b",
            "--namespace",
            "research",
            "--cluster",
            "fixture",
            "--request-auth",
        ],
        [
            "notebook",
            "create",
            "--name",
            "valid",
            "--namespace",
            "research",
            "--cluster",
            "fixture",
            "--request-auth",
        ],
        [
            "--request-auth",
            "notebook",
            "list",
            "--token-stdin",
            "--namespace",
            "research",
            "--cluster",
            "fixture",
        ],
        [
            "notebook",
            "editor-restart",
            "valid",
            "--namespace",
            "research",
            "--cluster",
            "fixture",
            "--helper-python",
            "relative",
            "--request-auth",
        ],
    ],
)
async def test_invalid_commands_fail_before_creating_request(monkeypatch, arguments):
    request = AsyncMock(side_effect=AssertionError("auth requested"))
    monkeypatch.setattr(auth, "request_token", request)
    with pytest.raises(LabError):
        await cli.run(cli.parser().parse_args(arguments))
    request.assert_not_called()


def test_auth_request_displays_the_same_readable_cluster_identity():
    args = cli.parser().parse_args(
        [
            "notebook",
            "list",
            "--cluster",
            "research-h200",
            "--namespace",
            "research",
            "--request-auth",
        ]
    )
    cli.validate_arguments(args)
    assert auth.description(args)["cluster"] == "research-h200"


async def test_byte_identical_replacement_is_rejected_before_prompt(monkeypatch, tmp_path):
    task, path = await pending(monkeypatch, tmp_path)
    original = path.read_bytes()
    replacement = path.with_name("replacement")
    replacement.write_bytes(original)
    replacement.chmod(0o600)
    replacement.replace(path)
    monkeypatch.setattr(
        auth, "hidden_token", lambda deadline: pytest.fail("Prompted after replacement")
    )
    with pytest.raises(LabError):
        await asyncio.to_thread(auth.send_token, str(path))
    with pytest.raises(LabError):
        await task
    assert not path.parent.exists()


async def test_reusable_session_handoff_displays_its_scope_without_exposing_token(
    monkeypatch, tmp_path, capsys
):
    args = cli.parser().parse_args(["session", "start", "--request-auth", "--json"])
    cli.validate_arguments(args)
    task, path = await pending(monkeypatch, tmp_path, action=auth.description(args))
    monkeypatch.setattr(auth, "hidden_token", lambda deadline: SECRET)
    await asyncio.to_thread(auth.send_token, str(path))
    assert await task == SECRET
    output = capsys.readouterr()
    assert "Authorize reusable lab access" in output.err
    assert "indexed clusters" in output.err
    assert SECRET not in output.out + output.err
    assert not path.parent.exists()
