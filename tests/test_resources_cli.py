"""Resource snapshots have a noninteractive, scoped and credential-free CLI contract."""

import asyncio
import json
import sys
from unittest.mock import AsyncMock

import pytest
from fixtures import CLUSTER_ID
from test_access import authorization as authorization
from test_access_integration import authorization_api as authorization_api
from test_resources import GPU, node, pod
from test_session import api as api

from lab import access, cli
from lab.errors import LabError
from lab.resources import snapshot


def report():
    return snapshot([node()], [pod(cpu="2", memory="4Gi", **{GPU: "3"})], ("research",)).report()


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
async def test_cli_reuses_authorization_and_selects_text_or_json(monkeypatch, capsys, json_flag):
    data = report()
    query = AsyncMock(return_value=data)
    monkeypatch.setattr(access, "request", query)
    monkeypatch.setattr(
        cli.Secrets, "authenticate", AsyncMock(side_effect=AssertionError("No new authentication"))
    )
    args = cli.parser().parse_args(
        [
            "resources",
            "--session",
            "lab-session-example",
            "--cluster",
            "research-example",
            "--namespace",
            "research",
            "--namespace",
            "second",
            *json_flag,
        ]
    )
    result = await cli.run(args)
    query.assert_awaited_once_with(
        "lab-session-example", "resources", "research-example", namespaces=("research", "second")
    )
    assert result["operation"] == "resources" and result["status"] == "partial"
    assert result["data"] == data
    output = capsys.readouterr().out
    if json_flag:
        assert output == ""
    else:
        assert "Cluster resources" in output and "node-a" in output
        assert "≤30" in output and "≤124" in output
        assert "reserved / total" in output


def test_json_stdout_is_one_document_even_with_partial_visibility(monkeypatch, capsys):
    monkeypatch.setattr(access, "request", AsyncMock(return_value=report()))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lab",
            "--json",
            "resources",
            "--session",
            "lab-session-example",
            "--cluster",
            "research-example",
            "--namespace",
            "research",
        ],
    )
    cli.main()
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert len(output.out.splitlines()) == 1 and output.err == ""
    assert value["schema_version"] == 1
    assert value["target"] == {"cluster": "research-example"}
    assert value["data"]["units"] == {"cpu": "millicores", "memory": "bytes", GPU: "units"}


@pytest.mark.parametrize(
    "arguments",
    [
        ["resources"],
        ["resources", "--session", "lab-session-example"],
        [
            "resources",
            "--session",
            "lab-session-example",
            "--cluster",
            "research",
            "--namespace",
            "bad/name",
        ],
        [
            "--request-auth",
            "resources",
            "--session",
            "lab-session-example",
            "--cluster",
            "research",
        ],
        ["--yes", "resources", "--session", "lab-session-example", "--cluster", "research"],
    ],
)
def test_invalid_resource_arguments_fail_without_connecting(arguments):
    with pytest.raises(LabError) as caught:
        cli.validate_arguments(cli.parser().parse_args(arguments))
    assert caught.value.code == 2


def test_resource_error_uses_json_error_contract(monkeypatch, capsys):
    monkeypatch.setattr(
        access, "request", AsyncMock(side_effect=LabError("Session unavailable", 3))
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lab",
            "resources",
            "--session",
            "lab-session-example",
            "--cluster",
            "research-example",
            "--json",
        ],
    )
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 3
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "error"
    assert output.err == ""


async def test_owner_queries_resources_without_kubectl_or_credential_grants(
    authorization, monkeypatch
):
    state = authorization
    value = snapshot([node()], [pod(**{GPU: "3"})], ("research",))
    query = AsyncMock(return_value=value)
    monkeypatch.setattr(access, "load_resources", query)
    original = access.Toolchain.load

    def tools():
        result = original()
        result.require = lambda name: (_ for _ in ()).throw(
            AssertionError("No native client required")
        )
        return result

    monkeypatch.setattr(access.Toolchain, "load", tools)
    data = await access.request(state.identity, "resources", CLUSTER_ID, namespaces=("research",))
    assert data["nodes"][0]["remaining_upper_bound"][GPU] == 5
    assert set(query.await_args.args[1]) == {"research"}
    connection = state.sessions[0]
    assert connection.closed.is_set() and not connection.path.exists()
    assert not state.owner.stopping.is_set()
    assert await access.request(state.identity, "list")
    assert not any(
        key in json.dumps(data) for key in ("kubeconfig", "capability", "synthetic grant")
    )


