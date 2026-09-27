"""Exercise resource navigation, scope and inline display reuse through real terminal input."""

import asyncio
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_display import until
from test_resources import GPU, node, pod
from test_ui_screens import ready, terminal

from lab.display import display_session
from lab.errors import LabError
from lab.resource_view import ResourceView, browse_resources
from lab.resources import snapshot


def example_view():
    nodes = []
    for index in range(20):
        source = node(f"node-{index + 1:03}", cpu="6", memory="12Gi", **{GPU: "3"})
        source["metadata"]["labels"][GPU + ".product"] = "Example Device"
        nodes.append(source)
    return ResourceView(snapshot(nodes, [], ("research",)))


def plain(line):
    return "".join(fragment[1] for fragment in line)


def test_content_widths_preserve_complete_labels_with_three_space_gutters():
    view = example_view()
    widths, gap = view.columns(116)
    assert gap == "   "
    assert widths[0] == len("node-001")
    assert widths[1] == len("Example Device")
    line = plain(view.node_line(view.visible[0], 116, False))
    assert "node-001   Example Device" in line
    assert "…" not in line
    assert len(line) < 100


def test_narrow_layout_reduces_gutters_before_truncating_identifiers():
    view = example_view()
    wide, _ = view.columns(116)
    natural_width = 3 + sum(wide) + 3 * (len(wide) - 1)
    narrow_width = natural_width - (len(wide) - 1)
    narrow, gap = view.columns(narrow_width)
    assert len(gap) == 2
    assert narrow == wide
    assert "…" not in plain(view.node_line(view.visible[0], narrow_width, False))


def test_text_compaction_preserves_node_suffix_and_numeric_cells():
    from prompt_toolkit.utils import get_cwidth

    view = example_view()
    for item in view.snapshot.nodes:
        item.name = "long-identifier-for-layout-tests-" + item.name
        item.labels[GPU + ".product"] = "Example device with a deliberately long display label"
    view.refilter()
    row = plain(view.node_line(view.visible[0], 80, False))
    assert get_cwidth(row) <= 80
    assert "…" in row and "node-001" in row
    for index in view.numeric_columns:
        assert view.node_cells(view.visible[0])[index] in row
    assert row.endswith("Ready")


async def test_measured_columns_align_on_resize_and_stay_stable_across_pages():
    async with terminal(width=160, height=24) as (screens, keys, output):
        view = example_view()
        task = asyncio.create_task(screens.resources(view))
        await ready(screens)
        await asyncio.sleep(0.03)

        def model_column():
            rows = text(screens).splitlines()
            heading = next(row for row in rows if "Reserved" in row and "Model" in row)
            values = [row for row in rows if "Example Device" in row]
            assert values and all(
                row.index("Example Device") == heading.index("Model") for row in values
            )
            return heading.index("Model")

        before = model_column()
        await send(keys, "\x1b[C")
        assert model_column() == before
        output.width = 80
        screens.page.app.invalidate()
        await asyncio.sleep(0.05)
        model_column()
        assert "Example Device" in text(screens)
        await send(keys, "q")
        await task


def cluster(count=512, namespaces=("research",)):
    nodes, pods = [], []
    for index in range(count):
        name = f"node-{index:04}"
        source = node(name)
        source["metadata"]["labels"][GPU + ".product"] = "Model A" if index % 2 else "Model B"
        nodes.append(source)
        workload = pod(name, cpu="1", **{GPU: str(index % 9)})
        workload["spec"]["nodeName"] = name
        pods.append(workload)
    return snapshot(nodes, pods, namespaces)


def text(screens):
    grid = screens.page.app.renderer._last_screen
    return "\n".join(
        "".join(row[x].char for x in sorted(row)) for _, row in sorted(grid.data_buffer.items())
    )


async def send(keys, value):
    keys.send_text(value)
    await asyncio.sleep(0.08)


@pytest.mark.parametrize("width,height", [(80, 24), (116, 40), (160, 48), (240, 32)])
async def test_thousands_of_units_search_pagination_detail_and_resize(width, height):
    async with terminal(width=width, height=height) as (screens, keys, output):
        view = ResourceView(cluster())
        task = asyncio.create_task(screens.resources(view))
        await ready(screens)
        assert "4,096 units" in text(screens)
        assert "≤" in text(screens)
        assert "Esc Back" in text(screens)
        assert len(view.visible) == 512
        lines = text(screens).splitlines()
        headings = next(line for line in lines if "Reserved" in line and "Model" in line)
        first = next(line for line in lines if "Model B" in line and "node-" in line)
        assert headings.index("Model") == first.index("Model B")
        assert len(first.rstrip()) <= 116
        page_size = view.page_size
        await send(keys, "\x1b[C")
        assert view.selected == page_size
        assert f"page 2 of {math.ceil(512 / view.page_size)}" in text(screens)
        await send(keys, "/node-0001\r")
        assert len(view.visible) == 1
        assert screens.page.app.layout.current_control is view
        await send(keys, "\r")
        assert view.detail
        assert "Allocatable" in text(screens)
        await send(keys, "q")
        assert not view.detail
        await send(keys, "cfff")
        assert all(item.remaining(GPU) >= 8 for item in view.visible)
        await send(keys, "c")
        output.width, output.height = 80, 24
        screens.page.app.invalidate()
        await asyncio.sleep(0.05)
        assert "Esc Back" in text(screens)
        await send(keys, "q")
        assert await asyncio.wait_for(task, 1) == "back"


