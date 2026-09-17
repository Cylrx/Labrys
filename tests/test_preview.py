"""Run complete simulated journeys through native terminal controls."""

import asyncio
import importlib
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

sys.path.insert(0, str(Path(__file__).parents[1]))
DemoApp = importlib.import_module("scripts.demo.app").DemoApp
Model = importlib.import_module("scripts.demo.model").Model
SAMPLE = importlib.import_module("scripts.demo.model").SAMPLE
UI = importlib.import_module("scripts.demo.ui").UI


class Terminal(DummyOutput):
    def __init__(self, width=100, height=40, top=0):
        self.width, self.height, self.top = width, height, top
        self.mouse_enabled = False
        self.mouse_disabled = False

    def get_size(self):
        return Size(rows=self.height, columns=self.width)

    def get_rows_below_cursor_position(self):
        return self.height - self.top

    def enter_alternate_screen(self):
        raise AssertionError("Simulator must stay inline")

    def enable_mouse_support(self):
        self.mouse_enabled = True

    def disable_mouse_support(self):
        self.mouse_disabled = True


@asynccontextmanager
async def running(*, width=100, height=40, top=0, entry="menu", fresh=False, scenario="ready"):
    clock = [0.0]
    output = Terminal(width, height, top)
    with create_pipe_input() as keys, create_app_session(input=keys, output=output):
        ui, model = UI(), Model(fresh=fresh, scenario=scenario, clock=lambda: clock[0])
        application = DemoApp(ui, model, fresh=fresh, entry=entry)
        task = asyncio.create_task(ui.run())
        await asyncio.sleep(0.04)
        try:
            yield application, ui, model, keys, output, clock
        finally:
            if not task.done():
                keys.send_text("\x03")
            await asyncio.wait_for(task, 1)
            assert output.mouse_disabled


async def press(keys, value):
    keys.send_text(value)
    await asyncio.sleep(0.04)


