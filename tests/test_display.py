"""A single terminal renderer spans pages; explicit handoff ends its ownership."""

import asyncio
from unittest.mock import Mock

import pytest
from test_ui_screens import ObservedScreens, ready, terminal

from lab.display import current_display, display_session, release_display
from lab.errors import LabError
from lab.ui import Field


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), 2)


async def test_same_renderer_retains_frame_during_work_and_replaces_it_without_erase():
    async with terminal() as (first, keys, output):
        second = ObservedScreens(output=output)
        answered, proceed = asyncio.Event(), asyncio.Event()
        async with display_session():

            async def workflow():
                value = await first.form("First", [Field("value", "Value")])
                answered.set()
                await proceed.wait()
                other = await second.form("Second", [Field("other", "Other")])
                return value, other

            task = asyncio.create_task(workflow())
            await ready(first)
            view = first.page
            app = view.app
            erase = Mock(wraps=app.renderer.erase)
            app.renderer.erase = erase
            field = view.inputs["value"]
            keys.send_text("kept\r")
            await answered.wait()
            assert app.is_running
            assert view.title == "First"
            assert erase.call_count == 0
            keys.send_text("ignored")
            await asyncio.sleep(0.04)
            assert field.area.text == "kept"
            proceed.set()
            await until(lambda: view.title == "Second")
            assert second.page is view
            assert view.app is app and app.is_running
            assert field.area.text == ""
            assert erase.call_count == 0
            keys.send_text("next\r")
            assert await task == ({"value": "kept"}, {"other": "next"})
            assert app.is_running
            assert erase.call_count == 0
        assert erase.call_count == 1
        assert view.app is None
        assert current_display() is None


async def test_slow_work_and_next_page_share_the_same_live_renderer():
    async with terminal() as (screens, keys, _), display_session():
        pending = asyncio.get_running_loop().create_future()

        async def workflow():
            await screens.work(pending, "Connecting")
            return await screens.details("Connected", "", [], [("done", "Done")])

        task = asyncio.create_task(workflow())
        await ready(screens)
        app = screens.page.app
        assert screens.page.title == "Connecting..."
        erase = Mock(wraps=app.renderer.erase)
        app.renderer.erase = erase
        pending.set_result(None)
        await until(lambda: screens.page.title == "Connected")
        assert screens.page.app is app
        assert erase.call_count == 0
        keys.send_text("\r")
        assert await task == "done"


async def test_terminal_handoff_stops_display_and_new_page_starts_cleanly():
    async with terminal() as (screens, keys, _), display_session():
        first = asyncio.create_task(screens.form("Before shell", [Field("value", "Value")]))
        await ready(screens)
        old = screens.page
        keys.send_text("example\r")
        assert await first == {"value": "example"}
        await release_display()
        assert old.app is None
        assert old.inputs == {}
        second = asyncio.create_task(screens.details("After shell", "", [], [("ok", "Done")]))
        await until(lambda: screens.page is not old and screens.page.app is not None)
        keys.send_text("\r")
        assert await second == "ok"


async def test_parent_cancellation_releases_display_and_preserves_operation_identity():
    async with terminal() as (screens, _, _):
        error = LabError("Unknown outcome", 130, data={"operation_id": "example-op"})

        async def operation():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise error from None

        async def workflow():
            async with display_session():
                await screens.work(operation(), "Creating")

        task = asyncio.create_task(workflow())
        await ready(screens)
        view = screens.page
        task.cancel()
        with pytest.raises(LabError) as caught:
            await task
        assert caught.value is error
        assert view.app is None


async def test_nested_display_context_does_not_close_outer_session():
    async with display_session():
        outer = current_display()
        async with display_session():
            assert current_display() is outer
        assert current_display() is outer
    assert current_display() is None


async def test_old_mouse_targets_and_submit_cannot_affect_the_next_page():
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    async with terminal() as (screens, keys, _), display_session():
        first = asyncio.create_task(screens.form("First", [Field("first", "First")]))
        await ready(screens)
        view = screens.page
        button = next(button for button in view.buttons if button.label == "Continue")
        control = view.inputs["first"].area.control
        keys.send_text("one\r")
        await first
        second = asyncio.create_task(screens.form("Second", [Field("second", "Second")]))
        await until(lambda: view.title == "Second")
        event = MouseEvent(Point(1, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
        with set_app(view.app):
            button.mouse(event)
            button.callback()
            control.mouse_handler(event)
        assert not view.reply.done()
        assert view.inputs["second"].area.text == ""
        keys.send_text("two\r")
        assert await second == {"second": "two"}


async def test_old_secret_reply_is_released_after_transition():
    import gc
    import weakref

    async with terminal() as (screens, keys, _), display_session():
        first = asyncio.create_task(screens.form("Token", [Field("token", "Token", secret=True)]))
        await ready(screens)
        reply = weakref.ref(screens.page.reply)
        keys.send_text("synthetic-secret\t\t\r")
        await first
        del first
        second = asyncio.create_task(screens.menu("Menu", "", [("Actions", [("done", "Done")])]))
        await until(lambda: screens.page.title == "Menu")
        gc.collect()
        assert reply() is None
        assert len(screens.page.app.layout._stack) == 1
        keys.send_text("\r")
        assert await second == "done"


async def test_page_output_override_cannot_redirect_the_managed_session():
    from test_ui_screens import Terminal

    async with terminal() as (screens, keys, output), display_session():
        redirected = Terminal()
        alternate = ObservedScreens(output=redirected)
        task = asyncio.create_task(
            alternate.form("Authorization", [Field("token", "Token", secret=True)])
        )
        await ready(alternate)
        assert alternate.page.app.output is output
        keys.send_text("synthetic\r")
        await task
        next_page = asyncio.create_task(
            screens.details("Copy", "", [("", "SYNTHETIC-CONTENT")], [("ok", "Done")])
        )
        await until(lambda: hasattr(screens, "page") and screens.page.title == "Copy")
        assert screens.page.app.output is output
        assert redirected.text == []
        keys.send_text("\r")
        await next_page


async def test_help_does_not_change_the_retained_frame_while_waiting():
    async with terminal() as (screens, keys, _), display_session():
        first = asyncio.create_task(
            screens.form("First", [Field("value", "Value", help="Example")])
        )
        await ready(screens)
        keys.send_text("\r")
        await first
        view = screens.page
        assert not view.accepting
        keys.send_text("\x1bOP")
        await asyncio.sleep(0.04)
        assert not view._help_open
