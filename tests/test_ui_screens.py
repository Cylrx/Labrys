"""Exercise live page lifetimes, terminal input, and cancellation without external services."""

import asyncio
from contextlib import asynccontextmanager, suppress

import pytest
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from lab.errors import LabError
from lab.ui import Field, Screens


class Terminal(DummyOutput):
    def __init__(self, width=100, height=40):
        self.width, self.height = width, height
        self.text = []
        self.mouse_enabled = False

    def get_size(self):
        return Size(rows=self.height, columns=self.width)

    def get_rows_below_cursor_position(self):
        return self.height

    def write(self, value):
        self.text.append(value)

    def enable_mouse_support(self):
        self.mouse_enabled = True

    def disable_mouse_support(self):
        self.mouse_enabled = False

    def enter_alternate_screen(self):
        raise AssertionError("Pages must stay inline")


class ObservedScreens(Screens):
    def _page(self):
        self.page = super()._page()
        return self.page


@asynccontextmanager
async def terminal(*, width=100, height=40):
    output = Terminal(width, height)
    with create_pipe_input() as keys, create_app_session(input=keys, output=output):
        screens = ObservedScreens(output=output)
        yield screens, keys, output
        assert not output.mouse_enabled


async def ready(screens):
    async def wait():
        while not (
            hasattr(screens, "page")
            and screens.page.app
            and screens.page.app.is_running
            and screens.page.app.renderer._last_screen
        ):
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), 1)


