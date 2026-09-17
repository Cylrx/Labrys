"""A read-only, vertically scrollable text viewport with exact source retention."""

from collections.abc import Callable

from prompt_toolkit.application import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, ScrollOffsets, Window
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.utils import get_cwidth


class Document(UIControl):
    """Wrap only the display; clipboard actions use the unchanged source text."""

    def __init__(self, text: str, *, enabled: Callable[[], bool] = lambda: True):
        self.text = text
        self.enabled = enabled
        self.offset = 0
        self.width = 0
        self.height = 1
        self.lines: list[str] = []
        self.keys = KeyBindings()
        for key, direction in (("up", -1), ("down", 1)):
            self.keys.add(key)(lambda event, direction=direction: self.scroll(direction))
        self.keys.add("pageup")(lambda event: self.scroll(-self.height))
        self.keys.add("pagedown")(lambda event: self.scroll(self.height))
        self.keys.add("home")(lambda event: self.scroll(-len(self.lines)))
        self.keys.add("end")(lambda event: self.scroll(len(self.lines)))
        self.window = Window(
            self,
            height=self.viewport_height,
            wrap_lines=False,
            get_vertical_scroll=lambda window: self.offset,
            scroll_offsets=ScrollOffsets(),
            always_hide_cursor=True,
            right_margins=[ScrollbarMargin(display_arrows=True)],
        )

    def _wrap(self, width: int) -> list[str]:
        if width == self.width and self.lines:
            return self.lines
        lines = []
        for source in self.text.split("\n"):
            line, cells = "", 0
            for char in source.expandtabs(4):
                size = get_cwidth(char)
                if cells + size > width and line:
                    lines.append(line)
                    line, cells = "", 0
                line += char
                cells += size
            lines.append(line)
        self.width, self.lines = width, lines
        return lines

    def viewport_height(self):
        width = max(1, get_app().output.get_size().columns - 5)
        return Dimension.exact(
            min(len(self._wrap(width)), max(3, get_app().output.get_size().rows - 18))
        )

    def create_content(self, width, height):
        if width != self.width:
            self.lines = self._wrap(max(1, width))
            self.width = width
        self.height = height
        self.offset = min(self.offset, max(0, len(self.lines) - height))
        return UIContent(
            get_line=lambda index: [("", self.lines[index])],
            line_count=len(self.lines),
            cursor_position=Point(0, self.offset),
            show_cursor=False,
        )

    def scroll(self, rows: int) -> None:
        self.offset = max(0, min(self.offset + rows, len(self.lines) - self.height))

    def is_focusable(self):
        return self.enabled()

    def get_key_bindings(self):
        return self.keys

    def mouse_handler(self, event):
        if not self.enabled():
            return None
        if event.event_type in {MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN}:
            self.scroll(-3 if event.event_type == MouseEventType.SCROLL_UP else 3)
        elif event.event_type == MouseEventType.MOUSE_DOWN:
            get_app().layout.focus(self)
        else:
            return NotImplemented
        return None

    def clear(self):
        self.text, self.lines = "", []