async def click(ui, keys, label):
    button = next(button for button in ui.buttons if button.label == label)
    ui.app.layout.focus(button.control)
    ui.app.invalidate()
    await asyncio.sleep(0.04)
    position = ui.app.renderer._last_screen.visible_windows_to_write_positions[button.window]
    x, y = position.xpos + 3, position.ypos + ui.app.renderer.rows_above_layout
    await press(keys, f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m")


async def fill(ui, keys, field, value):
    control = ui.inputs[field].area
    ui.app.layout.focus(control)
    control.buffer.cursor_position = len(control.text)
    await press(keys, "\x15" + str(value))


async def test_default_menu_lists_real_operations_and_supports_mouse():
    async with running(top=3) as (app, ui, model, keys, output, _):
        assert ui.title == "Session menu"
        labels = {button.label for button in ui.buttons}
        assert {
            "Browse Notebooks",
            "Create Notebook",
            "Notebook shell",
            "Open in VS Code",
            "Start Notebook",
            "Stop Notebook",
            "Delete Notebook",
            "Retry operation",
            "Save preset",
            "Session status",
            "Reconnect",
            "Disconnect",
        } <= labels
        await click(ui, keys, "Session status")
        assert ui.title == "Session status"
        assert output.mouse_enabled and not ui.app.full_screen
        await click(ui, keys, "Back")
        assert ui.title == "Session menu"


async def test_command_typing_uses_same_state_as_menu():
    async with running() as (app, ui, model, keys, _, _):
        await press(keys, "list --namespace research\r")
        assert ui.title == "Notebooks"
        await press(keys, "status\r")
        assert ui.title == "Session status"
        assert model.list_notebooks("research")


async def test_create_confirm_wait_preset_and_reuse():
    async with running() as (app, ui, model, keys, _, clock):
        app.notebooks.create("research")
        assert len(ui.inputs) == 11
        for name, value in {**SAMPLE, "name": "new-work"}.items():
            await fill(ui, keys, name, "" if value is None else value)
        await click(ui, keys, "Review Notebook")
        assert ui.title == "Create this Notebook?"
        assert model.operations() == []
        await click(ui, keys, "Create Notebook")
        assert ui.title == "Waiting for Notebook"
        clock[0] = 2
        await asyncio.sleep(1.05)
        assert ui.title == "Save this configuration?"
        await click(ui, keys, "Save as a new preset")
        await fill(ui, keys, "name", "Research preset")
        await click(ui, keys, "Save preset")
        assert model.presets("research")[0]["name"] == "Research preset"
        await click(ui, keys, "Create from this preset")
        assert ui.inputs["name"].area.text == ""
        assert ui.inputs["image"].area.text == SAMPLE["image"]


async def test_escape_cancels_mutation_confirmation():
    async with running() as (app, ui, model, keys, _, _):
        app.notebooks.stop("research", "demo-training")
        assert ui.title == "Stop Notebook?"
        await press(keys, "\x1b")
        await asyncio.sleep(0.5)
        assert ui.app.is_running
        assert ui.title == "Notebook status"
        assert model.get("research", "demo-training")["state"] == "Ready"


async def test_disconnect_retains_notebooks_and_editor_becomes_disconnected():
    async with running() as (app, ui, model, keys, _, _):
        app.notebooks.open("research", "demo-training")
        assert ui.title == "VS Code window · simulated"
        app.disconnect()
        assert model.status()["clients"][0]["state"] == "disconnected"
        app.authorize("demo-east")
        assert model.get("research", "demo-training")["state"] == "Ready"


async def test_fresh_setup_links_key_empty_index_and_profiles_without_authorizing():
    async with running(entry="setup", fresh=True) as (app, ui, model, keys, _, _):
        assert model.clusters == []
        await click(ui, keys, "Create key and empty index")
        await click(ui, keys, "Use an existing encryption key")
        await click(ui, keys, "Browse vault and items")
        await click(ui, keys, "Demo vault")
        await click(ui, keys, "lab · Local data encryption key")
        assert ui.title == "Name the connection index"
        await fill(ui, keys, "title", "Example index")
        await click(ui, keys, "Show item guide")
        assert ui.title == "Create index item · 2 of 2"
        await click(ui, keys, "Continue after simulated save")
        await click(ui, keys, "Browse vault and items")
        await click(ui, keys, "Demo vault")
        await click(ui, keys, "Example index")
        assert ui.title == "Profiles directory"
        await fill(ui, keys, "profiles_dir", "/demo/private/profiles")
        await click(ui, keys, "Confirm directory")
        assert ui.title == "Setup simulated"
        assert app.setup_references == {
            "key": "op://demo-vault/local-key/password",
            "index": "op://demo-vault/index/notesPlain",
            "profiles_dir": "/demo/private/profiles",
        }
        assert app.configured
        assert model.clusters == []
        assert model.state == "unauthorized"
        assert {button.label for button in ui.buttons} == {"Exit preview"}
        app.choose_cluster()
        assert ui.title == "No registered clusters"
        assert "lab cluster add" in ui.description
        assert {button.label for button in ui.buttons} == {"Exit preview"}


async def test_existing_index_setup_preserves_bindings_without_opening_session():
    async with running(entry="setup") as (app, ui, model, keys, _, _):
        clusters = [dict(cluster) for cluster in model.clusters]
        await click(ui, keys, "Connect an existing index")
        await click(ui, keys, "Browse vault and items")
        await click(ui, keys, "Demo vault")
        await click(ui, keys, "lab · Connection index")
        assert ui.title == "Verify configuration"
        assert "cluster" not in app.setup_references
        await click(ui, keys, "Choose profiles directory")
        await fill(ui, keys, "profiles_dir", "/demo/profiles")
        await click(ui, keys, "Confirm directory")
        assert model.clusters == clusters
        assert model.state == "unauthorized"
        assert app.setup_references["key"] == "op://demo-vault/local-key/password"
        assert {button.label for button in ui.buttons} == {"Exit preview"}


async def test_narrow_layout_and_help_are_separated():
    async with running(width=48, height=20) as (app, ui, model, keys, _, _):
        await click(ui, keys, "Help")
        assert ui._help_open
        await press(keys, "\x1b")
        await asyncio.sleep(0.5)
        assert not ui._help_open
        assert ui.app.is_running


async def test_wide_layout_never_draws_past_bounded_surface():
    async with running(width=180) as (app, ui, model, keys, _, _):
        screen = ui.app.renderer._last_screen
        for y in range(screen.height):
            line = "".join(screen.data_buffer[y][x].char for x in range(180)).rstrip()
            assert len(line) <= 78


async def test_history_is_visible_before_acceptance():
    async with running() as (app, ui, model, keys, _, _):
        app.notebooks.create("research")
        name = ui.inputs["name"].area
        ui.app.layout.focus(name)
        ui.app.invalidate()
        await asyncio.sleep(0.04)
        assert name.text == ""
        assert name.buffer.suggestion.text == "demo-training"
        await press(keys, "\t")
        assert name.text == "demo-training"


async def test_empty_recovery_picker_explains_the_next_action():
    async with running() as (app, ui, model, keys, _, _):
        app.notebooks.retry()
        assert "No creation receipts" in ui.description
        assert any(button.label == "Create Notebook" for button in ui.buttons)
