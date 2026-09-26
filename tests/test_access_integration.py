"""Native kubectl and the real credential helper against a local TLS fixture."""

import asyncio
import base64
import json
import shutil
import ssl
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
import yaml
from fixtures import CLUSTER_ID, EXAMPLES, cluster_data
from test_session import api as api

from lab import access, clock
from lab.config import parse_index


@pytest.fixture
async def authorization_api(api, tmp_path):
    connection, _ = api
    document = cluster_data()
    kubeconfig = yaml.safe_load(document["kubernetes"]["kubeconfig"])
    kubeconfig["clusters"][0]["cluster"] = {
        "server": connection.server,
        "tls-server-name": connection.tls_name,
        "certificate-authority-data": connection.ca_data,
    }
    kubeconfig["users"][0]["user"] = {"token": connection.token.get_secret_value()}
    document["kubernetes"]["kubeconfig"] = yaml.safe_dump(kubeconfig)
    index_text = (EXAMPLES / "index.yaml").read_text()
    index = parse_index(index_text)
    index_ref = "op://fixture/index/notesPlain"
    documents = {index_ref: index_text, index.clusters[0].config_ref: yaml.safe_dump(document)}
    secrets = SimpleNamespace(read=AsyncMock(side_effect=documents.__getitem__))
    owner = access.Authorization(
        secrets, {"index_ref": index_ref, "profiles_dir": str(tmp_path)}, index, clock.now()
    )
    ready = asyncio.get_running_loop().create_future()
    serving = asyncio.create_task(owner.serve(ready.set_result))
    try:
        identity = (await asyncio.wait_for(ready, 2))["session"]
        yield SimpleNamespace(owner=owner, task=serving, identity=identity, connection=connection)
    finally:
        owner.stopping.set()
        await asyncio.wait_for(serving, 2)


async def test_native_kubectl_uses_the_authorized_route_and_private_helper(
    authorization_api, capfd
):
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl is not installed on this test host")
    state = authorization_api
    assert await access.kubectl(state.identity, CLUSTER_ID, ["get", "--raw=/api/synthetic"]) == 0
    output = capfd.readouterr()
    assert json.loads(output.out) == {"authenticated": True}
    assert state.connection.token.get_secret_value() not in output.out + output.err
    assert await access.request(state.identity, "close") == {"status": "Closed"}
    await asyncio.wait_for(state.task, 2)
    assert not access.socket_path(state.identity).parent.exists()


@pytest.mark.parametrize("ending", ["close", "expiry"])
async def test_authorization_shutdown_closes_a_live_real_relay(
    authorization_api, monkeypatch, ending
):
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl is not installed on this test host")
    state = authorization_api
    tls = ssl.create_default_context(cadata=base64.b64decode(state.connection.ca_data).decode())
    closer = None
    async with access._client(state.identity, "kubectl", CLUSTER_ID) as (reader, grant):
        config_path = Path(grant["kubeconfig"])
        config = json.loads(config_path.read_text())
        endpoint = config["clusters"][0]["cluster"]["server"]
        async with aiohttp.ClientSession() as client:
            socket = await client.ws_connect(
                endpoint + "/stream", ssl=tls, server_hostname=state.connection.tls_name
            )
            try:
                await socket.send_str("synthetic ping")
                assert (await socket.receive()).data == "synthetic ping"
                if ending == "close":
                    closer = asyncio.create_task(access.request(state.identity, "close"))
                else:
                    monkeypatch.setattr(clock, "now", lambda: state.owner.deadline + 1)
                await asyncio.wait_for(asyncio.shield(state.task), 2)
                if closer is not None:
                    assert await closer == {"status": "Closed"}
                assert await asyncio.wait_for(reader.read(), 1) == b""
                result = await asyncio.wait_for(socket.receive(), 1)
                assert result.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
                assert state.owner.secrets is None
                assert not config_path.exists()
            finally:
                await socket.close()
                if closer is not None:
                    await asyncio.gather(closer, return_exceptions=True)
