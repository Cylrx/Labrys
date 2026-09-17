import asyncio
import json
import stat
import sys
from datetime import UTC, datetime

import aiohttp
import pytest
from test_session import api as api
from test_session import make_session

from lab.credential import CAPABILITY_ENV, PROTOCOL, _fetch
from lab.errors import LabError


def request_for(grant):
    config = json.loads(grant.config_path.read_text())
    return {"grant_id": grant.id, "cluster": config["clusters"][0]["cluster"]}


async def post(session, grant, *, capability=None, payload=None):
    connector = aiohttp.UnixConnector(path=str(session._credentials.socket_path))
    async with (
        aiohttp.ClientSession(connector=connector) as client,
        client.post(
            "http://localhost/v1/credential",
            json=request_for(grant) if payload is None else payload,
            headers={
                "Authorization": "Bearer "
                + (grant.environment[CAPABILITY_ENV] if capability is None else capability)
            },
        ) as response,
    ):
        return response.status, await response.json()


async def test_valid_grant_token_only_in_response_and_limited_cache(api):
    connection, _ = api
    async with make_session(connection) as session:
        grant = await session.create_grant()
        status, response = await post(session, grant)
        assert status == 200
        assert response["status"]["token"] == connection.token.get_secret_value()
        expires = datetime.fromisoformat(response["status"]["expirationTimestamp"])
        assert 0 < (expires - datetime.now(UTC)).total_seconds() <= session.remaining + 1
        raw = grant.config_path.read_text()
        assert connection.token.get_secret_value() not in raw
        assert grant.environment[CAPABILITY_ENV] not in raw
        assert grant.environment[CAPABILITY_ENV] not in repr(grant)
        assert stat.S_IMODE(grant.config_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(grant.config_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(session._credentials.socket_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("capability", ["", "wrong", "X" * 257])
async def test_wrong_or_missing_capability_denied(api, capability):
    connection, _ = api
    async with make_session(connection) as session:
        grant = await session.create_grant()
        status, response = await post(session, grant, capability=capability)
        assert status == 403
        assert response == {"error": "denied"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("server", "https://another.invalid"),
        ("tls-server-name", "another.invalid"),
        ("certificate-authority-data", "c3ludGhldGlj"),
        ("insecure-skip-tls-verify", True),
        ("proxy-url", "http://another.invalid"),
    ],
)
async def test_cluster_identity_mismatch_denied(api, field, value):
    connection, _ = api
    async with make_session(connection) as session:
        grant = await session.create_grant()
        payload = request_for(grant)
        payload["cluster"][field] = value
        assert (await post(session, grant, payload=payload))[0] == 403


async def test_grants_cross_session_revocation_and_disconnection(api):
    connection, _ = api
    async with make_session(connection) as first, make_session(connection) as second:
        first_grant = await first.create_grant()
        second_grant = await second.create_grant()
        assert (await post(second, first_grant))[0] == 403
        capability = first_grant.environment[CAPABILITY_ENV]
        payload = request_for(first_grant)
        await first.disconnect()
        assert (await post(first, first_grant))[0] == 403
        await first.reconnect()
        assert (await post(first, first_grant))[0] == 200
        await first.revoke_grant(first_grant)
        assert not first_grant.config_path.exists()
        assert (await post(first, first_grant, capability=capability, payload=payload))[0] == 403
        assert (await post(second, second_grant))[0] == 200


async def test_body_limits_and_unknown_fields_fail_without_secrets(api):
    connection, _ = api
    async with make_session(connection) as session:
        grant = await session.create_grant()
        payload = request_for(grant)
        payload["secret-ref"] = "synthetic-secret-in-malformed-request"
        status, response = await post(session, grant, payload=payload)
        assert status == 400
        assert response == {"error": "malformed"}
        connector = aiohttp.UnixConnector(path=str(session._credentials.socket_path))
        async with (
            aiohttp.ClientSession(connector=connector) as client,
            client.post(
                "http://localhost/v1/credential",
                data=b"X" * 65537,
                headers={"Content-Type": "application/json"},
            ) as reply,
        ):
            assert reply.status == 413
            assert await reply.json() == {"error": "malformed"}


async def test_helper_v1_validation_and_capture(api):
    connection, _ = api
    async with make_session(connection) as session:
        grant = await session.create_grant()
        info = json.dumps(
            {
                "apiVersion": PROTOCOL,
                "kind": "ExecCredential",
                "spec": {"interactive": False, "cluster": request_for(grant)["cluster"]},
            }
        )
        result = await _fetch(
            session._credentials.socket_path, grant.id, grant.environment[CAPABILITY_ENV], info
        )
        assert result["status"]["token"] == connection.token.get_secret_value()
        with pytest.raises(LabError, match="v1"):
            await _fetch(
                session._credentials.socket_path,
                grant.id,
                grant.environment[CAPABILITY_ENV],
                info.replace(PROTOCOL, "client.authentication.k8s.io/v1beta1"),
            )


async def test_helper_subprocess_stdout_and_stderr(api):
    connection, _ = api
    async with make_session(connection) as session:
        grant = await session.create_grant()
        info = json.dumps(
            {
                "apiVersion": PROTOCOL,
                "kind": "ExecCredential",
                "spec": {"interactive": False, "cluster": request_for(grant)["cluster"]},
            }
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "lab.credential",
            "--socket",
            str(session._credentials.socket_path),
            "--grant",
            grant.id,
            env={CAPABILITY_ENV: grant.environment[CAPABILITY_ENV], "KUBERNETES_EXEC_INFO": info},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate()
        assert process.returncode == 0
        assert not error
        assert json.loads(output)["status"]["token"] == connection.token.get_secret_value()


async def test_helper_refuses_a_terminal_without_disclosing_secret(api):
    import os
    import pty

    connection, _ = api
    async with make_session(connection) as session:
        grant = await session.create_grant()
        master, slave = pty.openpty()
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                "lab.credential",
                "--socket",
                str(session._credentials.socket_path),
                "--grant",
                grant.id,
                env={CAPABILITY_ENV: grant.environment[CAPABILITY_ENV]},
                stdout=slave,
                stderr=asyncio.subprocess.PIPE,
            )
            _, error = await process.communicate()
            assert process.returncode == 3
            assert connection.token.get_secret_value().encode() not in error
            assert grant.environment[CAPABILITY_ENV].encode() not in error
            assert b"unavailable" in error
        finally:
            os.close(slave)
            os.close(master)


async def test_helper_redacts_invalid_arguments():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "lab.credential",
        "--bad-synthetic-secret",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await process.communicate()
    assert process.returncode == 3
    assert not output
    assert b"bad-synthetic-secret" not in error
    assert error == b"lab: credential grant unavailable; return to the authorized session.\n"


@pytest.mark.parametrize("oversized", [False, True])
async def test_helper_reads_complete_chunked_response(tmp_path, oversized):
    import os
    import shutil
    import tempfile
    from pathlib import Path

    from aiohttp import web

    directory = Path(tempfile.mkdtemp(prefix="lab-test-", dir="/tmp"))
    socket_path = directory / "credential.sock"
    result = {
        "apiVersion": PROTOCOL,
        "kind": "ExecCredential",
        "status": {
            "token": "X" * 70000 if oversized else "synthetic-chunked",
            "expirationTimestamp": "2026-09-14T00:00:00Z",
        },
    }

    async def respond(request):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        data = json.dumps(result).encode()
        await response.write(data[:15])
        await asyncio.sleep(0.02)
        await response.write(data[15:])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/credential", respond)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.UnixSite(runner, str(socket_path)).start()
    os.chmod(socket_path, 0o600)
    try:
        fetch = _fetch(
            socket_path,
            "synthetic",
            "synthetic-capability",
            json.dumps(
                {
                    "apiVersion": PROTOCOL,
                    "kind": "ExecCredential",
                    "spec": {
                        "interactive": False,
                        "cluster": {
                            "server": "https://127.0.0.1:1",
                            "tls-server-name": "synthetic.invalid",
                        },
                    },
                }
            ),
        )
        if oversized:
            with pytest.raises(LabError, match="response"):
                await fetch
        else:
            assert await fetch == result
    finally:
        await runner.cleanup()
        shutil.rmtree(directory)