async def click(screens, keys, label):
    ui = screens.page
    button = next(button for button in ui.buttons if button.label == label)
    ui.app.layout.focus(button.control)
    ui.app.invalidate()
    await asyncio.sleep(0.03)
    position = ui.app.renderer._last_screen.visible_windows_to_write_positions[button.window]
    x, y = position.xpos + 3, position.ypos + ui.app.renderer.rows_above_layout
    keys.send_text(f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m")


async def test_menu_real_mouse_click_closes_page_before_return():
    async with terminal(width=180) as (screens, keys, output):
        task = asyncio.create_task(
            screens.menu(
                "Session",
                "",
                [("Notebooks", [("list", "Browse"), ("new", "Create"), ("retry", "Retry")])],
            )
        )
        await ready(screens)
        screen = screens.page.app.renderer._last_screen
        for row in screen.data_buffer.values():
            assert max((x for x, cell in row.items() if cell.char.strip()), default=0) < 78
        await click(screens, keys, "Create")
        assert await asyncio.wait_for(task, 1) == "new"
        assert screens.page.app is None
        assert not output.mouse_enabled


async def test_typed_menu_command_returns_without_echo():
    async with terminal() as (screens, keys, output):
        task = asyncio.create_task(screens.menu("Session", "", [], command=True))
        await ready(screens)
        keys.send_text("list --namespace research\r")
        assert await asyncio.wait_for(task, 1) == "command:list --namespace research"
        assert screens.page.command is None


async def test_ghost_acceptance_numeric_controls_and_secret_mask():
    secret = "fake-sensitive-text"
    field = Field("token", "Token", candidates=["never-suggest-this"], secret=True)
    assert secret not in repr(Field("token", "Token", value=secret, secret=True))
    async with terminal() as (screens, keys, output):
        task = asyncio.create_task(
            screens.form(
                "Fields",
                [
                    Field("image", "Image", candidates=["previous:v1"], group="Workload"),
                    Field("gpus", "GPUs", value="2", numeric=True),
                    field,
                ],
            )
        )
        await ready(screens)
        image = screens.page.inputs["image"]
        token = screens.page.inputs["token"]
        assert image.area.text == ""
        assert image.area.buffer.suggestion.text == "previous:v1"
        assert token.candidates.values == []
        keys.send_text("\t\t\x1b[C8\t" + secret)
        await asyncio.sleep(0.04)
        assert screens.page.inputs["gpus"].area.text == "8"
        assert token.area.text == secret
        screen = screens.page.app.renderer._last_screen
        rendered = "\n".join(
            "".join(cell.char for cell in row.values()) for row in screen.data_buffer.values()
        )
        assert secret not in rendered and "*" * len(secret) in rendered
        keys.send_text("\t\t\r")
        assert await asyncio.wait_for(task, 1) == {
            "image": "previous:v1",
            "gpus": "8",
            "token": secret,
        }
        assert token.area.text == ""
        assert token.area.buffer.history.get_strings() == []
        assert secret not in "".join(output.text)


@pytest.mark.parametrize("key", ["\x03", "\x1b"])
async def test_long_literal_readonly_page_cancels(key):
    content = "key: [literal]\n  value:  exact spacing\n" * 70
    async with terminal(height=20) as (screens, keys, _):
        task = asyncio.create_task(screens.details("Read only", "", [("", content)], []))
        await ready(screens)
        screen = screens.page.app.renderer._last_screen
        rendered = "\n".join(
            "".join(row[x].char for x in range(78)) for row in screen.data_buffer.values()
        )
        assert "key: [literal]" in rendered
        assert "  value:  exact spacing" in rendered
        keys.send_text("\x1b[6~" + key)
        with pytest.raises(LabError) as result:
            await asyncio.wait_for(task, 2)
        assert result.value.code == 130


@pytest.mark.parametrize("key, expected", [("\r", False), ("\t\r", True)])
async def test_confirm_defaults_to_cancel(key, expected):
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(screens.confirm("Delete?", "Review", [("Name", "sample")]))
        await ready(screens)
        keys.send_text(key)
        assert await asyncio.wait_for(task, 1) is expected


async def test_work_completes_and_closes_progress():
    async with terminal() as (screens, _, _):
        result = await screens.work(asyncio.sleep(0, result={"uid": "fake-uid"}), "Loading")
        assert result == {"uid": "fake-uid"}
        assert not hasattr(screens, "page")


async def test_work_preserves_operation_error():
    error = LabError("Synthetic failure.", data={"uid": "fake-uid"})

    async def fail():
        await asyncio.sleep(0)
        raise error

    async with terminal() as (screens, _, _):
        with pytest.raises(LabError) as result:
            await screens.work(fail(), "Loading")
        assert result.value is error
        assert not hasattr(screens, "page")


@pytest.mark.parametrize("parent_cancel", [False, True])
@pytest.mark.parametrize("backend_receipt", [False, True])
async def test_work_cancellation_joins_backend_and_preserves_receipt(
    parent_cancel, backend_receipt
):
    stopped = asyncio.Event()
    error = LabError("Creation interrupted.", 130, data={"uid": "known-remote-uid"})

    async def operation():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            if backend_receipt:
                raise error from None
            raise
        finally:
            stopped.set()

    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(screens.work(operation(), "Creating"))
        await ready(screens)
        if parent_cancel:
            task.cancel()
        else:
            keys.send_text("\x03")
        expected = asyncio.CancelledError if parent_cancel and not backend_receipt else LabError
        with pytest.raises(expected) as result:
            await asyncio.wait_for(task, 1)
        if backend_receipt:
            assert result.value is error
        elif not parent_cancel:
            assert isinstance(result.value, LabError)
            assert result.value.code == 130
        assert stopped.is_set()
        assert screens.page.app is None


@pytest.mark.parametrize("failure", [False, True])
async def test_work_completion_wins_simultaneous_page_cancel(failure):
    async with terminal() as (screens, _, _):
        completed = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(screens.work(completed, "Loading"))
        await ready(screens)
        screens.page.app.exit(result=None)
        if failure:
            error = LabError("Unknown result.", 130, data={"uid": "known-uid"})
            completed.set_exception(error)
            with pytest.raises(LabError) as result:
                await asyncio.wait_for(task, 1)
            assert result.value is error
        else:
            completed.set_result("accepted")
            assert await asyncio.wait_for(task, 1) == "accepted"


async def test_help_escape_closes_help_before_cancelling_page():
    async with terminal(width=48) as (screens, keys, _):
        task = asyncio.create_task(
            screens.details("Help", "", [], [("ok", "Continue")], help_text="Additional detail.")
        )
        await ready(screens)
        await click(screens, keys, "Help")
        await asyncio.sleep(0.04)
        assert screens.page._help_open
        keys.send_text("\x1b")
        await asyncio.sleep(0.6)
        assert not screens.page._help_open
        assert not task.done()
        keys.send_text("\x03")
        with suppress(LabError):
            await asyncio.wait_for(task, 1)


async def test_secret_field_enter_submits_and_clears_input():
    async with terminal() as (screens, keys, output):
        task = asyncio.create_task(
            screens.form("Sign in", [Field("token", "Token", secret=True)], cancel_label="Cancel")
        )
        await ready(screens)
        token = screens.page.inputs["token"]
        assert [button.label for button in screens.page.buttons] == ["Cancel", "Continue", "Help"]
        keys.send_text("synthetic-token\r")
        assert await asyncio.wait_for(task, 1) == {"token": "synthetic-token"}
        assert token.area.text == ""
        assert "synthetic-token" not in "".join(output.text)


async def test_enter_advances_until_last_field_then_submits():
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.form("Configure", [Field("image", "Image"), Field("name", "Name")])
        )
        await ready(screens)
        keys.send_text("image:v1\r")
        await asyncio.sleep(0.03)
        assert not task.done()
        assert screens.page.app.layout.has_focus(screens.page.inputs["name"].area)
        keys.send_text("sample\r")
        assert await asyncio.wait_for(task, 1) == {"image": "image:v1", "name": "sample"}


async def test_choice_filter_uses_local_labels_and_returns_original_identifier():
    choices = [(f"id-{index}", f"Research item {index}") for index in range(100)]
    async with terminal() as (screens, keys, output):
        task = asyncio.create_task(screens.choose("Select item", choices))
        await ready(screens)
        assert screens.page.command is None
        keys.send_text("ITEM 73")
        await asyncio.sleep(0.04)
        assert [button.label for button in screens.page.buttons] == ["Research item 73"]
        assert "COMMAND" not in "".join(output.text)
        keys.send_text("\r")
        assert await asyncio.wait_for(task, 1) == "id-73"
        assert screens.page._search is None


