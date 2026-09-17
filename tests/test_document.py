"""Long text remains navigable and Copy always receives the unmodified source."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_ui_screens import click, ready, terminal

from lab.errors import LabError


def rendered(ui):
    return "\n".join(
        "".join(row[x].char for x in sorted(row))
        for row in ui.app.renderer._last_screen.data_buffer.values()
    )


async def press(keys, sequence):
    keys.send_text(sequence)
    await asyncio.sleep(0.04)


@pytest.mark.parametrize("width,height", [(100, 24), (48, 20), (180, 50)])
async def test_scroll_wrapped_config_to_both_ends_and_copy_exact_source(width, height):
    source = "FIRST\n  certificate: " + "abcdefgh" * 2000 + "\n    token: synthetic-only\nLAST\n"
    async with terminal(width=width, height=height) as (screens, keys, _):
        task = asyncio.create_task(
            screens.details(
                "Configuration",
                "Scroll or copy.",
                [("", source)],
                [("saved", "Saved")],
                copy_text=source,
            )
        )
        await ready(screens)
        ui = screens.page
        copied = AsyncMock()
        ui.copy_text = copied
        document = ui.documents[0]
        assert document.width == width - 5
        assert "FIRST" in rendered(ui)
        await press(keys, "\x1b[6~")
        assert document.offset > 0
        assert "abcdefgh" in rendered(ui)
        await press(keys, "\x1b[F")
        assert "LAST" in rendered(ui)
        await press(keys, "\x1b[H")
        assert document.offset == 0
        assert "FIRST" in rendered(ui)
        position = ui.app.renderer._last_screen.visible_windows_to_write_positions[document.window]
        x, y = position.xpos + 2, position.ypos + ui.app.renderer.rows_above_layout + 1
        await press(keys, f"\x1b[<65;{x};{y}M")
        assert document.offset > 0
        await click(screens, keys, "Copy")
        await asyncio.sleep(0.04)
        copied.assert_awaited_once_with(source)
        assert any(button.label == "Copied" for button in ui.buttons)
        assert not task.done()
        await click(screens, keys, "Saved")
        assert await asyncio.wait_for(task, 1) == "saved"
        assert document.text == "" and document.lines == []


async def test_document_remains_complete_past_the_outer_pane_limit_and_after_resize():
    source = "\n".join(f"line-{index}" for index in range(12000))
    async with terminal(height=24) as (screens, keys, output):
        task = asyncio.create_task(screens.details("Long", "", [("", source)], [("ok", "Done")]))
        await ready(screens)
        await press(keys, "\x1b[F")
        assert "line-11999" in rendered(screens.page)
        output.width = 40
        screens.page.app.invalidate()
        await asyncio.sleep(0.04)
        assert screens.page.documents[0].width == 35
        await press(keys, "\x1b[H")
        assert "line-0" in rendered(screens.page)
        await click(screens, keys, "Done")
        assert await task == "ok"


async def test_copy_error_is_visible_and_document_can_still_be_used():
    source = "first\nlast"
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.details("Copy", "", [("", source)], [("done", "Done")], copy_text=source)
        )
        await ready(screens)
        ui = screens.page
        ui.copy_text = AsyncMock(side_effect=LabError("Clipboard unavailable", 9))
        await click(screens, keys, "Copy")
        await asyncio.sleep(0.04)
        assert "Clipboard unavailable" in ui.error_message
        assert not task.done()
        assert ui.documents[0].text == source
        assert any(button.label == "Copy" for button in ui.buttons)
        await click(screens, keys, "Done")
        assert await task == "done"


async def test_leaving_copy_page_cancels_inflight_copy():
    started, finished = asyncio.Event(), asyncio.Event()

    async def pending(text):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.details("Copy", "", [("", "example")], [("done", "Done")], copy_text="example")
        )
        await ready(screens)
        screens.page.copy_text = pending
        await click(screens, keys, "Copy")
        await asyncio.wait_for(started.wait(), 1)
        await click(screens, keys, "Done")
        assert await task == "done"
        assert finished.is_set()
