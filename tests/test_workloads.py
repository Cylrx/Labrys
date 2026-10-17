"""Workloads group visible pods by controller and show who holds a resource."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_resource_view import send, text
from test_resources import GPU, node, pod
from test_ui_screens import ready, terminal

from lab.errors import LabError
from lab.kubernetes import ApiError
from lab.resource_view import browse_resources
from lab.resources import snapshot
from lab.workload_view import WorkloadView
from lab.workloads import group, load_usage, load_workloads, pod_usage


def member(name, owner, controller=None, node_name="node-a", **resources):
    item = pod(name, cpu="1", memory="1Gi", **resources)
    item["metadata"]["labels"] = {"team.example/owner": owner} if owner else {}
    item["metadata"]["creationTimestamp"] = "2000-01-01T00:00:00+00:00"
    if controller:
        item["metadata"]["ownerReferences"] = [
            {"kind": controller[0], "name": controller[1], "controller": True}
        ]
    if node_name:
        item["spec"]["nodeName"] = node_name
    else:
        del item["spec"]["nodeName"]
        item["status"] = {
            "phase": "Pending",
            "conditions": [
                {"type": "PodScheduled", "status": "False", "message": "Insufficient devices"}
            ],
        }
    return item


def example():
    notebook = member("desk-0", "ada", ("StatefulSet", "desk"), **{GPU: "1"})
    notebook["metadata"]["labels"]["notebook-name"] = "desk"
    pods = [
        member("train-master-0", "ada", ("PyTorchJob", "train"), **{GPU: "4"}),
        member("train-worker-0", "ada", ("PyTorchJob", "train"), "node-b", **{GPU: "4"}),
        notebook,
        member("serve-x1", "", ("ReplicaSet", "serve-5d8"), **{GPU: "2"}),
        member("queued-master-0", "lin", ("PyTorchJob", "queued"), None, **{GPU: "8"}),
        member("cpu-job-x1", "lin", ("Job", "cpu-job")),
    ]
    result = snapshot([node(), node("node-b")], pods, ("research",), owner_key="team.example/owner")
    result.usage = {("research", "train-master-0"): {"cpu": 500, "memory": 1024**3}}
    return result


def test_pods_group_by_controller_with_kubeflow_notebooks_named_as_such():
    items = {item.name: item for item in group(example().pods)}
    assert items["train"].kind == "PyTorchJob" and items["train"].owner == "ada"
    assert items["train"].held(GPU) == 8 and items["train"].nodes() == ["node-a", "node-b"]
    assert items["desk"].kind == "Notebook"
    assert items["serve-5d8"].kind == "ReplicaSet" and items["serve-5d8"].owner == ""
    assert items["queued"].held(GPU) == 0 and items["queued"].waiting(GPU) == 8
    assert items["queued"].state(GPU) == "Waiting for 8"
    assert items["queued"].pods[0].waiting == "Insufficient devices"


def test_unassigned_pods_are_recorded_without_reserving_capacity():
    result = example()
    assert {record.name for record in result.pods if not record.node} == {"queued-master-0"}
    assert result.nodes[0].reserved[GPU] == 7


def test_usage_rounds_nanocores_and_skips_unreadable_samples():
    items = [
        {
            "metadata": {"namespace": "research", "name": "a"},
            "containers": [
                {"usage": {"cpu": "1500000000n", "memory": "1024Ki"}},
                {"usage": {"cpu": "250999u", "memory": "0"}},
            ],
        },
        {"metadata": {"namespace": "research", "name": "b"}, "containers": [{"usage": {}}]},
    ]
    assert pod_usage(items) == {("research", "a"): {"cpu": 1750, "memory": 1024 * 1024}}


async def test_missing_or_slow_metrics_api_is_optional_but_disconnection_is_not():
    api = SimpleNamespace(collection=AsyncMock(side_effect=ApiError(404, "GET")))
    assert await load_usage(api, ("research",)) is None
    assert api.collection.await_args.args[0] == (
        "/apis/metrics.k8s.io/v1beta1/namespaces/research/pods"
    )
    api = SimpleNamespace(collection=AsyncMock(side_effect=LabError("Interrupted", 5)))
    assert await load_usage(api, None) is None
    api = SimpleNamespace(collection=AsyncMock(side_effect=LabError("Connection lost", 4)))
    with pytest.raises(LabError, match="Connection lost"):
        await load_usage(api, None)


async def test_cluster_wide_pods_read_cluster_wide_usage():
    paths = []

    async def collection(path, **params):
        paths.append(path)
        return {"/api/v1/nodes": [node()], "/api/v1/pods": [member("a", "ada", **{GPU: "1"})]}.get(
            path, []
        )

    result = await load_workloads(SimpleNamespace(collection=collection), (), "team.example/owner")
    assert paths == ["/api/v1/nodes", "/api/v1/pods", "/apis/metrics.k8s.io/v1beta1/pods"]
    assert result.usage == {} and result.pods[0].owner == "ada"


async def test_owners_open_workloads_then_pods_and_return_to_the_same_owner():
    async with terminal(width=140, height=40) as (screens, keys, _):
        view = WorkloadView(example(), "team.example/owner", "example-cluster")
        task = asyncio.create_task(screens.resources(view))
        await ready(screens)
        page = text(screens)
        assert "11 of 16 held · 8 waiting" in page
        assert [owner.name for owner in view.visible] == ["ada", "", "lin"]
        assert "? / 3" in page  # one of ada's pods has no usage sample
        await send(keys, "\x1b[B\x1b[B\r")
        assert view.level.parent == "lin" and [item.name for item in view.visible] == ["queued"]
        await send(keys, "\r")
        assert view.level.kind == "pods" and "Insufficient devices" in text(screens)
        await send(keys, "q")
        await send(keys, "f")
        assert {item.name for item in view.visible} == {"queued", "cpu-job"}
        await send(keys, "q")
        assert len(view.levels) == 1 and view.visible[view.selected].name == "lin"
        await send(keys, "\t")
        assert [item.name for item in view.visible][:2] == ["train", "serve-5d8"]
        await send(keys, "q")
        assert await asyncio.wait_for(task, 1) == "back"


async def test_failed_refresh_keeps_the_previous_workloads():
    async def work(awaitable, title):
        return await awaitable

    shown = []

    async def resources(view):
        shown.append((view.snapshot, view.notice))
        return "refresh" if len(shown) == 1 else "back"

    api = SimpleNamespace(
        collection=AsyncMock(side_effect=[[node()], [], [], LabError("Disconnected", 5)])
    )
    await browse_resources(
        api,
        SimpleNamespace(work=work, resources=resources),
        (),
        load_workloads,
        lambda data: WorkloadView(data, "", "example-cluster"),
    )
    assert shown[1][0] is shown[0][0] and "Disconnected" in shown[1][1]


def test_fuzzy_prefers_word_starts_then_substrings_then_letters_in_order():
    from lab.workload_view import fuzzy

    fields = ["train-sweep-1", "rack-a-gpu-node002"]
    assert fuzzy(["sweep"], fields) == 3
    assert fuzzy(["weep"], fields) == 2
    assert fuzzy(["ts1"], fields) == 1
    assert fuzzy(["sweep", "node002"], fields) == 6
    assert fuzzy(["sweep", "node009"], fields) == 0


async def test_heading_clicks_cycle_ascending_descending_and_default():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    async with terminal(width=140, height=40) as (screens, keys, _):
        view = WorkloadView(example(), "team.example/owner", "example-cluster")
        task = asyncio.create_task(screens.resources(view))
        await ready(screens)
        await send(keys, "\t")
        default = [item.name for item in view.visible]

        def click(heading):
            index = [column.heading for column in view.columns("workloads")].index(heading)
            position = Point(view.spans[index][0], 0)
            view.mouse_handler(
                MouseEvent(position, MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
            )
            return [item.name for item in view.visible]

        assert click("Workload") == ["desk", "queued", "serve-5d8", "train"]
        screens.page.app.invalidate()
        await asyncio.sleep(0.05)
        assert "Workload ↑" in text(screens) and "by Workload ↑" in text(screens)
        assert click("Workload") == ["train", "serve-5d8", "queued", "desk"]
        assert click("Workload") == default
        assert click("Owner")[-1] == "serve-5d8"  # no owner sorts last in both directions
        assert click("Owner")[-1] == "serve-5d8"
        await send(keys, "q")
        assert await asyncio.wait_for(task, 1) == "back"


async def test_search_matches_any_column_and_keeps_arrows_on_the_list():
    async with terminal(width=140, height=40) as (screens, keys, _):
        view = WorkloadView(example(), "team.example/owner", "example-cluster")
        task = asyncio.create_task(screens.resources(view))
        await ready(screens)
        await send(keys, "/lin")
        assert [owner.name for owner in view.visible] == ["lin"]
        await send(keys, "\x15\t")  # clear the query, then return to the list
        await send(keys, "\t/node-b")
        assert [item.name for item in view.visible] == ["train"]
        await send(keys, "\x15srv")
        assert [item.name for item in view.visible] == ["serve-5d8"]
        await send(keys, "\r")
        assert view.level.kind == "pods" and "serve-x1" in text(screens)
        assert view.search.text == ""  # each level keeps its own search
        await send(keys, "q")
        assert view.search.text == "srv" and view.visible[view.selected].name == "serve-5d8"
        await send(keys, "q")
        assert await asyncio.wait_for(task, 1) == "back"


def browse(snapshot, owner_key="team.example/owner"):
    return WorkloadView(snapshot, owner_key, "example-cluster")


def test_uninterpretable_requests_stay_visible_as_lower_bounds():
    claimed = member("claimed-0", "kim", ("PyTorchJob", "claimed"))
    claimed["spec"]["resourceClaims"] = [{"name": "gpu", "resourceClaimName": "gpu"}]
    view = browse(
        snapshot(
            [node()],
            [claimed, member("plain-0", "ada", **{GPU: "2"})],
            None,
            owner_key="team.example/owner",
        )
    )
    assert [owner.name for owner in view.visible] == ["ada", "kim"]
    assert view.cells(view.visible[1])[1] == "?"
    summary = "".join(text for _, text in view.summary(120))
    assert "≥2 of 8 held" in summary and "some requests unreadable" in summary


def test_clusters_without_extended_resources_list_every_workload():
    view = browse(snapshot([node()], [member("cpu-0", "ada", ("Job", "cpu"))], None), "")
    view.resource = ""
    view.refilter()
    assert [item.name for item in view.visible] == ["cpu"]


def test_without_an_owner_label_there_is_no_owners_table():
    view = browse(example(), "")
    assert view.level.kind == "workloads"
    view.switch()
    assert view.level.kind == "workloads"
    assert "Tab" not in "".join(text for _, text in view.footer())


def test_owner_scope_follows_the_resource_and_refresh_keeps_the_selection():
    pods = [
        member("gpu-0", "ada", ("Job", "gpu"), **{GPU: "2"}),
        member("rdma-0", "ada", ("Job", "rdma"), **{"rdma.example/hca": "3"}),
    ]
    source = node(**{"rdma.example/hca": "4"})
    view = browse(snapshot([source], pods, None, owner_key="team.example/owner"))
    view.resource = GPU
    view.refilter()
    view.open()
    assert "2 of 8 held" in "".join(text for _, text in view.summary(120))
    view.next_resource()
    assert view.resource == "rdma.example/hca"
    assert "3 of 4 held" in "".join(text for _, text in view.summary(120))
    view.move(len(view.visible))
    chosen = view.current()
    view.update(view.snapshot)
    assert view.current() == chosen


async def test_pod_level_pages_and_ignores_clicks_on_the_page_indicator():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    pods = [
        member(f"wide-{index:02}", "ada", ("PyTorchJob", "wide"), **{GPU: "1"})
        for index in range(30)
    ]
    async with terminal(width=140, height=24) as (screens, keys, _):
        view = browse(snapshot([node(**{GPU: "64"})], pods, None, owner_key="team.example/owner"))
        task = asyncio.create_task(screens.resources(view))
        await ready(screens)
        await send(keys, "\r\r")
        assert view.level.kind == "pods" and len(view.visible) == 30
        assert "page 1 of" in text(screens)
        view.mouse_handler(
            MouseEvent(
                Point(4, view.page_size + 1),
                MouseEventType.MOUSE_DOWN,
                MouseButton.LEFT,
                frozenset(),
            )
        )
        assert view.selected == 0
        await send(keys, "\x1b[C")
        assert view.selected == view.page_size and "page 2 of" in text(screens)
        await send(keys, "q")
        await send(keys, "q")
        await send(keys, "q")
        assert await asyncio.wait_for(task, 1) == "back"