async def test_choice_filter_recovers_empty_matches_and_supports_arrows():
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.choose("Select item", [("a", "Research A"), ("b", "Research B")])
        )
        await ready(screens)
        keys.send_text("no matches\r")
        await asyncio.sleep(0.04)
        assert not task.done()
        assert screens.page.buttons == []
        keys.send_text("\x15research\x1b[B\x1b[B\r")
        assert await asyncio.wait_for(task, 1) == "b"


async def test_choice_filter_supports_mouse_selection():
    async with terminal() as (screens, keys, _):
        task = asyncio.create_task(
            screens.choose("Select item", [("a", "Research A"), ("b", "Research B")])
        )
        await ready(screens)
        keys.send_text("research")
        await asyncio.sleep(0.04)
        await click(screens, keys, "Research B")
        assert await asyncio.wait_for(task, 1) == "b"


@pytest.mark.parametrize("width", [40, 60, 100, 180])
async def test_authorization_label_is_complete_and_separate_from_cursor(width):
    async with terminal(width=width) as (screens, keys, _):
        task = asyncio.create_task(
            screens.form("Authorize", [Field("token", "Service Account Token", secret=True)])
        )
        await ready(screens)
        ui = screens.page
        screen = ui.app.renderer._last_screen
        lines = {
            y: "".join(row[x].char for x in sorted(row)) for y, row in screen.data_buffer.items()
        }
        label_y, line = next(
            (y, line) for y, line in lines.items() if "Service Account Token" in line
        )
        cursor = screen.get_cursor_position(ui.app.layout.current_window)
        label_end = line.index("Service Account Token") + len("Service Account Token")
        assert cursor.y > label_y or (cursor.y == label_y and cursor.x >= label_end + 2)
        keys.send_text("\r")
        assert await task == {"token": ""}


async def test_form_uses_one_label_width_for_aligned_input_columns():
    async with terminal(width=100) as (screens, keys, _):
        task = asyncio.create_task(
            screens.form("Fields", [Field("a", "Short"), Field("b", "Service Account Token")])
        )
        await ready(screens)
        ui = screens.page
        positions = ui.app.renderer._last_screen.visible_windows_to_write_positions
        columns = [positions[item.area.window].xpos for item in ui.inputs.values()]
        assert columns[0] == columns[1]
        keys.send_text("\r\r")
        assert await task == {"a": "", "b": ""}


@pytest.mark.parametrize("no_color", [False, True])
async def test_filter_background_and_padding_remain_visible_without_focus(monkeypatch, no_color):
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    async with terminal(width=100) as (screens, keys, _):
        task = asyncio.create_task(screens.choose("Items", [("a", "Alpha"), ("b", "Beta")]))
        await ready(screens)
        ui = screens.page

        def background():
            screen = ui.app.renderer._last_screen
            position = screen.visible_windows_to_write_positions[ui._search.window]
            cell = screen.data_buffer[position.ypos][position.xpos]
            padding = screen.data_buffer[position.ypos][position.xpos - 1]
            padding_style = ui.app.style.get_attrs_for_style_str(padding.style)
            assert padding_style.bgcolor or padding_style.reverse
            return ui.app.style.get_attrs_for_style_str(cell.style)

        focused = background()
        assert focused.bgcolor or focused.reverse
        keys.send_text("\x1b[B")
        await asyncio.sleep(0.04)
        inactive = background()
        assert inactive.bgcolor or inactive.reverse
        assert inactive != focused
        keys.send_text("\r")
        assert await task == "a"


async def test_fast_work_does_not_render_a_transient_progress_page():
    async with terminal() as (screens, _, output):
        assert await screens.work(asyncio.sleep(0, result="ready"), "Connecting") == "ready"
        assert output.text == []
        assert not hasattr(screens, "page")


async def test_visible_progress_has_ellipsis_and_remains_cancellable():
    async with terminal() as (screens, keys, _):
        pending = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(screens.work(pending, "Connecting to cluster"))
        await ready(screens)
        assert screens.page.title == "Connecting to cluster..."
        keys.send_text("\x03")
        with pytest.raises(LabError) as caught:
            await task
        assert caught.value.code == 130
        assert pending.cancelled()


async def test_cancellation_before_progress_display_retains_backend_identity():
    started = asyncio.Event()

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise LabError("Outcome unknown", 130, data={"operation_id": "synthetic-id"}) from None

    async with terminal() as (screens, _, output):
        task = asyncio.create_task(screens.work(operation(), "Creating"))
        await started.wait()
        task.cancel()
        with pytest.raises(LabError) as caught:
            await task
        assert caught.value.data == {"operation_id": "synthetic-id"}
        assert output.text == []
