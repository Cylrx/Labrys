"""Keyboard-level verification of shared terminal controls."""

import asyncio

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from lab.inputs import Field, form


async def enter(fields, keys):
    with create_pipe_input() as terminal, create_app_session(input=terminal, output=DummyOutput()):
        operation = asyncio.create_task(form("Synthetic form", fields))
        await asyncio.sleep(0.02)
        terminal.send_text(keys)
        return await asyncio.wait_for(operation, 1)


async def test_typing_replaces_ghost_without_accepting_it():
    result = await enter([Field("image", "Image", candidates=["previous:v1"])], "custom:v2\r")
    assert result == {"image": "custom:v2"}


async def test_two_tabs_accept_then_advance():
    result = await enter([Field("image", "Image", candidates=["previous:v1"])], "\t\t\t\r")
    assert result == {"image": "previous:v1"}


async def test_history_moves_recent_to_older():
    result = await enter(
        [Field("image", "Image", candidates=["recent:v1", "older:v1"])], "\x1b[A\x1b[A\t\t\r"
    )
    assert result == {"image": "older:v1"}


async def test_numeric_stepping_preserves_units():
    result = await enter([Field("memory", "Memory", value="4Gi", numeric=True)], "\x1b[C\r")
    assert result == {"memory": "5Gi"}


async def test_direct_numeric_entry_replaces_the_stepped_value():
    result = await enter([Field("gpus", "GPUs", value="2", numeric=True)], "\x1b[C8\r")
    assert result == {"gpus": "8"}


async def test_inline_choice_filters_and_returns_the_original_id():
    from lab.inputs import choose

    with create_pipe_input() as terminal, create_app_session(input=terminal, output=DummyOutput()):
        operation = asyncio.create_task(
            choose("Select an item", [("first", "First item"), ("key-id", "Encryption key")])
        )
        await asyncio.sleep(0.02)
        terminal.send_text("encryption\r")
        assert await asyncio.wait_for(operation, 1) == "key-id"


async def test_inline_choice_cancels_without_accepting_default():
    import pytest

    from lab.errors import LabError
    from lab.inputs import choose

    with create_pipe_input() as terminal, create_app_session(input=terminal, output=DummyOutput()):
        operation = asyncio.create_task(choose("Select an item", [("first", "First item")]))
        await asyncio.sleep(0.02)
        terminal.send_text("\x03")
        with pytest.raises(LabError) as error:
            await asyncio.wait_for(operation, 1)
        assert error.value.code == 130


async def test_narrow_form_keeps_all_fields_keyboard_accessible_without_alternate_screen():
    from prompt_toolkit.data_structures import Size

    class NarrowTerminal(DummyOutput):
        def get_size(self):
            return Size(rows=20, columns=48)

        def enter_alternate_screen(self):
            raise AssertionError("Inline forms must keep the terminal's normal screen")

    fields = [Field(f"field{i}", f"Field {i}") for i in range(11)]
    with (
        create_pipe_input() as terminal,
        create_app_session(input=terminal, output=NarrowTerminal()),
    ):
        operation = asyncio.create_task(form("Synthetic form", fields))
        await asyncio.sleep(0.02)
        terminal.send_text("".join(f"value{i}\t" for i in range(11)) + "\t\r")
        assert await asyncio.wait_for(operation, 1) == {f"field{i}": f"value{i}" for i in range(11)}
