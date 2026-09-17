"""Arrow movement follows responsive layout while fields keep their editing keys."""

import asyncio
from contextlib import asynccontextmanager

from test_ui_screens import ready, terminal

from lab.ui import Field


@asynccontextmanager
async def page(kind, *args, **kwargs):
    async with terminal(width=100, height=40) as (screens, keys, output):
        task = asyncio.create_task(getattr(screens, kind)(*args, **kwargs))
        try:
            await ready(screens)
            yield screens.page, keys, output
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def press(keys, sequence):
    keys.send_text(sequence)
    await asyncio.sleep(0.04)


def focus(ui, label):
    button = next(button for button in ui.buttons if button.label == label)
    return ui.app.layout.current_control is button.control


async def test_horizontal_actions_use_left_right_without_arrow_wrap():
    async with page("details", "Welcome", "", [], [("start", "Start setup"), ("exit", "Exit")]) as (
        ui,
        keys,
        _,
    ):
        assert focus(ui, "Start setup")
        await press(keys, "\x1b[B")
        assert focus(ui, "Start setup")
        await press(keys, "\x1b[C")
        assert focus(ui, "Exit")
        await press(keys, "\x1b[C")
        assert focus(ui, "Exit")
        await press(keys, "\x1b[A")
        assert focus(ui, "Exit")
        await press(keys, "\x1b[D")
        assert focus(ui, "Start setup")
        assert "←→ select" in ui._hint()
        await press(keys, "\t\t")
        assert focus(ui, "Start setup")


async def test_resizing_to_stacked_buttons_updates_arrows_and_hint():
    async with page("details", "Welcome", "", [], [("start", "Start setup"), ("exit", "Exit")]) as (
        ui,
        keys,
        output,
    ):
        output.width = 24
        ui.app.invalidate()
        await asyncio.sleep(0.04)
        await press(keys, "\x1b[C")
        assert focus(ui, "Start setup")
        await press(keys, "\x1b[B")
        assert focus(ui, "Exit")
        assert "↑↓ select" in ui._hint()
        await press(keys, "\x1b[A")
        assert focus(ui, "Start setup")
        output.width = 100
        ui.app.invalidate()
        await asyncio.sleep(0.04)
        await press(keys, "\x1b[C")
        assert focus(ui, "Exit")


async def test_menu_arrows_stop_at_edges_while_tab_keeps_linear_order():
    async with page("menu", "Menu", "", [("Actions", [("a", "Browse"), ("b", "Create")])]) as (
        ui,
        keys,
        _,
    ):
        await press(keys, "\x1b[C\x1b[A")
        assert focus(ui, "Browse")
        await press(keys, "\x1b[B")
        assert focus(ui, "Create")
        await press(keys, "\x1b[B\x1b[D")
        assert focus(ui, "Create")
        await press(keys, "\t")
        assert focus(ui, "Browse")


async def test_form_editing_footer_and_help_follow_distinct_navigation_rules():
    async with page(
        "form",
        "Resources",
        [
            Field("image", "Image", value="abc", candidates=["previous"]),
            Field("gpu", "GPUs", value="2", numeric=True),
        ],
        help_text="Resource help.",
    ) as (ui, keys, _):
        image = ui.inputs["image"].area
        await press(keys, "\x1b[DX")
        assert image.text == "abXc"
        await press(keys, "\x1b[A")
        assert image.text == "previous"
        assert ui.app.layout.current_control is image.control
        await press(keys, "\t\x1b[C")
        assert ui.inputs["gpu"].area.text == "3"
        assert "←→ adjust" in ui._hint()
        await press(keys, "\t")
        assert focus(ui, "Back")
        await press(keys, "\x1b[C")
        assert focus(ui, "Continue")
        await press(keys, "\x1b[A")
        assert ui.app.layout.current_control is ui.inputs["gpu"].area.control
        await press(keys, "\t\x1b[B")
        assert focus(ui, "Help")
        await press(keys, "\x1b[A")
        assert focus(ui, "Back")


async def test_filter_arrows_visit_results_and_return_to_filter():
    async with page("choose", "Item", [("a", "Alpha"), ("b", "Beta"), ("g", "Gamma")]) as (
        ui,
        keys,
        _,
    ):
        await press(keys, "a\x1b[B")
        assert focus(ui, "Alpha")
        await press(keys, "\x1b[B")
        assert focus(ui, "Beta")
        await press(keys, "\x1b[C")
        assert focus(ui, "Beta")
        await press(keys, "\x1b[A\x1b[A")
        assert ui.app.layout.current_control is ui._search.control
        await press(keys, "\x1b[A")
        assert ui.app.layout.current_control is ui._search.control


async def test_disabled_actions_skip_navigation_and_ignore_mouse_and_keyboard():
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    async with page(
        "details",
        "Stopped",
        "",
        [],
        [("start", "Start"), ("shell", "Shell"), ("refresh", "Refresh")],
        disabled={"shell": "Requires a Ready instance"},
    ) as (ui, keys, _):
        shell = next(button for button in ui.buttons if button.label == "Shell")
        assert not shell.enabled() and not shell.control.is_focusable()
        assert shell.control not in ui.controls
        assert "Requires a Ready instance" in shell.display_label
        assert focus(ui, "Start")
        for kind in (MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP):
            shell.mouse(MouseEvent(Point(0, 0), kind, MouseButton.LEFT, frozenset()))
        for key in (Keys.ControlM, " "):
            bindings = shell.control.key_bindings.get_bindings_for_keys((key,))
            assert bindings
            for binding in bindings:
                binding.handler(None)
        assert not ui.app.future.done()
        assert focus(ui, "Start")
        await press(keys, "\t")
        assert focus(ui, "Refresh")
        await press(keys, "\x1b[D")
        assert focus(ui, "Start")
        assert not ui.app.future.done()
        with set_app(ui.app):
            start = next(button for button in ui.buttons if button.label == "Start")
            assert start.fragments()[0][0] == "class:focused"
            ui.accepting = False
            assert start.fragments()[0][0] == "class:focused"
