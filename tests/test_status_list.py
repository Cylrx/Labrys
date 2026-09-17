"""Bulk observations and page-owned loading keep status lists responsive and scoped."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_ui_screens import ready, terminal

from lab.display import display_session
from lab.errors import LabError
from lab.kubernetes import ApiError
from lab.notebooks import Notebooks


def notebook(name, uid, stopped=False):
    return {
        "metadata": {
            "name": name,
            "uid": uid,
            "namespace": "research",
            "annotations": {"kubeflow-resource-stopped": "now"} if stopped else {},
        }
    }


def owned(name, uid, parent, **extra):
    return {"metadata": {"name": name, "uid": uid, "ownerReferences": [{"uid": parent}]}, **extra}


async def test_bulk_status_uses_two_concurrent_collections_and_current_owner_uids():
    notebooks = [notebook("a", "a", True), notebook("b", "b"), notebook("c", "c", True)]
    notebooks.extend(notebook(f"empty-{i}", f"empty-{i}") for i in range(50))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def collection(path, **params):
        calls.append((path, params))
        if len(calls) == 2:
            entered.set()
        await release.wait()
        if path.endswith("/statefulsets"):
            return [
                owned("a", "parent-a", "a"),
                owned("b", "parent-b", "b"),
                owned("c", "old-parent", "old-c"),
                owned("wrong-name", "wrong-parent", "c"),
            ]
        return [
            owned("a-0", "pod-a", "parent-a", status={"phase": "Running"}),
            owned(
                "b-0",
                "pod-b",
                "parent-b",
                status={"conditions": [{"type": "Ready", "status": "True"}]},
            ),
            owned("c-old", "pod-c", "old-parent"),
            owned("wrong", "pod-wrong", "wrong-parent"),
            owned("unrelated", "pod-other", "other"),
        ]

    service = Notebooks(SimpleNamespace(collection=collection), None, None, None)
    task = asyncio.create_task(service.statuses("research", notebooks))
    await asyncio.wait_for(entered.wait(), 1)
    assert not task.done()
    release.set()
    result = await task
    assert len(calls) == 2
    assert ("/api/v1/namespaces/research/pods", {"labelSelector": "notebook-name"}) in calls
    assert result["a"]["state"] == "Stopping"
    assert result["b"]["state"] == "Running"
    assert result["c"]["state"] == "Stopped" and result["c"]["pods"] == []
    assert all(result[f"empty-{i}"]["state"] == "Starting" for i in range(50))


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_bulk_lookup_drains_other_query(cancel):
    entered, drained = asyncio.Event(), asyncio.Event()

    async def collection(path, **params):
        if path.endswith("/statefulsets"):
            await entered.wait()
            if not cancel:
                raise ApiError(403, "GET")
            await asyncio.Event().wait()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    service = Notebooks(SimpleNamespace(collection=collection), None, None, None)
    task = asyncio.create_task(service.statuses("research", [notebook("a", "a", True)]))
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ApiError):
        await task
    assert drained.is_set()


async def test_empty_batch_does_not_query_and_namespace_mismatch_fails_before_io():
    query = AsyncMock()
    service = Notebooks(SimpleNamespace(collection=query), None, None, None)
    assert await service.statuses("research", []) == {}
    with pytest.raises(LabError, match="cross namespaces"):
        await service.statuses("other", [notebook("a", "a")])
    query.assert_not_called()


def screen_text(ui):
    screen = ui.app.renderer._last_screen
    return "\n".join(
        "".join(row[x].char for x in sorted(row)) for _, row in sorted(screen.data_buffer.items())
    )


@pytest.mark.parametrize("width", [32, 100])
@pytest.mark.parametrize("second_name", ["bb", "longer-notebook-name"])
async def test_status_cells_update_without_rebuilding_rows_or_moving_focus(width, second_name):
    release = asyncio.Event()

    async def load():
        await release.wait()
        return {"one": "Stopped", "two": "Running"}

    async with terminal(width=width) as (screens, keys, output):
        task = asyncio.create_task(
            screens.status_list(
                "Notebooks",
                "research",
                [("one", "a"), ("two", second_name)],
                load,
                [("back", "Back")],
            )
        )
        try:
            await ready(screens)
            ui = screens.page
            assert "Name" in screen_text(ui) and "Status" in screen_text(ui)
            assert screen_text(ui).count("Checking…") == 2
            keys.send_text("\x1b[B")
            await asyncio.sleep(0.04)
            focus, body, controls = ui.app.layout.current_control, ui._body, list(ui.controls)
            before = [
                line.index("Checking…")
                for line in screen_text(ui).splitlines()
                if "Checking…" in line
            ]
            release.set()
            await asyncio.sleep(0.04)
            assert "Checking…" not in screen_text(ui)
            assert ui.app.layout.current_control is focus and ui._body is body
            assert ui.controls == controls
            after = [
                line.index(value)
                for line in screen_text(ui).splitlines()
                for value in ("Stopped", "Running")
                if value in line
            ]
            assert before == after
            keys.send_text("\r")
            assert await asyncio.wait_for(task, 1) == "two"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_leaving_list_cancels_lookup_before_the_next_shared_page():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def load():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with terminal() as (screens, keys, _):

        async def journey():
            async with display_session():
                assert (
                    await screens.status_list("Notebooks", "", [("uid", "name")], load, []) == "uid"
                )
                assert cancelled.is_set()
                return await screens.details("Next page", "", [], [("back", "Back")])

        task = asyncio.create_task(journey())
        try:
            await entered.wait()
            await ready(screens)
            keys.send_text("\r")
            for _ in range(100):
                if screens.page.title == "Next page":
                    break
                await asyncio.sleep(0.01)
            assert screens.page.title == "Next page" and cancelled.is_set()
            assert not screens.page.error_message
            keys.send_text("\r")
            assert await asyncio.wait_for(task, 1) == "back"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_lookup_failure_shows_unknown_without_blocking_selection():
    async def load():
        raise ApiError(403, "GET")

    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.status_list("Notebooks", "", [("uid", "example")], load, [("back", "Back")])
        )
        try:
            await ready(screens)
            await asyncio.sleep(0.04)
            assert "Unknown" in screen_text(screens.page)
            assert "access denied" in screens.page.error_message
            keys.send_text("\r")
            assert await asyncio.wait_for(task, 1) == "uid"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_clicking_status_cell_selects_its_row_while_loading():
    async def load():
        await asyncio.Event().wait()

    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.status_list(
                "Notebooks", "", [("a", "Alpha"), ("b", "Beta")], load, [("back", "Back")]
            )
        )
        try:
            await ready(screens)
            ui = screens.page
            button = next(item for item in ui.buttons if item.label == "Beta")
            position = ui.app.renderer._last_screen.visible_windows_to_write_positions[
                button.window
            ]
            x = position.xpos + position.width + 3
            y = position.ypos + ui.app.renderer.rows_above_layout
            keys.send_text(f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m")
            assert await asyncio.wait_for(task, 1) == "b"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
