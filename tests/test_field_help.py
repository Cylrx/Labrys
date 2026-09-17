"""Field navigation and contextual help preserve focus, input and secret masking."""

import asyncio

import pytest
from test_document import rendered
from test_ui_screens import click, ready, terminal

from lab.guidance import NOTEBOOK, SETUP
from lab.ui import Field


async def press(keys, sequence):
    keys.send_text(sequence)
    await asyncio.sleep(0.04)


def focused(ui, name):
    return ui.app.layout.current_control is ui.inputs[name].area.control


async def test_empty_history_arrows_navigate_fields_and_shift_tab_preserves_values():
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.form(
                "Notebook",
                [Field("name", "Name"), Field("namespace", "Namespace"), Field("owner", "Owner")],
            )
        )
        await ready(screens)
        ui = screens.page
        await press(keys, "scene\tteam\talex")
        assert focused(ui, "owner")
        await press(keys, "\x1b[A")
        assert focused(ui, "namespace")
        await press(keys, "\x1b[Z")
        assert focused(ui, "name")
        await press(keys, "\x1b[B\x1b[B\r")
        assert await task == {"name": "scene", "namespace": "team", "owner": "alex"}


async def test_history_arrows_keep_their_meaning_while_shift_tab_goes_back():
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.form(
                "Notebook",
                [
                    Field("name", "Name", value="scene"),
                    Field("image", "Image", candidates=["recent:v1", "older:v1"]),
                ],
            )
        )
        await ready(screens)
        ui = screens.page
        await press(keys, "\t\x1b[A\x1b[A")
        assert focused(ui, "image")
        assert ui.inputs["image"].area.text == "older:v1"
        await press(keys, "\x1b[Z")
        assert focused(ui, "name")
        await press(keys, "\t\r")
        assert await task == {"name": "scene", "image": "older:v1"}


@pytest.mark.parametrize("width,height", [(100, 40), (48, 20)])
async def test_help_click_keeps_owner_context_and_tracks_field_changes(width, height):
    fields = [
        Field(name, name.title(), help=NOTEBOOK[name])
        for name in (
            "name",
            "namespace",
            "owner",
            "image",
            "gpu_type",
            "gpus",
            "cpu",
            "memory",
            "node",
            "storage_source",
            "mount_path",
            "workdir",
        )
    ]
    async with terminal(width=width, height=height) as (screens, keys, _):
        task = asyncio.create_task(screens.form("Notebook", fields))
        await ready(screens)
        ui = screens.page
        await press(keys, "\t\talex")
        await click(screens, keys, "Help")
        await asyncio.sleep(0.04)
        assert ui._help_open
        assert focused(ui, "owner")
        assert ui._active_field.definition.name == "owner"
        assert "alex.chen" in rendered(ui)
        assert ui.inputs["owner"].area.text == "alex"
        await press(keys, "\x1b[Z")
        assert focused(ui, "namespace")
        assert "login account" in ui._field_help()
        assert "Kubernetes" in rendered(ui)
        await press(keys, "\x1bOP")
        assert not ui._help_open
        assert focused(ui, "namespace")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_clicking_a_field_label_returns_to_it_without_erasing_input():
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.form("Fields", [Field("a", "First"), Field("b", "Second")])
        )
        await ready(screens)
        ui = screens.page
        await press(keys, "kept\tother")
        position = ui.app.renderer._last_screen.visible_windows_to_write_positions[
            ui.inputs["a"].area.window
        ]
        y = position.ypos + ui.app.renderer.rows_above_layout + 1
        await press(keys, f"\x1b[<0;4;{y}M\x1b[<0;4;{y}m")
        assert focused(ui, "a")
        assert ui.inputs["a"].area.text == "kept"
        await press(keys, "\t\r")
        assert await task == {"a": "kept", "b": "other"}


async def test_secret_field_help_never_echoes_the_entered_token():
    secret = "synthetic-sensitive-token"
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.form(
                "Authorization",
                [Field("token", "Service Account Token", secret=True, help=SETUP["token"])],
            )
        )
        await ready(screens)
        ui = screens.page
        await press(keys, secret + "\x1bOP")
        assert ui._help_open
        assert "restricted" in rendered(ui)
        assert secret not in rendered(ui)
        assert secret not in ui._field_help()
        await press(keys, "\r")
        assert await task == {"token": secret}


async def test_shift_tab_returns_to_fields_in_setup_and_preset_forms():
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.form(
                "Setup",
                [
                    Field("title", "Item title", help=SETUP["title"]),
                    Field("id", "Cluster name", help=SETUP["cluster_name"]),
                ],
            )
        )
        await ready(screens)
        ui = screens.page
        await press(keys, "My cluster\twest\x1b[Z")
        assert focused(ui, "title")
        assert "Shift-Tab previous" in ui._hint()
        await press(keys, "\t\r")
        assert await task == {"title": "My cluster", "id": "west"}


async def test_help_mouse_press_and_release_never_take_input_focus():
    async with terminal(height=40) as (screens, keys, _):
        task = asyncio.create_task(
            screens.form(
                "Notebook", [Field("owner", "Owner", value="alex", help=NOTEBOOK["owner"])]
            )
        )
        await ready(screens)
        ui = screens.page
        for _ in range(4):
            button = next(button for button in ui.buttons if button.label == "Help")
            position = ui.app.renderer._last_screen.visible_windows_to_write_positions[
                button.window
            ]
            x = position.xpos + 4
            y = position.ypos + ui.app.renderer.rows_above_layout + 1
            await press(keys, f"\x1b[<0;{x};{y}M")
            assert focused(ui, "owner")
            await press(keys, f"\x1b[<0;{x};{y}m")
            assert focused(ui, "owner")
            assert ui.inputs["owner"].area.text == "alex"
        keys.send_text("\r")
        assert await task == {"owner": "alex"}


async def test_path_placeholder_is_muted_disappears_on_input_and_returns_when_cleared():
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.form(
                "Local profiles directory",
                [Field("profiles_dir", "Directory path", placeholder="/path/to/profiles")],
            )
        )
        await ready(screens)
        ui = screens.page
        area = ui.inputs["profiles_dir"].area
        assert "/path/to/profiles" in rendered(ui)
        assert area.text == "" and area.buffer.cursor_position == 0
        fragments = area.control.create_content(width=48, height=1).get_line(0)
        assert any(
            "class:muted" in style and "/path/to/profiles" in text for style, text, *_ in fragments
        )
        await press(keys, "/actual/profiles")
        assert "/path/to/profiles" not in rendered(ui)
        assert area.text == "/actual/profiles"
        await press(keys, "\x7f" * len("/actual/profiles"))
        assert "/path/to/profiles" in rendered(ui)
        await press(keys, "\t")
        assert area.text == ""
        await click(screens, keys, "Continue")
        assert await task == {"profiles_dir": ""}


async def test_placeholder_does_not_replace_or_interfere_with_a_real_suggestion():
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.form(
                "Directory",
                [
                    Field(
                        "path",
                        "Path",
                        candidates=["/saved/profiles"],
                        placeholder="/path/to/profiles",
                    )
                ],
            )
        )
        await ready(screens)
        assert "/saved/profiles" in rendered(screens.page)
        assert "/path/to/profiles" not in rendered(screens.page)
        await press(keys, "\t\r")
        assert await task == {"path": "/saved/profiles"}
