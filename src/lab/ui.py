"""Native terminal pages shared by interactive lab workflows and the simulator."""

from __future__ import annotations

import asyncio
import os
import textwrap
import weakref
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import Clipboard, DummyClipboard, DynamicClipboard, InMemoryClipboard
from prompt_toolkit.document import Document as BufferDocument
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
    DynamicContainer,
    FormattedTextControl,
    HorizontalAlign,
    HSplit,
    Layout,
    ScrollablePane,
    VSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.controls import BufferControl, UIControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor, PasswordProcessor
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

from lab import clipboard
from lab.display import current_display
from lab.document import Document
from lab.errors import LabError

T = TypeVar("T")

WIDTH = 78


def available_width() -> int:
    return max(12, min(WIDTH, get_app().output.get_size().columns) - 4)


def paragraph(value, style="", *, height=None, offset=0) -> Window:
    def content():
        result = value() if callable(value) else value
        return "\n".join(
            textwrap.fill(
                line,
                max(12, available_width() - offset),
                break_long_words=False,
                break_on_hyphens=False,
            )
            for line in str(result).split("\n")
        )

    return Window(
        FormattedTextControl(content),
        style=style,
        height=height,
        wrap_lines=True,
        dont_extend_height=True,
    )


def literal(value: str) -> Window:
    return Window(FormattedTextControl(value), wrap_lines=True, dont_extend_height=True)


@dataclass
class Field:
    name: str
    label: str
    value: str = field(default="", repr=False)
    candidates: list[str] = field(default_factory=list, repr=False)
    numeric: bool = False
    step: str = "1"
    group: str = ""
    secret: bool = False
    help: str = ""
    placeholder: str = ""


class Candidates(AutoSuggest):
    def __init__(self, values: Sequence[str]) -> None:
        self.values = list(dict.fromkeys(values))
        self.index = -1

    def get_suggestion(self, buffer, document):
        if self.values and not document.text:
            return Suggestion(self.values[max(0, self.index)])
        return None

    def cycle(self, direction):
        if self.values:
            self.index = 0 if self.index < 0 else (self.index + direction) % len(self.values)
            return self.values[self.index]
        return None


class SecretBuffer(Buffer):
    def __init__(self, max_bytes: int, error: Callable[[str], None], enabled: Callable[[], bool]):
        self.max_bytes, self.error = max_bytes, error
        self.rejected = False
        super().__init__(
            multiline=True, history=DummyHistory(), read_only=Condition(lambda: not enabled())
        )

    def fits(self, text: str) -> bool:
        if len(text) <= self.max_bytes and len(text.encode("utf-8")) <= self.max_bytes:
            return True
        self.rejected = True
        self.error(f"Paste exceeds the {self.max_bytes:,}-byte limit. Paste a smaller kubeconfig.")
        return False

    def set_document(self, value: BufferDocument, bypass_readonly: bool = False) -> None:
        if self.fits(value.text):
            super().set_document(value, bypass_readonly=bypass_readonly)

    def _set_text(self, value: str) -> bool:
        if not self.fits(value):
            return False
        self.rejected = False
        return super()._set_text(value)

    def save_to_undo_stack(self, clear_redo_stack: bool = True) -> None:
        pass

    def clear(self) -> None:
        self.reset()
        self.rejected = False


class SecretInput:
    def __init__(self, buffer: SecretBuffer, enabled: Callable[[], bool]):
        self.buffer = buffer
        self.control = BufferControl(
            buffer=buffer,
            input_processors=[PasswordProcessor()],
            focusable=Condition(enabled),
            focus_on_click=Condition(enabled),
        )
        self.window = Window(
            self.control,
            height=lambda: max(3, min(12, get_app().output.get_size().rows - 16)),
            wrap_lines=True,
            right_margins=[ScrollbarMargin(display_arrows=True)],
            style="class:filter",
        )

    @property
    def text(self):
        return self.buffer.text

    def __pt_container__(self):
        return self.window


