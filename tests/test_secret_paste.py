"""Verify masked multiline paste with native terminal input and page lifetimes."""

import asyncio

import pytest
from test_ui_screens import ready, terminal

from lab.display import display_session
from lab.errors import LabError
from lab.ui import UI


async def test_paste_preserves_yaml_and_masks_terminal():
    value = 'apiVersion: v1\nusers:\n  - name: "fake-secret-ä"\n    token: ab\tcd\n'
    async with terminal() as (screens, keys, output):
        task = asyncio.create_task(screens.paste("Import", "Paste a kubeconfig"))
        await ready(screens)
        area = screens.page._paste
        keys.send_text("\x1b[200~" + value + "\x1b[201~")
        await asyncio.sleep(0.06)
        assert area.text == value
        assert not task.done()
        screen = screens.page.app.renderer._last_screen
        rendered = "\n".join(
            "".join(cell.char for cell in row.values()) for row in screen.data_buffer.values()
        )
        assert "fake-secret" not in rendered
        assert "apiVersion" not in rendered
        assert "****" in rendered
        keys.send_text("\t\t\r")
        assert await asyncio.wait_for(task, 1) == value
        assert area.text == ""
        assert area.buffer._undo_stack == []
        assert area.buffer._redo_stack == []
        assert area.buffer.history.get_strings() == []
        assert area.buffer.document_before_paste is None
        assert "fake-secret" not in "".join(output.text)


async def test_enter_and_arrows_edit_text_tab_moves_to_actions():
    async with terminal(width=55, height=25) as (screens, keys, _):
        task = asyncio.create_task(screens.paste("Import", ""))
        await ready(screens)
        area = screens.page._paste
        keys.send_text("one\rtwo\x1b[A")
        await asyncio.sleep(0.05)
        assert area.text == "one\ntwo"
        assert area.buffer.document.cursor_position_row == 0
        assert screens.page.app.layout.has_focus(area)
        keys.send_text("\x1b[B\t")
        await asyncio.sleep(0.05)
        assert area.buffer.document.cursor_position_row == 1
        assert screens.page.app.layout.current_control is screens.page.buttons[0].control
        assert area.text == "one\ntwo"
        keys.send_text("\x1b[Z")
        await asyncio.sleep(0.05)
        assert screens.page.app.layout.has_focus(area)
        keys.send_text("\x1b[Z\r")
        assert await asyncio.wait_for(task, 1) == "one\ntwo"


@pytest.mark.parametrize("cancel", ["\x1b", "\x03", "\t\r"])
async def test_cancel_clears_shared_display_buffer(cancel):
    async with terminal() as (screens, keys, _), display_session():
        task = asyncio.create_task(screens.paste("Import", ""))
        await ready(screens)
        area = screens.page._paste
        keys.send_text("fake-secret")
        await asyncio.sleep(0.04)
        keys.send_text(cancel)
        with pytest.raises(LabError, match="Cancelled"):
            await asyncio.wait_for(task, 2)
        assert area.text == ""
        assert area.buffer._undo_stack == []
        assert screens.page._paste is None
        assert screens.page.app.is_running


async def test_oversize_paste_is_rejected_without_storing_it():
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(screens.paste("Import", "", max_bytes=8))
        await ready(screens)
        area = screens.page._paste
        keys.send_text("\x1b[200~" + "ä" * 5 + "\x1b[201~")
        await asyncio.sleep(0.05)
        assert area.text == ""
        assert "8-byte limit" in screens.page.error_message
        assert not task.done()
        keys.send_text("\t\t\r")
        await asyncio.sleep(0.05)
        assert not task.done()
        keys.send_text("\x1b")
        with pytest.raises(LabError):
            await asyncio.wait_for(task, 2)


def test_replacement_clears_secret_and_stale_submit_cannot_respond():
    ui = UI()
    results = []
    ui.paste("Import", "", results.append, lambda: None)
    area = ui._paste
    area.buffer.insert_text("fake-secret")
    submit = ui.buttons[-1].callback
    ui.menu("Next", "", [])
    submit()
    assert area.text == ""
    assert results == []


async def test_long_paste_scrolls_and_cancelled_task_clears_shared_page():
    async with terminal() as (screens, keys, _), display_session():
        task = asyncio.create_task(screens.paste("Import", ""))
        await ready(screens)
        area = screens.page._paste
        value = "fake-secret\n" * 80
        keys.send_text("\x1b[200~" + value + "\x1b[201~")
        await asyncio.sleep(0.06)
        info = area.window.render_info
        assert info.window_height <= 12
        assert info.window_width > 42
        assert area.window.vertical_scroll > 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert area.text == ""
        assert area.buffer._undo_stack == []
        assert screens.page._paste is None


@pytest.mark.parametrize("kind", ["paste", "field"])
async def test_secret_kill_commands_cannot_leak_into_later_plain_fields(kind):
    from lab.ui import Field

    async with terminal() as (screens, keys, output), display_session():
        if kind == "paste":
            task = asyncio.create_task(screens.paste("Paste", ""))
        else:
            task = asyncio.create_task(
                screens.form("Token", [Field("token", "Token", secret=True)])
            )
        await ready(screens)
        ui = screens.page
        ui._public_clipboard.set_text("public-text")
        sentinel = "synthetic-private-value"
        keys.send_text(sentinel + "\x01\x0b")
        await asyncio.sleep(0.05)
        assert (ui._paste.text if kind == "paste" else ui.inputs["token"].area.text) == ""
        keys.send_text("\x1b")
        with pytest.raises(LabError):
            await asyncio.wait_for(task, 2)
        task = asyncio.create_task(screens.form("Plain", [Field("text", "Text")]))
        await ready(screens)
        keys.send_text("\x19")
        await asyncio.sleep(0.05)
        assert screens.page.inputs["text"].area.text == "public-text"
        assert sentinel not in "".join(output.text)
        keys.send_text("\r")
        assert await asyncio.wait_for(task, 1) == {"text": "public-text"}
