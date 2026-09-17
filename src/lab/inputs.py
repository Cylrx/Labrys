"""Short terminal prompts using the shared native page renderer."""

import sys
from contextlib import ExitStack, contextmanager

from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_input
from prompt_toolkit.output import create_output

from lab.errors import LabError
from lab.ui import Field, Input, Screens

__all__ = [
    "Field",
    "Input",
    "Screens",
    "ask",
    "choose",
    "confirm",
    "controlling_terminal",
    "form",
    "terminal_required",
]


@contextmanager
def controlling_terminal(enabled: bool):
    """Keep editor interaction separate from an explicitly piped token input."""
    if not enabled:
        yield
        return
    with ExitStack() as resources:
        try:
            terminal = resources.enter_context(open("/dev/tty", "r+", buffering=1))
        except OSError:
            raise LabError("Editor interaction requires a controlling terminal.", 2) from None
        keyboard = create_input(terminal)
        resources.callback(keyboard.close)
        resources.enter_context(create_app_session(input=keyboard, output=create_output(terminal)))
        yield


def terminal_required() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise LabError("This action requires an interactive terminal.", 2)


async def choose(title, choices, *, description=""):
    return await Screens().choose(title, choices, description)


async def ask(label, *, hidden=False, default="", description=""):
    screens = Screens(output=create_output(sys.stderr) if hidden else None)
    values = await screens.form(
        label,
        [Field("value", label, default, secret=hidden)],
        description=description,
        cancel_label="Cancel",
    )
    return values["value"]


async def form(title, fields):
    return await Screens().form(title, fields, submit_label="Review")


async def confirm(message):
    try:
        return await Screens().confirm("Confirm action", message, [])
    except LabError as error:
        if error.code == 130:
            return False
        raise