class Input:
    def __init__(
        self,
        definition: Field,
        advance: Callable[[], None],
        *,
        on_enter: Callable[[], None] | None = None,
        label_width: int = 22,
        navigate: Callable[[str], None] | None = None,
        enabled: Callable[[], bool] = lambda: True,
    ):
        self.definition = definition
        self.candidates = Candidates([] if definition.secret else definition.candidates)
        self.replace_on_type = definition.numeric and bool(definition.value)
        self.area = TextArea(
            text=definition.value,
            password=definition.secret,
            history=DummyHistory(),
            multiline=False,
            height=1,
            wrap_lines=False,
            focus_on_click=Condition(enabled),
            focusable=Condition(enabled),
            width=Dimension(preferred=42, max=48),
            auto_suggest=self.candidates,
            input_processors=[
                ConditionalProcessor(
                    AfterInput([("class:muted", definition.placeholder)]),
                    Condition(
                        lambda: (
                            bool(definition.placeholder)
                            and not self.area.text
                            and not self.area.buffer.suggestion
                        )
                    ),
                )
            ],
        )
        self.area.buffer.cursor_position = len(self.area.text)

        def suggest(_):
            self.area.buffer.suggestion = self.candidates.get_suggestion(
                self.area.buffer, self.area.buffer.document
            )

        self.area.buffer.on_text_changed += suggest
        suggest(self.area.buffer)
        keys = KeyBindings()

        @keys.add("tab")
        def accept(event):
            suggestion = self.candidates.get_suggestion(self.area.buffer, self.area.buffer.document)
            if suggestion:
                self.area.text = suggestion.text
                self.area.buffer.cursor_position = len(self.area.text)
                self.replace_on_type = definition.numeric
            else:
                advance()

        for key, direction in (("up", 1), ("down", -1)):

            @keys.add(key)
            def history(event, direction=direction):
                value = self.candidates.cycle(direction)
                if value is not None:
                    self.area.text = value
                    self.area.buffer.cursor_position = len(value)
                    self.replace_on_type = definition.numeric
                elif navigate:
                    navigate("up" if direction == 1 else "down")

        if definition.numeric:
            for key, direction in (("left", -1), ("right", 1)):

                @keys.add(key)
                def quantity(event, direction=direction):
                    raw, suffix = self.area.text, ""
                    while raw and raw[-1].isalpha():
                        suffix, raw = raw[-1] + suffix, raw[:-1]
                    try:
                        number = max(
                            Decimal(0), Decimal(raw or "0") + Decimal(definition.step) * direction
                        )
                    except InvalidOperation:
                        return
                    self.area.text = f"{number:f}{suffix}"
                    self.area.buffer.cursor_position = len(self.area.text)
                    self.replace_on_type = True

        @keys.add("enter")
        def next_field(event):
            (on_enter or advance)()

        @keys.add("<any>")
        def type_value(event):
            if self.replace_on_type:
                self.area.text, self.replace_on_type = "", False
            self.area.buffer.insert_text(event.data)

        self.area.control.key_bindings = keys

        def focus_label(event):
            if enabled() and event.event_type == MouseEventType.MOUSE_DOWN:
                get_app().layout.focus(self.area)
                return None
            return NotImplemented

        label = FormattedTextControl(
            lambda: [
                (
                    "class:accent" if get_app().layout.has_focus(self.area) else "class:muted",
                    ("› " if get_app().layout.has_focus(self.area) else "  ") + definition.label,
                    focus_label,
                )
            ]
        )
        label_width = max(label_width, get_cwidth(definition.label) + 2)
        wide = VSplit([Window(label, width=label_width, height=1), Window(width=2), self.area])
        narrow = HSplit([Window(label, wrap_lines=True, dont_extend_height=True), self.area])
        self.row = DynamicContainer(
            lambda: narrow if available_width() < max(58, label_width + 26) else wide
        )


class Action:
    def __init__(
        self,
        label: str,
        callback: Callable,
        invoke: Callable,
        primary=False,
        preserve_focus=False,
        enabled: Callable[[], bool] = lambda: True,
        disabled_reason: str = "",
    ):
        self.label, self.callback, self.invoke, self.primary = label, callback, invoke, primary
        self.preserve_focus = preserve_focus
        self.disabled_reason = disabled_reason
        self.enabled = lambda: not self.disabled_reason and enabled()
        keys = KeyBindings()

        @keys.add("enter")
        @keys.add(" ")
        def activate(event):
            if self.enabled():
                self.invoke(self.callback)

        self.control = FormattedTextControl(
            self.fragments,
            focusable=not bool(disabled_reason),
            key_bindings=keys,
            show_cursor=False,
        )
        self.window = Window(
            self.control, wrap_lines=True, dont_extend_height=True, dont_extend_width=True
        )

    @property
    def display_label(self) -> str:
        return f"{self.label} ({self.disabled_reason})" if self.disabled_reason else self.label

    def fragments(self) -> StyleAndTextTuples:
        if self.disabled_reason:
            return [("class:muted", "  " + self.display_label, self.mouse)]
        focused = get_app().layout.has_focus(self.control)
        style = "class:focused" if focused else "class:accent" if self.primary else ""
        return [(style, ("› " if focused else "  ") + self.label, self.mouse)]

    def mouse(self, event: MouseEvent):
        if not self.enabled():
            return None
        if event.button != MouseButton.LEFT:
            return NotImplemented
        if event.event_type == MouseEventType.MOUSE_DOWN:
            if not self.preserve_focus:
                get_app().layout.focus(self.control)
        elif event.event_type == MouseEventType.MOUSE_UP:
            if not self.preserve_focus:
                get_app().layout.focus(self.control)
            self.invoke(self.callback)
        return None


