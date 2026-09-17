"""Static status tables keep every row selectable and their columns aligned."""

import asyncio

import pytest
from test_status_list import screen_text
from test_ui_screens import ready, terminal

from lab.errors import LabError


@pytest.mark.parametrize("width", [28, 32, 100])
@pytest.mark.parametrize(
    "name", ["b", "profile-with-a-very-long-name-that-exceeds-the-terminal-width" * 2]
)
@pytest.mark.parametrize("mouse", [False, True])
async def test_profile_table_alignment_and_unavailable_selection(width, name, mouse):
    async with terminal(width=width) as (screens, keys, _):
        task = asyncio.create_task(
            screens.table(
                "Clusters",
                "Choose a profile.",
                [("a.yaml", "a", "Available"), ("b.yaml", name, "Unavailable")],
                [("refresh", "Refresh"), ("back", "Back")],
                name_label="Profile",
            )
        )
        try:
            await ready(screens)
            ui = screens.page
            text = screen_text(ui)
            assert "Profile" in text and "Status" in text
            columns = [
                line.index(status)
                for line in text.splitlines()
                for status in ("Available", "Unavailable")
                if status in line
            ]
            assert len(columns) == 2 and columns[0] == columns[1]
            screen = ui.app.renderer._last_screen
            assert all(
                x < width
                for row in screen.data_buffer.values()
                for x, cell in row.items()
                if cell.char.strip()
            )
            button = next(button for button in ui.buttons if button.label == name)
            if mouse:
                position = screen.visible_windows_to_write_positions[button.window]
                x = position.xpos + position.width + 4
                y = position.ypos + ui.app.renderer.rows_above_layout
                keys.send_text(f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m")
            else:
                keys.send_text("\x1b[B")
                await asyncio.sleep(0.04)
                assert ui.app.layout.current_control is button.control
                assert "class:focused" in button.control.text()[0][0]
                keys.send_text("\r")
            assert await asyncio.wait_for(task, 1) == "b.yaml"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("selection", ["refresh", "back", "escape"])
async def test_empty_profile_table_actions_and_escape(selection):
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.table(
                "Clusters",
                "",
                [],
                [("refresh", "Refresh"), ("back", "Back")],
                name_label="Profile",
                empty_message="No profiles found.",
            )
        )
        try:
            await ready(screens)
            assert "No profiles found." in screen_text(screens.page)
            if selection == "escape":
                keys.send_text("\x1b")
                with pytest.raises(LabError) as error:
                    await asyncio.wait_for(task, 2)
                assert error.value.code == 130
            else:
                keys.send_text("\t\r" if selection == "back" else "\r")
                assert await asyncio.wait_for(task, 1) == selection
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
