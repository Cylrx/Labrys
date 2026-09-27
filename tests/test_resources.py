"""Node headroom remains explicit about reservations, visibility and unsupported accounting."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from test_kubernetes import api_server as api_server

from lab.config import quantity
from lab.errors import LabError
from lab.kubernetes import ApiError
from lab.resources import load_resources, pod_requests, quantities, snapshot

GPU = "vendor.example/accelerator"


def node(name="node-a", **resources):
    return {
        "metadata": {"name": name, "labels": {GPU + ".product": "Example"}},
        "spec": {},
        "status": {
            "allocatable": {"cpu": "32", "memory": "128Gi", GPU: "8", **resources},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def container(name="main", **resources):
    return {"name": name, "resources": {"requests": resources}}


def pod(name="work", **resources):
    return {
        "metadata": {"name": name, "namespace": "research", "uid": name},
        "spec": {"nodeName": "node-a", "containers": [container(**resources)]},
        "status": {"phase": "Running"},
    }


def test_api_zero_is_supported_without_weakening_creation_validation():
    assert quantities({"cpu": "0m", "memory": "0Gi", GPU: "0"}) == {"cpu": 0, "memory": 0, GPU: 0}
    assert quantities({"cpu": "1.25", "memory": "1024Mi", GPU: "2e0"}) == {
        "cpu": 1250,
        "memory": 1024**3,
        GPU: 2,
    }
    with pytest.raises(ValueError):
        quantity("0", cpu=True)


def test_init_sidecar_overlap_and_overhead_are_counted_once():
    workload = pod(cpu="2", memory="1Gi", **{GPU: "2"})
    workload["spec"]["containers"].append(container("helper", cpu="500m"))
    workload["spec"]["initContainers"] = [
        container("prepare", cpu="4"),
        {**container("sidecar", cpu="1"), "restartPolicy": "Always"},
        container("download", cpu="8"),
        {**container("metrics", cpu="500m"), "restartPolicy": "Always"},
    ]
    workload["spec"]["overhead"] = {"cpu": "100m", "memory": "64Mi"}
    workload["status"]["initContainerStatuses"] = [
        {"name": "prepare", "allocatedResources": {}, "state": {"terminated": {"exitCode": 0}}}
    ]
    assert pod_requests(workload) == {"cpu": 9100, "memory": 1088 * 1024**2, GPU: 2}


def test_pod_budget_overrides_only_supported_resources_before_overhead():
    workload = pod(cpu="2", memory="1Gi", **{GPU: "1"})
    workload["spec"]["resources"] = {"requests": {"cpu": "8", "memory": "2Gi"}}
    workload["spec"]["overhead"] = {"cpu": "200m"}
    assert pod_requests(workload) == {"cpu": 8200, "memory": 2 * 1024**3, GPU: 1}


def test_compute_status_does_not_need_to_repeat_extended_resource_requests():
    workload = pod(cpu="1", memory="1Gi", **{GPU: "1"})
    workload["status"]["containerStatuses"] = [
        {
            "name": "main",
            "allocatedResources": {"cpu": "1", "memory": "1Gi"},
            "resources": {"requests": {"cpu": "1", "memory": "1Gi"}},
        }
    ]
    assert snapshot([node()], [workload], None).nodes[0].remaining(GPU) == 7


def test_invalid_node_quantities_do_not_become_zero_inventory():
    with pytest.raises(LabError, match="allocatable quantities"):
        snapshot([node(cpu="invalid")], [], None)


def test_all_assigned_nonterminal_pods_count_even_pending_or_terminating():
    running = pod(cpu="1", **{GPU: "1"})
    pending = pod("assigned", cpu="2", **{GPU: "2"})
    pending["status"]["phase"] = "Pending"
    terminating = pod("terminating", cpu="3", **{GPU: "3"})
    terminating["metadata"]["deletionTimestamp"] = "2000-01-01T00:00:00Z"
    ignored = []
    for state in ("Succeeded", "Failed"):
        item = pod(state, **{GPU: "8"})
        item["status"]["phase"] = state
        ignored.append(item)
    queued = pod("unassigned", **{GPU: "8"})
    del queued["spec"]["nodeName"]
    result = snapshot(
        [node()], [running, deepcopy(running), pending, terminating, queued, *ignored], None
    )
    assert result.nodes[0].reserved[GPU] == 6
    assert result.nodes[0].remaining(GPU) == 2
    assert result.nodes[0].remaining("cpu") == 26000
    assert result.resources == [GPU]


@pytest.mark.parametrize("case", ["resize", "allocation", "dra", "bad-quantity", "overallocated"])
def test_ambiguous_accounting_keeps_a_capacity_ceiling(case):
    workload = pod(cpu="1", **{GPU: "1"})
    if case == "resize":
        workload["status"]["conditions"] = [{"type": "PodResizePending", "status": "True"}]
    elif case == "allocation":
        workload["status"]["containerStatuses"] = [
            {"name": "main", "allocatedResources": {"cpu": "2", GPU: "1"}}
        ]
    elif case == "dra":
        workload["spec"]["resourceClaims"] = [
            {"name": "accelerator", "resourceClaimName": "device"}
        ]
    elif case == "bad-quantity":
        workload["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "invalid"
    else:
        workload["spec"]["containers"][0]["resources"]["requests"][GPU] = "99"
    result = snapshot([node()], [workload], None)
    assert result.nodes[0].issue
    assert result.nodes[0].remaining(GPU) == 8
    assert result.nodes[0].remaining("cpu") == 32000
    assert result.partial


def test_no_pod_access_differs_from_empty_namespace_and_preserves_node_state():
    source = node()
    source["spec"] = {
        "unschedulable": True,
        "taints": [{"key": "dedicated", "value": "training", "effect": "NoSchedule"}],
    }
    missing = snapshot([source], None, ())
    empty = snapshot([source], [], ("research",))
    assert missing.nodes[0].remaining(GPU) == 8
    assert missing.nodes[0].issue
    assert empty.nodes[0].remaining(GPU) == 8
    assert empty.nodes[0].state == "Cordoned"
    assert empty.nodes[0].taints == ("dedicated=training:NoSchedule",)
    assert empty.partial and empty.scope == "research"


async def test_cluster_listing_is_authoritative_and_does_not_need_a_profile():
    query = AsyncMock(side_effect=[[node()], [pod(**{GPU: "3"})]])
    result = await load_resources(SimpleNamespace(collection=query), ("research",))
    assert result.namespaces is None
    assert result.nodes[0].remaining(GPU) == 5
    assert [call.args[0] for call in query.call_args_list] == ["/api/v1/nodes", "/api/v1/pods"]


async def test_rbac_fallback_uses_known_names_when_discovery_is_denied():
    responses = {
        "/api/v1/nodes": [node()],
        "/api/v1/pods": ApiError(403, "GET"),
        "/api/v1/namespaces": ApiError(403, "GET"),
        "/api/v1/namespaces/research/pods": [pod(**{GPU: "2"})],
        "/api/v1/namespaces/other/pods": ApiError(403, "GET"),
    }

    async def collection(path, **params):
        value = responses[path]
        if isinstance(value, Exception):
            raise value
        return value

    query = AsyncMock(side_effect=collection)
    result = await load_resources(
        SimpleNamespace(collection=query), ("research", "research", "other")
    )
    assert result.namespaces == ("research",)
    assert result.nodes[0].remaining(GPU) == 6
    assert any("other" in issue for issue in result.issues)
    assert any("discovery denied" in issue for issue in result.issues)
    assert {call.args[0] for call in query.call_args_list[-2:]} == {
        "/api/v1/namespaces/research/pods",
        "/api/v1/namespaces/other/pods",
    }


async def test_denial_without_namespaces_keeps_capacity_ceiling_and_validates_hints():
    query = AsyncMock(side_effect=[[node()], ApiError(403, "GET"), ApiError(403, "GET")])
    result = await load_resources(SimpleNamespace(collection=query))
    assert result.nodes[0].remaining(GPU) == 8
    assert result.scope == "No pod data"
    query.reset_mock()
    with pytest.raises(LabError):
        await load_resources(SimpleNamespace(collection=query), ("bad/name",))
    query.assert_not_called()


async def test_transport_errors_do_not_become_empty_snapshots():
    query = AsyncMock(side_effect=[[node()], LabError("Disconnected", 5)])
    with pytest.raises(LabError, match="Disconnected"):
        await load_resources(SimpleNamespace(collection=query))


async def test_every_discovered_namespace_contributes_despite_individual_failures():
    seen = []

    async def collection(path, **params):
        seen.append(path)
        if path == "/api/v1/nodes":
            return [node()]
        if path == "/api/v1/pods":
            raise ApiError(403, "GET")
        if path == "/api/v1/namespaces":
            return [
                {"metadata": {"name": name}}
                for name in ("alpha", "beta", "hidden", "gone", "error")
            ]
        name = path.split("/")[-2]
        if name in {"hidden", "gone", "error"}:
            raise ApiError({"hidden": 403, "gone": 404, "error": 500}[name], "GET")
        return [pod(name, **{GPU: "2"})]

    result = await load_resources(SimpleNamespace(collection=collection), ("extra", "alpha"))
    assert set(result.namespaces) == {"alpha", "beta", "extra"}
    assert result.nodes[0].remaining(GPU) == 2
    assert result.nodes[0].reserved[GPU] == 6
    assert len(result.issues) == 3 and result.partial
    assert seen.count("/api/v1/namespaces/alpha/pods") == 1


def test_unaccountable_pod_does_not_discard_other_observed_reservations():
    unknown = pod("resizing", **{GPU: "3"})
    unknown["status"]["conditions"] = [{"type": "PodResizePending", "status": "True"}]
    result = snapshot([node()], [pod(**{GPU: "2"}), unknown], None)
    assert result.nodes[0].remaining(GPU) == 6
    assert result.partial


def test_snapshot_retains_only_resource_data_and_model_labels():
    sensitive = "synthetic-private-pod-value-must-not-be-retained"
    source = node()
    source["metadata"]["labels"]["example.invalid/private"] = sensitive
    source["metadata"]["annotations"] = {"example.invalid/private": sensitive}
    source["spec"]["providerID"] = sensitive
    workload = pod(**{GPU: "2"})
    workload["metadata"]["annotations"] = {"example.invalid/private": sensitive}
    workload["spec"]["containers"][0].update(
        {
            "env": [{"name": "PRIVATE_VALUE", "value": sensitive}],
            "command": ["application", sensitive],
        }
    )
    result = snapshot([source], [workload], ("research",))
    assert sensitive not in repr(result)
    assert result.nodes[0].labels == {GPU + ".product": "Example"}
    assert result.nodes[0].model(GPU) == "Example"
    assert result.nodes[0].remaining(GPU) == 6


def test_model_label_minimization_preserves_nvidia_partition_model_lookup():
    source = node(**{"nvidia.com/mig-example": "7"})
    source["metadata"]["labels"]["nvidia.com/gpu.product"] = "Example Device"
    result = snapshot([source], [], None)
    assert result.nodes[0].model("nvidia.com/mig-example") == "Example Device"


async def test_namespace_reads_are_bounded_and_all_results_are_included():
    active, peak = 0, 0

    async def collection(path, **params):
        nonlocal active, peak
        if path == "/api/v1/nodes":
            return [node()]
        if path == "/api/v1/pods":
            raise ApiError(403, "GET")
        if path == "/api/v1/namespaces":
            return [{"metadata": {"name": f"team-{i}"}} for i in range(40)]
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.001)
            return []
        finally:
            active -= 1

    result = await load_resources(SimpleNamespace(collection=collection))
    assert len(result.namespaces) == 40
    assert 1 < peak <= 8 and active == 0


async def test_cancelling_discovery_joins_namespace_reads():
    entered = asyncio.Event()
    active = 0

    async def collection(path, **params):
        nonlocal active
        if path == "/api/v1/nodes":
            return [node()]
        if path == "/api/v1/pods":
            raise ApiError(403, "GET")
        if path == "/api/v1/namespaces":
            return [{"metadata": {"name": f"team-{i}"}} for i in range(40)]
        active += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    task = asyncio.create_task(load_resources(SimpleNamespace(collection=collection)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0


@pytest.mark.parametrize("cluster_wide", [True, False])
async def test_pod_queries_filter_history_and_unassigned_pods_on_the_server(
    api_server, cluster_wide
):
    queries = []

    async def response(request):
        assert request.method == "GET"
        if request.path == "/api/v1/nodes":
            return web.json_response({"items": [node()]})
        if request.path == "/api/v1/namespaces":
            return web.json_response({"items": [{"metadata": {"name": "research"}}]})
        assert request.query["fieldSelector"] == (
            "status.phase!=Succeeded,status.phase!=Failed,spec.nodeName!="
        )
        queries.append(request.path)
        if request.path == "/api/v1/pods" and not cluster_wide:
            return web.Response(status=403)
        assigned = pod("pending", **{GPU: "2"})
        assigned["status"]["phase"] = "Pending"
        terminating = pod("terminating", **{GPU: "1"})
        terminating["metadata"]["deletionTimestamp"] = "2000-01-01T00:00:00Z"
        return web.json_response({"items": [pod(**{GPU: "3"}), assigned, terminating]})

    result = await load_resources(await api_server(response))
    assert result.nodes[0].reserved[GPU] == 6
    assert result.nodes[0].remaining(GPU) == 2
    assert queries == (
        ["/api/v1/pods"] if cluster_wide else ["/api/v1/pods", "/api/v1/namespaces/research/pods"]
    )