class UI:
    """Build aligned page groups while keeping all interaction inside one region."""

    def __init__(
        self,
        *,
        error_types: tuple[type[Exception], ...] = (LabError, ValueError),
        badge: str = "",
        exit_message: str | None = "",
        output: Output | None = None,
        copy_text: Callable[[str], Awaitable[None]] = clipboard.copy,
    ):
        self.context = "lab"
        self._public_clipboard = InMemoryClipboard()
        self._hidden_clipboard = DummyClipboard()
        self.reply: asyncio.Future | None = None
        self.accepting = True
        self.on_exit: Callable[[], None] | None = None
        self.copy_text = copy_text
        self.documents: list[Document] = []
        self._copy_task: asyncio.Task | None = None
        self._copy_button: Action | None = None
        self.error_types, self.badge = error_types, badge
        self.exit_message, self.output = exit_message, output
        self.title, self.description, self.error_message = "", "", ""
        self.controls: list[UIControl] = []
        self.inputs: dict[str, Input] = {}
        self.command: TextArea | None = None
        self._search: TextArea | None = None
        self._paste: SecretInput | None = None
        self.buttons: list[Action] = []
        self._footer: list[Action] = []
        self._body = HSplit([])
        self._back: Callable | None = None
        self._help_text, self._help_open = "", False
        self._active_field: Input | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._generation = 0
        self.app: Application[Any] | None = None

    def invoke(self, callback):
        if not self.accepting:
            return
        try:
            callback()
        except self.error_types as error:
            self.error(str(error))
        if self.app:
            self.app.invalidate()

    def error(self, message):
        self.error_message = str(message)
        if self.app:
            self.app.invalidate()

    def _move(self, direction):
        if not self.controls or not self.app:
            return
        current = self.app.layout.current_control
        index = self.controls.index(current) if current in self.controls else -1
        self.app.layout.focus(self.controls[(index + direction) % len(self.controls)])

    def _surface_width(self) -> int:
        columns = get_app().output.get_size().columns
        return columns if self.documents else min(WIDTH, columns)

    def _footer_is_horizontal(self) -> bool:
        return (
            sum(get_cwidth(button.display_label) + 4 for button in self._footer)
            <= self._surface_width() - 4
        )

    def _navigate(self, direction: str) -> None:
        if not self.app:
            return
        footer: list[UIControl] = [
            button.control for button in self._footer if button.control in self.controls
        ]
        rows: list[list[UIControl]] = []
        for control in self.controls:
            if control in footer and self._footer_is_horizontal():
                if control is footer[0]:
                    rows.append(footer)
            else:
                rows.append([control])
        current = self.app.layout.current_control
        for row_index, row in enumerate(rows):
            if current not in row:
                continue
            column = row.index(current)
            if direction in {"left", "right"}:
                column += -1 if direction == "left" else 1
                if 0 <= column < len(row):
                    self.app.layout.focus(row[column])
            else:
                row_index += -1 if direction == "up" else 1
                if 0 <= row_index < len(rows):
                    row = rows[row_index]
                    self.app.layout.focus(row[min(column, len(row) - 1)])
            return

    def _hint(self) -> str:
        current = self.app.layout.current_control if self.app else None
        if isinstance(current, Document):
            return "Scroll / ↑↓ · PgUp/PgDn · Home/End · Tab actions · Shift-drag select"
        for item in self.inputs.values():
            if current is item.area.control:
                arrows = "←→ adjust" if item.definition.numeric else "←→ cursor"
                vertical = "↑↓ history" if item.candidates.values else "↑↓ fields"
                return (
                    "Shift-Tab previous · Tab accept / next · F1 help\n"
                    + arrows
                    + " · "
                    + vertical
                    + " · Enter next / submit · Esc back"
                )
        if self._paste and current is self._paste.control:
            return (
                "Enter newline · ↑↓ cursor · Tab / Shift-Tab actions · Import to submit · Esc back"
            )
        if self._search and current is self._search.control:
            return "Type to filter · ↓ results · Tab next · Enter select · Esc back"
        if self.command and current is self.command.control:
            return "Type a command · ←→ cursor · Tab next · Enter run · Esc back"
        if (
            len(self._footer) > 1
            and self._footer_is_horizontal()
            and any(current is button.control for button in self._footer)
        ):
            return "←→ select · ↑↓ change row · Tab next · Enter confirm · Esc back"
        return "↑↓ select · Tab next · Enter confirm · Esc back"

    def _editing_clipboard(self) -> Clipboard:
        """Keep masked text out of the shared editing clipboard and kill ring."""
        current = self.app.layout.current_control if self.app else None
        if self._paste is not None or any(
            item.definition.secret and current is item.area.control for item in self.inputs.values()
        ):
            return self._hidden_clipboard
        return self._public_clipboard

    def _clear_paste(self):
        if self._paste:
            self._paste.buffer.clear()
            self._paste = None

    def clear(self):
        self._clear_paste()
        for document in self.documents:
            document.clear()
        for item in self.inputs.values():
            item.area.buffer.reset()
        if self.command:
            self.command.buffer.reset()
        if self._search:
            self._search.buffer.reset()
        self.inputs, self.documents, self.controls, self.buttons = {}, [], [], []
        self.command, self._search = None, None
        self._active_field, self._copy_button = None, None
        self._footer = []
        self._body = HSplit([])
        self._back = None
        self.title, self.description, self._help_text = "", "", ""
        self.reply = None

    def _begin(self, title, description, back, help_text):
        self._clear_paste()
        self.accepting = True
        for item in self.inputs.values():
            item.area.buffer.reset()
        if self.command:
            self.command.buffer.reset()
        if self._search:
            self._search.buffer.reset()
        self._generation += 1
        for document in self.documents:
            document.clear()
        self.documents = []
        self._copy_button = None
        if self._copy_task:
            self._copy_task.cancel()
        if self._timer:
            self._timer.cancel()
        self.title, self.description = title, description
        self.error_message = ""
        self._back, self._help_text, self._help_open = back, help_text, False
        self._active_field = None
        self.controls, self.buttons, self.inputs = [], [], {}
        self._footer = []
        self.command, self._search = None, None

    def _action(self, label, callback, primary=False, preserve_focus=False, disabled_reason=""):
        generation = self._generation
        action = Action(
            label,
            callback,
            self.invoke,
            primary,
            preserve_focus,
            enabled=lambda: self.accepting and self._generation == generation,
            disabled_reason=disabled_reason,
        )
        self.buttons.append(action)
        if not disabled_reason:
            self.controls.append(action.control)
        return action

    def _finish(self, blocks, actions, *, preferred=None, back_label="Back", disabled=None):
        if self._back and not any(callback is self._back for _, callback in actions):
            actions = [(back_label, self._back), *actions]
        buttons = [
            self._action(
                label,
                callback,
                primary=index == len(actions) - 1,
                disabled_reason=(disabled or {}).get(label, ""),
            )
            for index, (label, callback) in enumerate(actions)
        ]
        self._footer = buttons
        footer = []
        if buttons:
            row = VSplit([button.window for button in buttons], padding=2)

            column = HSplit([button.window for button in buttons])
            footer.extend(
                [
                    Window(height=2),
                    DynamicContainer(lambda: column if not self._footer_is_horizontal() else row),
                ]
            )
        if self._help_text or self.inputs:
            help_button = self._action("Help", self._toggle_help, preserve_focus=True)
            footer.extend(
                [
                    Window(height=1),
                    help_button.window,
                    ConditionalContainer(
                        VSplit(
                            [
                                Window(width=2),
                                paragraph(lambda: self._help_text, "class:muted", offset=2),
                            ]
                        ),
                        filter=Condition(lambda: self._help_open and not self.inputs),
                    ),
                ]
            )
        footer.extend(
            [
                Window(height=1),
                paragraph(
                    self._hint,
                    "class:muted",
                ),
            ]
        )
        self._body = HSplit(
            [
                *blocks,
                ConditionalContainer(
                    HSplit(
                        [Window(height=1), paragraph(lambda: self.error_message, "class:error")]
                    ),
                    filter=Condition(lambda: bool(self.error_message)),
                ),
                *footer,
            ]
        )
        if self.app and self.controls:
            target = preferred or self.controls[0]
            self.app.layout = Layout(self.app.layout.container, focused_element=target)
            self.app.invalidate()

    def _track_field(self, app):
        for item in self.inputs.values():
            if app.layout.current_control is item.area.control:
                self._active_field = item
                break

    def _field_help(self) -> str:
        if self._active_field and self._active_field.definition.help:
            return self._active_field.definition.help
        return self._help_text or (
            "Type a value or accept a grey suggestion with Tab. "
            "Shift-Tab returns to the previous field without changing any values."
        )

    def _toggle_help(self):
        self._help_open = not self._help_open
        if (
            self._active_field
            and self.app
            and not self.app.layout.has_focus(self._active_field.area)
        ):
            self.app.layout.focus(self._active_field.area)

    def menu(self, title, description, sections, back=None, on_command=None, help_text=""):
        self._begin(title, description, back, help_text)
        blocks: list = []
        for heading, entries in sections:
            if blocks:
                blocks.append(Window(height=1))
            if heading:
                blocks.append(paragraph(heading, "class:muted"))
            buttons = [self._action(label, callback) for label, callback in entries]

            blocks.append(HSplit([button.window for button in buttons]))

        if on_command:
            blocks.extend(self._command_input(on_command))
        self._finish(blocks, [])

    def _command_input(self, on_command):
        generation = self._generation
        command = TextArea(
            height=1,
            multiline=False,
            focus_on_click=Condition(lambda: self.accepting and self._generation == generation),
            width=Dimension(preferred=42, max=70),
            prompt="› ",
        )
        self.command = command
        keys = KeyBindings()

        @keys.add("enter")
        def dispatch(event):
            value = command.text
            command.text = ""
            if value.strip():
                self.invoke(lambda: on_command(value))

        command.control.key_bindings = keys
        self.controls.append(command.control)
        return [Window(height=2), paragraph("COMMAND", "class:muted"), command]

    def table(
        self,
        title,
        description,
        choices,
        states,
        actions,
        back=None,
        on_command=None,
        *,
        name_label="Name",
        empty_message="No items found.",
    ):
        """Render selectable names and independently updated, fixed-width status cells."""
        self._begin(title, description, back, "")
        minimum = get_cwidth(name_label) + 2
        longest = max(minimum, max((get_cwidth(label) + 2 for _, label, _ in choices), default=0))
        status_width = max(12, max((get_cwidth(value) + 2 for value in states.values()), default=0))

        def name_width():
            return max(minimum, min(longest, available_width() - status_width - 2))

        blocks: list = [
            VSplit(
                [
                    Window(
                        FormattedTextControl("  " + name_label),
                        width=name_width,
                        height=1,
                        style="class:muted",
                    ),
                    Window(
                        FormattedTextControl("  Status"),
                        width=status_width,
                        height=1,
                        style="class:muted",
                    ),
                ],
                padding=2,
            )
        ]
        for key, label, callback in choices:
            button = self._action(label, callback)
            button.window.width = name_width
            button.window.wrap_lines = Condition(lambda: False)

            def status(key=key, button=button):
                value = states[key]
                style = "class:muted" if value == "Checking…" else ""
                return [(style, "  " + value, button.mouse)]

            blocks.append(
                VSplit(
                    [
                        button.window,
                        Window(FormattedTextControl(status), width=status_width, height=1),
                    ],
                    padding=2,
                )
            )
        if not choices:
            blocks.append(paragraph("  " + empty_message, "class:muted"))
        if on_command:
            blocks.extend(self._command_input(on_command))
        self._finish(blocks, actions)

    def choose(self, title, description, choices):
        self._begin(title, description, None, "")
        generation = self._generation
        search = TextArea(
            height=1,
            multiline=False,
            focus_on_click=Condition(lambda: self.accepting and self._generation == generation),
            width=Dimension(preferred=42, max=48),
            history=DummyHistory(),
        )
        self._search = search
        search_box = VSplit(
            [Window(width=2, height=1), search, Window(width=2, height=1)],
            width=Dimension(preferred=52, max=52),
            style=lambda: (
                "class:filter-focused" if get_app().layout.has_focus(search) else "class:filter"
            ),
        )
        keys = KeyBindings()

        @keys.add("enter")
        def select_first(event):
            if self.buttons:
                self.invoke(self.buttons[0].callback)

        @keys.add("down")
        def next_choice(event):
            self._navigate("down")

        @keys.add("up")
        def previous_choice(event):
            self._navigate("up")

        search.control.key_bindings = keys

        def refresh(_=None):
            self.controls, self.buttons = [search.control], []
            query = search.text.casefold()
            buttons = [
                self._action(label, callback)
                for label, callback in choices
                if query in label.casefold()
            ]
            self._finish(
                [
                    paragraph("FILTER", "class:muted"),
                    search_box,
                    Window(height=1),
                    HSplit([button.window for button in buttons])
                    if buttons
                    else paragraph("No matches. Edit the filter to try again.", "class:muted"),
                ],
                [],
                preferred=search.control,
            )

        search.buffer.on_text_changed += refresh
        refresh()

    def form(
        self,
        title,
        description,
        fields,
        submit_label,
        on_submit,
        back,
        help_text="",
        cancel_label="Back",
    ):
        self._begin(title, description, back, help_text)
        blocks: list = []
        group = None
        label_width = max((get_cwidth(item.label) + 2 for item in fields), default=22)
        generation = self._generation
        inputs = self.inputs

        def submit():
            if self.accepting and generation == self._generation:
                on_submit({name: item.area.text for name, item in inputs.items()})

        for index, definition in enumerate(fields):
            if definition.group != group:
                if blocks:
                    blocks.append(Window(height=1))
                if definition.group:
                    blocks.append(paragraph(definition.group, "class:muted"))
                group = definition.group
            control = Input(
                definition,
                lambda: self._move(1),
                on_enter=(lambda: self.invoke(submit)) if index == len(fields) - 1 else None,
                label_width=max(22, label_width),
                navigate=self._navigate,
                enabled=lambda: self.accepting and generation == self._generation,
            )
            self.inputs[definition.name] = control
            self.controls.append(control.area.control)
            blocks.append(control.row)
        self._finish(
            blocks,
            [(submit_label, submit)],
            back_label=cancel_label,
        )

    def paste(self, title, description, on_submit, back, *, max_bytes=1048576):
        if max_bytes < 1:
            raise ValueError("Paste limit must be positive.")
        self._begin(title, description, back, "")
        generation = self._generation

        def enabled():
            return self.accepting and self._generation == generation

        buffer = SecretBuffer(max_bytes, self.error, enabled)
        area = SecretInput(buffer, enabled)
        self._paste = area
        keys = KeyBindings()

        @keys.add("enter")
        def newline(event):
            buffer.insert_text("\n")

        @keys.add("up")
        def up(event):
            buffer.cursor_up()

        @keys.add("down")
        def down(event):
            buffer.cursor_down()

        @keys.add("tab")
        def next_action(event):
            self._move(1)

        @keys.add("s-tab")
        def previous_action(event):
            self._move(-1)

        @keys.add("<any>")
        @keys.add("<bracketed-paste>")
        def insert(event):
            buffer.insert_text(event.data)

        area.control.key_bindings = keys
        self.controls.append(area.control)

        def submit():
            if enabled() and not buffer.rejected and buffer.fits(buffer.text):
                on_submit(buffer.text)
                self.accepting = False

        self._finish(
            [
                area,
                paragraph(
                    lambda: (
                        f"{len(buffer.text):,} characters · "
                        f"{len(buffer.document.lines):,} lines · masked"
                    ),
                    "class:muted",
                ),
            ],
            [("Import", submit)],
        )

    def details(
        self,
        title,
        description,
        rows,
        actions,
        back=None,
        help_text="",
        copy_text=None,
        disabled=None,
    ):
        self._begin(title, description, back, help_text)
        blocks: list = []
        generation = self._generation
        for label, value in rows:
            if "\n" in str(value) or (copy_text is not None and str(value) == copy_text):
                document = Document(
                    str(value), enabled=lambda: self.accepting and self._generation == generation
                )
                self.documents.append(document)
                self.controls.append(document)
                if label:
                    blocks.append(paragraph(label, "class:muted"))
                blocks.append(document.window)
                continue
            if not label:
                blocks.append(literal(str(value)))
                continue
            wide = VSplit(
                [
                    Window(FormattedTextControl(str(label)), width=20, style="class:muted"),
                    Window(width=2),
                    literal(str(value)),
                ]
            )
            narrow = HSplit(
                [paragraph(str(label), "class:muted"), literal(str(value)), Window(height=1)]
            )

            def row(wide=wide, narrow=narrow):
                return narrow if available_width() < 58 else wide

            blocks.append(DynamicContainer(row))
        if copy_text is not None:
            actions = [("Copy", lambda: self._copy(copy_text)), *actions]
        self._finish(blocks, actions, disabled=disabled)
        if copy_text is not None:
            self._copy_button = next(button for button in self._footer if button.label == "Copy")

    def _copy(self, text: str) -> None:
        if self._copy_task and not self._copy_task.done():
            return
        button = self._copy_button
        assert button is not None
        button.label = "Copying…"
        generation = self._generation

        async def perform():
            try:
                await self.copy_text(text)
            except LabError as error:
                if generation == self._generation:
                    button.label = "Copy"
                    self.error(str(error))
            except Exception:
                if generation == self._generation:
                    button.label = "Copy"
                    self.error("Clipboard copy failed. Select the text manually or try again.")
            else:
                if generation == self._generation:
                    button.label = "Copied"
                    self.error_message = ""
            if self.app:
                self.app.invalidate()

        self._copy_task = asyncio.create_task(perform())

    def confirm(self, title, description, rows, on_confirm, back):
        self.details(title, description, rows, [("Cancel", back), ("Confirm", on_confirm)])
        self._back = back
        if self.app:
            self.app.layout.focus(self.buttons[0].control)

    def schedule(self, delay_seconds, callback):
        generation = self._generation
        if self._timer:
            self._timer.cancel()

        def run():
            if self._generation == generation:
                self.invoke(callback)

        self._timer = asyncio.get_running_loop().call_later(delay_seconds, run)

    def exit(self):
        if self.on_exit:
            self.on_exit()
        elif self.app:
            self.app.exit(result=self.exit_message)

    async def run(self):
        keys = KeyBindings()

        @keys.add("<any>", filter=Condition(lambda: not self.accepting), eager=True)
        def waiting(event):
            return

        @keys.add("c-c", filter=Condition(lambda: not self.accepting), eager=True)
        def interrupt_wait(event):
            self.exit()

        @keys.add("tab")
        def forward(event):
            self._move(1)

        @keys.add("s-tab")
        def backward(event):
            self._move(-1)

        for direction in ("up", "down", "left", "right"):

            @keys.add(
                direction,
                filter=(
                    Condition(
                        lambda: not isinstance(get_app().layout.current_control, BufferControl)
                    )
                    if direction in {"left", "right"}
                    else True
                ),
            )
            def navigate(event, direction=direction):
                self._navigate(direction)

        @keys.add("f1", eager=True)
        def help_toggle(event):
            if self.accepting and (self.inputs or self._help_text):
                self._toggle_help()

        @keys.add("escape")
        def back(event):
            if self._help_open:
                self._help_open = False
            elif self._back:
                self.invoke(self._back)
            else:
                self.exit()

        @keys.add("c-c")
        def interrupt(event):
            self.exit()

        @keys.add("<any>")
        def start_command(event):
            assert self.app is not None
            current = self.app.layout.current_control
            target = self.command or self._search
            if target and (not isinstance(current, BufferControl) or current is target.control):
                self.app.layout.focus(target)
                target.buffer.insert_text(event.data)

        content = HSplit(
            [
                Window(height=1),
                VSplit(
                    [
                        paragraph(lambda: self.context, "class:accent"),
                        Window(
                            FormattedTextControl(lambda: self.badge),
                            height=1,
                            style="class:muted",
                            align=WindowAlign.RIGHT,
                        ),
                    ]
                ),
                Window(height=1),
                paragraph(lambda: self.title, "class:heading"),
                ConditionalContainer(
                    paragraph(lambda: self.description, "class:muted"),
                    filter=Condition(lambda: bool(self.description)),
                ),
                Window(height=2),
                DynamicContainer(lambda: self._body),
                Window(height=1),
            ],
            width=lambda: Dimension.exact(max(1, self._surface_width() - 4)),
        )
        pane = ScrollablePane(
            VSplit([Window(width=2), content, Window(width=2)]),
            width=lambda: Dimension.exact(self._surface_width()),
            show_scrollbar=False,
        )

        @keys.add("pagedown")
        def scroll_down(event):
            if self.documents:
                self.documents[0].scroll(self.documents[0].height)
            else:
                pane.vertical_scroll += max(1, event.app.output.get_size().rows - 4)

        @keys.add("pageup")
        def scroll_up(event):
            if self.documents:
                self.documents[0].scroll(-self.documents[0].height)
            else:
                pane.vertical_scroll = max(
                    0, pane.vertical_scroll - max(1, event.app.output.get_size().rows - 4)
                )

        field_help = VSplit(
            [
                Window(width=2),
                HSplit(
                    [
                        Window(height=1),
                        paragraph(
                            lambda: (
                                self._active_field.definition.label
                                if self._active_field
                                else "Help"
                            ),
                            "class:heading",
                        ),
                        paragraph(self._field_help, "class:muted"),
                        Window(height=1),
                    ]
                ),
                Window(width=2),
            ],
            width=lambda: Dimension.exact(self._surface_width()),
        )
        root = HSplit(
            [
                VSplit([pane], align=HorizontalAlign.LEFT),
                ConditionalContainer(
                    field_help, filter=Condition(lambda: self._help_open and bool(self.inputs))
                ),
            ]
        )
        self.app = Application(
            clipboard=DynamicClipboard(self._editing_clipboard),
            layout=Layout(root, focused_element=self.controls[0] if self.controls else None),
            key_bindings=keys,
            full_screen=False,
            mouse_support=True,
            erase_when_done=True,
            output=self.output,
            before_render=self._track_field,
            min_redraw_interval=1 / 60,
            style=Style.from_dict(
                {
                    "accent": "bold" if "NO_COLOR" in os.environ else "ansicyan bold",
                    "heading": "bold",
                    "muted": "" if "NO_COLOR" in os.environ else "ansibrightblack",
                    "focused": "reverse bold",
                    "filter": "reverse" if "NO_COLOR" in os.environ else "bg:#202d3d #e4edf7",
                    "filter-focused": (
                        "reverse bold" if "NO_COLOR" in os.environ else "bg:#2c4056 #f0f6ff"
                    ),
                    "error": "bold" if "NO_COLOR" in os.environ else "ansired",
                    "auto-suggestion": "ansibrightblack",
                }
            ),
        )
        try:
            return await self.app.run_async()
        finally:
            if self._timer:
                self._timer.cancel()
            if self._copy_task:
                if not self._copy_task.done():
                    self._copy_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._copy_task
                self._copy_task = None


