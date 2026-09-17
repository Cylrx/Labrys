import asyncio
import base64
import ssl
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import SecretStr

from lab import clock
from lab.errors import LabError
from lab.session import Session, Tools


@pytest.fixture
async def api(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.invalid")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("synthetic.invalid")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(pem)
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_file, key_file)
    app = web.Application()

    async def version(request):
        assert "Authorization" not in request.headers
        return web.json_response({"gitVersion": "synthetic"})

    async def stream(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            await socket.send_str(message.data)
        return socket

    async def authenticated(request):
        if request.headers.get("Authorization") != "Bearer synthetic-kubernetes-token":
            return web.Response(status=401)
        return web.json_response({"authenticated": True})

    app.router.add_get("/version", version)
    app.router.add_get("/stream", stream)
    app.router.add_get("/api/synthetic", authenticated)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    connection = SimpleNamespace(
        server=f"https://127.0.0.1:{port}",
        tls_name="synthetic.invalid",
        ca_data=base64.b64encode(pem).decode(),
        token=SecretStr("synthetic-kubernetes-token"),
    )
    yield connection, runner
    await runner.cleanup()


def make_session(connection, **kwargs):
    return Session(
        connection,
        SimpleNamespace(mode="direct", ssh_target=None),
        kwargs.pop("max_age_seconds", 30),
        Tools(ssh=None, python=Path(sys.executable), credential=Path("/protected/lab-credential")),
        cluster_id="synthetic-cluster",
        **kwargs,
    )


async def test_verified_tls_and_stable_endpoint_after_explicit_reconnect(api):
    connection, _ = api
    async with make_session(connection) as session:
        endpoint, deadline = session.endpoint, session.deadline
        grant = await session.create_grant()
        await session.disconnect()
        assert session.state == "disconnected"
        with pytest.raises(LabError, match="disconnected"):
            session.check()
        with pytest.raises(LabError, match="Reconnect"):
            await session.connect()
        reader, writer = await asyncio.open_connection("127.0.0.1", session._relay.port)
        assert await asyncio.wait_for(reader.read(), 1) == b""
        writer.close()
        await session.reconnect()
        assert session.endpoint == endpoint
        assert session.deadline == deadline
        assert session.token == connection.token
        assert grant.config_path.exists()
    assert not grant.config_path.exists()
    assert not grant.environment
    with pytest.raises(LabError):
        _ = session.token


async def test_tls_identity_mismatch_fails_closed(api):
    connection, _ = api
    connection.tls_name = "another.invalid"
    session = make_session(connection)
    try:
        with pytest.raises(LabError, match="verified"):
            await session.connect()
        assert session.state == "disconnected"
        assert session._relay.destination is None
    finally:
        await session.close()


async def test_existing_stream_closes_on_disconnect_other_session_survives(api):
    connection, _ = api
    async with (
        make_session(connection) as first,
        make_session(connection) as second,
        aiohttp.ClientSession() as client,
    ):
        socket = await client.ws_connect(
            first.endpoint + "/stream",
            ssl=first.ssl_context,
            server_hostname=first.tls_name,
        )
        await socket.send_str("synthetic ping")
        assert (await socket.receive()).data == "synthetic ping"
        await first.disconnect()
        result = await asyncio.wait_for(socket.receive(), 1)
        assert result.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE)
        await second.check_health()
        assert second.state == "connected"


async def test_suspend_inclusive_expiry_closes_without_next_operation(api, monkeypatch):
    connection, _ = api
    instant = [clock.now()]
    monkeypatch.setattr(clock, "now", lambda: instant[0])
    session = make_session(connection)
    await session.connect()
    grant = await session.create_grant()
    instant[0] += 31
    await asyncio.sleep(0.3)
    assert session.state == "closed"
    assert session._token is None
    assert not grant.config_path.exists()
    with pytest.raises(LabError, match="expired"):
        session.check()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", session._relay.port)


async def test_authorization_deadline_includes_configuration_fetch(api):
    connection, _ = api
    session = make_session(connection, authorized_at=clock.now() - 31)
    with pytest.raises(LabError, match="expired"):
        await session.connect()
    await session.close()


async def test_confirmed_outage_disconnects_and_requires_reconnect(api):
    connection, runner = api
    async with make_session(connection) as session:
        await runner.cleanup()
        with pytest.raises(LabError, match="lost"):
            await session.check_health()
        assert session.state == "disconnected"
        with pytest.raises(LabError):
            await session.create_grant()


async def test_ordinary_stream_eof_does_not_disconnect(api):
    connection, _ = api
    async with make_session(connection) as session:
        reader, writer = await asyncio.open_connection("127.0.0.1", session._relay.port)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)
        session.check()
        await session.check_health()


def test_clock_moves_forward_without_wallclock(monkeypatch):
    before = clock.now()
    monkeypatch.setattr("time.time", lambda: -1_000_000)
    after = clock.now()
    assert after >= before > 0


async def test_installed_kubectl_exec_plugin_uses_only_its_grant(api):
    import json
    import shutil

    from lab.tools import client_environment

    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("kubectl is not installed on this test host")
    connection, _ = api
    session = make_session(connection)
    session.tools = Tools(
        None, Path(sys.executable), Path(sys.executable).parent / "lab-credential"
    )
    session._credentials._helper = session.tools.credential
    async with session:
        grant = await session.create_grant()
        assert connection.token.get_secret_value() not in grant.config_path.read_text()

        async def request():
            process = await asyncio.create_subprocess_exec(
                kubectl,
                "--kubeconfig",
                str(grant.config_path),
                "--request-timeout=3s",
                "get",
                "--raw=/api/synthetic",
                env=client_environment(grant.environment),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            assert connection.token.get_secret_value().encode() not in stdout + stderr
            return process.returncode, stdout

        code, stdout = await request()
        assert code == 0
        assert json.loads(stdout) == {"authenticated": True}
        await session.disconnect()
        code, _ = await request()
        assert code != 0