async def test_disconnect_cancels_resource_query_and_cleans_up(authorization, monkeypatch):
    state = authorization
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def query(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(access, "load_resources", query)
    reader, writer = await asyncio.open_unix_connection(access.socket_path(state.identity))
    writer.write(
        json.dumps({"operation": "resources", "cluster": CLUSTER_ID, "namespaces": []}).encode()
        + b"\n"
    )
    await writer.drain()
    await asyncio.wait_for(entered.wait(), 1)
    writer.close()
    await writer.wait_closed()
    await asyncio.wait_for(stopped.wait(), 1)
    await asyncio.wait_for(state.sessions[0].closed.wait(), 1)


async def test_closing_authorization_cancels_resource_reads(authorization, monkeypatch):
    state = authorization
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def query(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(access, "load_resources", query)
    client = asyncio.create_task(access.request(state.identity, "resources", CLUSTER_ID))
    await asyncio.wait_for(entered.wait(), 1)
    assert await access.request(state.identity, "close") == {"status": "Closed"}
    with pytest.raises(LabError):
        await client
    assert stopped.is_set() and state.sessions[0].closed.is_set()
    assert state.owner.secrets is None


@pytest.mark.parametrize("namespaces", ["research", ["bad/name"], [None], ["secret-value\n"]])
async def test_owner_rejects_invalid_namespace_payload_before_reading_secrets(
    authorization, namespaces
):
    state = authorization
    state.secrets.read.reset_mock()
    reader, writer = await asyncio.open_unix_connection(access.socket_path(state.identity))
    try:
        writer.write(
            json.dumps(
                {"operation": "resources", "cluster": CLUSTER_ID, "namespaces": namespaces}
            ).encode()
            + b"\n"
        )
        await writer.drain()
        result = json.loads(await asyncio.wait_for(reader.readline(), 1))
        assert result["code"] == 2
        assert "secret-value" not in json.dumps(result)
        state.secrets.read.assert_not_awaited()
    finally:
        writer.close()
        await writer.wait_closed()


def test_report_excludes_unready_nodes_from_remaining_totals_and_omits_unsupported_slots():
    active, cordoned = node(), node("node-b", pods="10")
    cordoned["spec"]["unschedulable"] = True
    data = snapshot([active, cordoned], [pod(**{GPU: "2"})], ("research",)).report()
    assert data["scope"] == {"all_namespaces": False, "namespaces": ["research"]}
    assert data["summary"]["nodes"] == 2 and data["summary"]["ready_nodes"] == 1
    assert data["summary"]["allocatable"][GPU] == 16
    assert data["summary"]["remaining_upper_bound"][GPU] == 6
    assert data["nodes"][1]["remaining_upper_bound"] is None
    assert "pods" not in data["nodes"][1]["allocatable"]


def test_no_pod_data_reports_unknown_reservations_and_capacity_ceilings():
    data = snapshot([node()], None, ()).report()
    assert data["partial"] is True
    assert data["summary"]["observed_reserved"] is None
    assert data["nodes"][0]["observed_reserved"] is None
    assert data["nodes"][0]["remaining_upper_bound"][GPU] == 8


def test_report_never_serializes_full_pods_or_unrelated_node_metadata():
    secret = "synthetic-private-value-must-not-be-exported"
    source = node()
    source["metadata"]["labels"]["example.invalid/private"] = secret
    workload = pod(**{GPU: "1"})
    workload["spec"]["containers"][0]["env"] = [{"name": "PRIVATE", "value": secret}]
    data = snapshot([source], [workload], None).report()
    assert secret not in json.dumps(data)
    assert data["scope"] == {"all_namespaces": True, "namespaces": None}
    assert data["partial"] is False


async def test_resources_use_the_real_tls_route_without_exporting_credentials(
    authorization_api, monkeypatch
):
    async def collection(client, path, **params):
        response = await client.request("GET", "/api/synthetic")
        assert response == {"authenticated": True}
        if path == "/api/v1/nodes":
            return [node()]
        assert path == "/api/v1/pods"
        assert (
            params["fieldSelector"]
            == "status.phase!=Succeeded,status.phase!=Failed,spec.nodeName!="
        )
        return [pod(**{GPU: "2"})]

    monkeypatch.setattr(access.Kubernetes, "collection", collection)
    monkeypatch.setattr(
        access.Session,
        "create_grant",
        AsyncMock(side_effect=AssertionError("No credential grant needed")),
    )
    state = authorization_api
    data = await access.request(state.identity, "resources", CLUSTER_ID)
    assert data["summary"]["remaining_upper_bound"][GPU] == 6
    assert data["partial"] is False
    assert state.connection.token.get_secret_value() not in json.dumps(data)