class Screens:
    """Present sequential pages on the active display or a standalone surface.

    :param context: Session identity displayed above each page.
    :param badge: Optional environment label.
    :param output: Terminal output override, including secret-entry stderr output.
    """

    def __init__(self, context: str = "lab", badge: str = "", output: Output | None = None):
        self.context, self.badge, self.output = context, badge, output

    def _page(self) -> UI:
        display = current_display()
        if display is not None:
            return display.page(self.context, self.badge)
        ui = UI(badge=self.badge, output=self.output, exit_message=None)
        ui.context = self.context
        return ui

    @staticmethod
    def _respond(ui: UI) -> Callable[[Any], None]:
        reply_ref = weakref.ref(ui.reply) if ui.reply is not None else None

        def respond(value: Any) -> None:
            if reply_ref is not None:
                reply = reply_ref()
                if reply is not None and not reply.done():
                    ui.accepting = False
                    reply.set_result(value)
            elif ui.app and ui.app.is_running:
                ui.app.exit(result=value)

        return respond

    @staticmethod
    def _select(ui: UI, value: Any) -> Callable[[], None]:
        respond = Screens._respond(ui)
        return lambda: respond(value)

    @staticmethod
    async def _show(ui: UI) -> Any:
        display = current_display()
        try:
            answer = await display.show(ui) if display is not None else await ui.run()
            if answer is None:
                raise LabError("Cancelled.", 130)
            return answer
        finally:
            ui._clear_paste()
            if display is None:
                ui.clear()
                ui.app = None

    async def menu(
        self,
        title: str,
        description: str,
        sections: Sequence[tuple[str, Sequence[tuple[str, str]]]],
        command: bool = False,
        help_text: str = "",
    ) -> str:
        """Return a selected identifier or ``command:`` followed by entered text."""
        ui = self._page()
        respond = self._respond(ui)
        ui.menu(
            title,
            description,
            [
                (heading, [(label, self._select(ui, key)) for key, label in choices])
                for heading, choices in sections
            ],
            on_command=(lambda text: respond("command:" + text)) if command else None,
            help_text=help_text,
        )
        return await self._show(ui)

    async def table(
        self,
        title: str,
        description: str,
        choices: Sequence[tuple[str, str, str]],
        actions: Sequence[tuple[str, str]],
        *,
        name_label: str = "Name",
        empty_message: str = "No items found.",
    ) -> str:
        """Return the identifier of a selected row or action from a status table."""
        ui = self._page()
        ui.table(
            title,
            description,
            [(key, label, self._select(ui, key)) for key, label, _ in choices],
            {key: status for key, _, status in choices},
            [(label, self._select(ui, key)) for key, label in actions],
            name_label=name_label,
            empty_message=empty_message,
        )
        return await self._show(ui)

    async def status_list(
        self,
        title: str,
        description: str,
        choices: Sequence[tuple[str, str]],
        load: Callable[[], Awaitable[dict[str, str]]],
        actions: Sequence[tuple[str, str]],
    ) -> str:
        """Keep the list interactive while a page-owned lookup fills its status cells."""
        ui = self._page()
        states = dict.fromkeys((key for key, _ in choices), "Checking…")
        ui.table(
            title,
            description,
            [(key, label, self._select(ui, key)) for key, label in choices],
            states,
            [(label, self._select(ui, key)) for key, label in actions],
            empty_message="No Notebooks found in this namespace.",
        )

        async def update():
            try:
                values = await load()
                for key in states:
                    states[key] = values.get(key, "Unknown")
            except Exception as error:
                states.update(dict.fromkeys(states, "Unknown"))
                ui.error(
                    str(error)
                    if isinstance(error, LabError)
                    else "Notebook status lookup failed. Refresh to retry."
                )
            if ui.app:
                ui.app.invalidate()

        lookup = asyncio.create_task(update())
        try:
            return await self._show(ui)
        finally:
            lookup.cancel()
            await asyncio.gather(lookup, return_exceptions=True)

    async def choose(
        self, title: str, choices: Sequence[tuple[str, str]], description: str = ""
    ) -> str:
        """Filter local labels and return the identifier of a selected choice."""
        if not choices:
            raise LabError("No choices are available. Enter an explicit reference or value.", 4)
        ui = self._page()
        ui.choose(title, description, [(label, self._select(ui, key)) for key, label in choices])
        return await self._show(ui)

    async def form(
        self,
        title: str,
        fields: Sequence[Field],
        description: str = "",
        submit_label: str = "Continue",
        help_text: str = "",
        cancel_label: str = "Back",
    ) -> dict[str, str]:
        """Read editable fields without echoing or retaining their values."""
        ui = self._page()
        ui.form(
            title,
            description,
            fields,
            submit_label,
            self._respond(ui),
            self._select(ui, None),
            help_text,
            cancel_label,
        )
        return await self._show(ui)

    async def paste(self, title: str, description: str, *, max_bytes: int = 1048576) -> str:
        """Read masked multiline text in memory and return it only on Import."""
        ui = self._page()
        ui.paste(title, description, self._respond(ui), self._select(ui, None), max_bytes=max_bytes)
        return await self._show(ui)

    async def details(
        self,
        title: str,
        description: str,
        rows: Sequence[tuple[str, str]],
        actions: Sequence[tuple[str, str]],
        help_text: str = "",
        copy_text: str | None = None,
        disabled: dict[str, str] | None = None,
    ) -> str:
        """Display literal read-only values and return the selected action identifier."""
        ui = self._page()
        ui.details(
            title,
            description,
            rows,
            [(label, self._select(ui, key)) for key, label in actions],
            help_text=help_text,
            copy_text=copy_text,
            disabled={
                label: disabled[key] for key, label in actions if disabled and key in disabled
            },
        )
        return await self._show(ui)

    async def confirm(self, title: str, description: str, rows: Sequence[tuple[str, str]]) -> bool:
        """Request confirmation with Cancel focused initially."""
        ui = self._page()
        ui.confirm(title, description, rows, self._select(ui, True), self._select(ui, False))
        ui._back = None
        return await self._show(ui)

    async def work(
        self,
        awaitable: Awaitable[T],
        title: str,
        description: str = "",
        rows: Sequence[tuple[str, str]] = (),
        cancel_label: str = "Cancel",
    ) -> T:
        """Await work beside a cancellable page and preserve backend failure identity.

        Cancellation joins the operation before returning, allowing the backend
        to report an accepted remote resource through :class:`~lab.errors.LabError`.
        """
        page: asyncio.Task | None = None
        display = current_display()
        if display is not None:
            display.owner = asyncio.current_task()
        operation = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait((operation,), timeout=0.15)
            if operation in done:
                return await operation
            ui = self._page()
            ui.details(
                title.rstrip(".…") + "...",
                description,
                rows,
                [(cancel_label, self._select(ui, None))],
            )
            page = asyncio.create_task(self._show(ui))
            done, _ = await asyncio.wait((page, operation), return_when=asyncio.FIRST_COMPLETED)
            if operation in done:
                return await operation
            await page
            raise LabError("Cancelled.", 130)
        finally:
            if not operation.done():
                operation.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await operation
            finally:
                if page is not None:
                    if not page.done():
                        page.cancel()
                    with suppress(asyncio.CancelledError, LabError):
                        await page
