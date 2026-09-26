"""Public CLI contracts for reusable authorization and native kubectl execution."""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fixtures import EXAMPLES

from lab import auth, cli
from lab.errors import LabError


async def test_start_authorizes_without_a_cluster_or_notebook_history(monkeypatch, capsys):
    bootstrap = {"index_ref": "op://vault/index/notesPlain", "profiles_dir": "/synthetic/profiles"}
    source = SimpleNamespace(read=AsyncMock(return_value=(EXAMPLES / "index.yaml").read_text()))
    authenticate = AsyncMock(return_value=source)
    request_token = AsyncMock(return_value="synthetic-service-token")
    monkeypatch.setattr(cli, "read_bootstrap", lambda paths: bootstrap)
    monkeypatch.setattr(cli.Secrets, "authenticate", authenticate)
    monkeypatch.setattr(auth, "request_token", request_token)

    class Owner:
        reason = "explicit close"

        def __init__(self, secrets, configuration, index, authorized_at):
            assert secrets is source
            assert configuration == bootstrap
            assert index.clusters

        async def serve(self, ready):
            ready({"session": "lab-session-example", "idle_timeout": 900})

    monkeypatch.setattr(cli.access, "Authorization", Owner)
    args = cli.parser().parse_args(["session", "start", "--request-auth", "--json"])
    assert await cli.run(args) is None
    request_token.assert_awaited_once_with({"command": "session", "action": "start"})
    authenticate.assert_awaited_once_with("synthetic-service-token")
    source.read.assert_awaited_once_with(bootstrap["index_ref"])
    output = capsys.readouterr()
    assert json.loads(output.out)["data"]["session"] == "lab-session-example"
    assert "synthetic-service-token" not in output.out + output.err


def test_native_exit_status_and_remote_json_option_are_not_reinterpreted(monkeypatch, capsys):
    arguments = ["exec", "pod", "--", "program", "--json"]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lab",
            "kubectl",
            "--session",
            "lab-session-example",
            "--cluster",
            "research",
            "--",
            *arguments,
        ],
    )
    execute = AsyncMock(return_value=17)
    monkeypatch.setattr(cli.access, "kubectl", execute)
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 17
    execute.assert_awaited_once_with("lab-session-example", "research", arguments)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "arguments",
    [
        ["--token-stdin", "session", "start", "--request-auth"],
        ["--request-auth", "session", "close", "lab-session-example"],
        ["--yes", "session", "start"],
        ["kubectl", "--session", "lab-session-example", "--cluster", "research", "get", "pods"],
        [
            "--json",
            "kubectl",
            "--session",
            "lab-session-example",
            "--cluster",
            "research",
            "--",
            "get",
            "pods",
        ],
    ],
)
def test_invalid_access_flags_are_rejected_before_authentication(arguments):
    with pytest.raises(LabError) as error:
        cli.validate_arguments(cli.parser().parse_args(arguments))
    assert error.value.code == 2


async def test_metadata_queries_use_the_existing_owner_without_new_authentication(
    monkeypatch, capsys
):
    query = AsyncMock(return_value={"clusters": [{"id": "research"}]})
    monkeypatch.setattr(cli.access, "request", query)
    args = cli.parser().parse_args(
        ["cluster", "list", "--session", "lab-session-example", "--json"]
    )
    result = await cli.run(args)
    assert result["data"] == {"clusters": [{"id": "research"}]}
    query.assert_awaited_once_with("lab-session-example", "list", None)
    assert capsys.readouterr().out == ""


async def test_foreground_authorization_ready_output_and_sigterm_cleanup():
    program = """
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from lab import cli
cli.read_bootstrap = lambda paths: {
    "index_ref": "op://fixture/index/notesPlain", "profiles_dir": "/synthetic/profiles"
}
index = "schema_version: 1\\ndata_key_ref: op://fixture/key/password\\nclusters: []\\n"
cli.Secrets.authenticate = AsyncMock(
    return_value=SimpleNamespace(read=AsyncMock(return_value=index))
)
sys.argv = ["lab", "session", "start", "--token-stdin", "--json"]
cli.main()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        program,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        process.stdin.write(b"synthetic-session-token\n")
        await process.stdin.drain()
        process.stdin.close()
        ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert ready["status"] == "Ready"
        identity = ready["data"]["session"]
        assert await cli.access.request(identity, "list") == {"clusters": []}
        process.terminate()
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 130
        assert b"synthetic-session-token" not in stdout + stderr
        assert not cli.access.socket_path(identity).parent.exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("partial", [b"", b"synthetic-partial-token"])
async def test_sigterm_cancels_pending_stdin_token_without_closing_the_pipe(partial):
    program = """
import sys
from lab import cli
cli.read_bootstrap = lambda paths: {"index_ref": "op://fixture/index/notesPlain"}
original = cli.read_token
async def read_token(from_stdin):
    print("WAITING_FOR_TOKEN", file=sys.stderr, flush=True)
    return await original(from_stdin)
cli.read_token = read_token
sys.argv = ["lab", "session", "start", "--token-stdin", "--json"]
cli.main()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        program,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stderr.readline(), 5) == b"WAITING_FOR_TOKEN\n"
        process.stdin.write(partial)
        await process.stdin.drain()
        await asyncio.sleep(0.05)
        process.terminate()
        await asyncio.wait_for(process.wait(), 2)
        assert process.returncode == 130
        output = await process.stdout.read() + await process.stderr.read()
        assert b"synthetic-partial-token" not in output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        process.stdin.close()


@pytest.mark.parametrize("source", ["pipe", "file"])
async def test_token_input_consumes_one_line_and_preserves_remaining_stdin(tmp_path, source):
    program = """
import asyncio, sys
from lab.cli import read_token
assert asyncio.run(read_token(True)) == "synthetic-token"
assert sys.stdin.buffer.readline() == b"remaining input\\n"
print("OK")
"""
    payload = b"synthetic-token\r\nremaining input\n"
    path = tmp_path / "synthetic-stdin"
    path.write_bytes(payload)
    with path.open("rb") as stream:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            stdin=asyncio.subprocess.PIPE if source == "pipe" else stream,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload if source == "pipe" else None), 5
        )
    assert process.returncode == 0
    assert stdout == b"OK\n"
    assert not stderr


@pytest.mark.parametrize(
    "payload, code",
    [
        (b"x" * 16384 + b"\r\n", 0),
        (b"x" * 16385 + b"\n", 3),
        (b"\xff\n", 3),
        (b"", 3),
    ],
)
async def test_token_input_byte_limits_and_encoding(payload, code):
    program = """
import asyncio, sys
from lab.cli import read_token
from lab.errors import LabError
try:
    asyncio.run(read_token(True))
except LabError as error:
    sys.exit(error.code)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(payload), 5)
    assert process.returncode == code
    assert not stdout and not stderr