async def test_unreadable_pods_show_capacity_ceiling_without_claiming_zero_reserved():
    async with terminal(width=100) as (screens, keys, _):
        view = ResourceView(snapshot([node()], None, ()))
        task = asyncio.create_task(screens.resources(view))
        await ready(screens)
        result = text(screens)
        assert "reservations unknown" in result
        assert "≤8 remaining" in result
        assert "≤32" in result and "≤128" in result
        assert "????????" not in result
        assert "0 observed reserved" not in result
        await send(keys, "q")
        await task


async def test_empty_search_native_editing_and_namespace_action():
    async with terminal() as (screens, keys, _):
        view = ResourceView(cluster(8))
        task = asyncio.create_task(screens.resources(view))
        await ready(screens)
        await send(keys, "/missing\r")
        assert "No matching nodes" in text(screens)
        await send(keys, "/\x15node-0000\r")
        assert len(view.visible) == 1
        await send(keys, "n")
        assert await task == "namespaces"


async def test_resource_page_reuses_session_display_and_does_not_steal_later_keys():
    async with terminal() as (screens, keys, _), display_session():
        view = ResourceView(cluster(8, None))
        task = asyncio.create_task(screens.resources(view))
        await ready(screens)
        application = screens.page.app
        assert "≤" not in text(screens)
        await send(keys, "q")
        await task
        task = asyncio.create_task(screens.menu("Session", "", [("Session", [("done", "Done")])]))
        await asyncio.sleep(0.05)
        assert screens.page.app is application
        assert screens.page.viewport is None
        assert "Cluster resources" not in text(screens)
        assert not view.enabled()
        await send(keys, "\r")
        assert await task == "done"


async def test_refresh_failure_retains_dated_snapshot_with_explicit_warning():
    async def work(awaitable, title):
        return await awaitable

    shown = []

    async def resources(view):
        shown.append((view.snapshot, view.notice))
        return "refresh" if len(shown) == 1 else "back"

    screens = SimpleNamespace(work=work, resources=resources)
    api = SimpleNamespace(
        collection=AsyncMock(side_effect=[[node()], [], LabError("Disconnected", 5)])
    )
    await browse_resources(api, screens)
    assert shown[1][0] is shown[0][0]
    assert "Previous snapshot" in shown[1][1] and "Disconnected" in shown[1][1]


async def test_cancelled_resource_page_releases_terminal():
    async with terminal() as (screens, _, _):
        task = asyncio.create_task(screens.resources(ResourceView(cluster(8))))
        await ready(screens)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert screens.page.app is None


async def test_initial_browser_automatically_loads_known_namespace_after_denial():
    from lab.kubernetes import ApiError

    async def work(awaitable, title):
        return await awaitable

    async def resources(view):
        assert view.snapshot.nodes[0].remaining(GPU) == 5
        assert view.snapshot.scope == "research"
        return "back"

    api = SimpleNamespace(
        collection=AsyncMock(
            side_effect=[
                [node()],
                ApiError(403, "GET"),
                ApiError(403, "GET"),
                [pod(**{GPU: "3"})],
            ]
        )
    )
    screens = SimpleNamespace(work=work, resources=resources)
    assert await browse_resources(api, screens, ("research",)) == ("research",)


async def test_invalid_namespace_entry_does_not_poison_subsequent_discovery():
    async def work(awaitable, title):
        return await awaitable

    actions = iter(("namespaces", "namespaces", "back"))

    async def resources(view):
        return next(actions)

    screens = SimpleNamespace(
        work=work,
        resources=resources,
        form=AsyncMock(side_effect=[{"namespaces": "bad/name"}, {"namespaces": "additional"}]),
    )
    api = SimpleNamespace(collection=AsyncMock(side_effect=[[node()], [], [node()], []]))
    assert await browse_resources(api, screens, ("research",)) == ("research", "additional")
    assert api.collection.await_count == 4


async def test_namespace_form_slow_read_returns_to_resource_browser_in_managed_display():
    from lab.kubernetes import ApiError

    released = asyncio.Event()

    async def collection(path, **params):
        if path == "/api/v1/nodes":
            return [node()]
        if path in {"/api/v1/pods", "/api/v1/namespaces"}:
            raise ApiError(403, "GET")
        assert path == "/api/v1/namespaces/research/pods"
        await released.wait()
        return [pod(**{GPU: "3"})]

    async with terminal(width=160, height=40) as (screens, keys, _), display_session():
        task = asyncio.create_task(
            browse_resources(SimpleNamespace(collection=collection), screens)
        )
        try:
            await ready(screens)
            await until(lambda: screens.page.viewport is not None)
            await send(keys, "n")
            await until(lambda: screens.page.title == "Additional namespaces")
            await send(keys, "research\r")
            await until(lambda: screens.page.title == "Reading cluster resources...")
            released.set()
            await until(lambda: screens.page.viewport is not None)
            await until(lambda: "≤5 remaining" in text(screens))
            assert "≤5 remaining" in text(screens)
            await send(keys, "q")
            assert await asyncio.wait_for(task, 1) == ("research",)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
