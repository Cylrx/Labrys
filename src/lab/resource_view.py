"""Browse cluster resource snapshots on the session's existing terminal surface."""

import asyncio
import math
from collections.abc import Callable

from prompt_toolkit.application import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, HorizontalAlign, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

from lab.errors import LabError
from lab.kubernetes import Kubernetes
from lab.resources import NodeResources, ResourceSnapshot, amount, load_resources
from lab.ui import Field, Screens


def fit(text: str, width: int, right=False, *, middle=False) -> str:
    if width < 1:
        return ""
    if get_cwidth(text) > width:
        if middle:
            left, tail = text[: len(text) // 2], text[len(text) // 2 :]
            while get_cwidth(left + "…" + tail) > width:
                if get_cwidth(left) >= get_cwidth(tail):
                    left = left[:-1]
                else:
                    tail = tail[1:]
            text = left + "…" + tail
        else:
            while text and get_cwidth(text + "…") > width:
                text = text[:-1]
            text += "…"
    padding = " " * max(0, width - get_cwidth(text))
    return padding + text if right else text + padding


class ResourceView(UIControl):
    """Keep a bounded node page, search buffer, and filters over one snapshot."""

    filters = (
        "All nodes",
        "Headroom ≥1",
        "Headroom ≥4",
        "Headroom ≥8",
        "No device units",
        "Unavailable",
    )
    sorts = ("Most headroom", "Node name", "Most reserved")
    headings = ("Node", "Model", "Reserved", "Units", "CPU", "RAM GiB", "State")
    numeric_columns = (3, 4, 5)

    def __init__(self, snapshot: ResourceSnapshot):
        self.snapshot = snapshot
        self.resource = next(
            (key for key in snapshot.resources if key.endswith("/gpu")),
            next(iter(snapshot.resources), ""),
        )
        self.model, self.filter, self.sort = "All", 0, 0
        self.selected, self.page_size = 0, 8
        self.detail, self.help = False, False
        self.notice = ""
        self._preferred_widths = list(map(get_cwidth, self.headings))
        self.respond: Callable[[str], None] = lambda value: None
        self.enabled: Callable[[], bool] = lambda: True
        self.search = TextArea(
            height=1, prompt="  / Search  ", multiline=False, wrap_lines=False, style="class:filter"
        )
        self.search.buffer.on_text_changed += lambda buffer: self.refilter()
        self.keys = KeyBindings()
        self._bindings()
        content = HSplit(
            [
                Window(FormattedTextControl(self.header), height=7, wrap_lines=False),
                self.search,
                Window(FormattedTextControl(self.toolbar), height=3, wrap_lines=False),
                Window(self, always_hide_cursor=True),
                Window(FormattedTextControl(self.footer), height=3, wrap_lines=False),
            ],
            width=lambda: Dimension.exact(self.width()),
            height=lambda: Dimension.exact(max(16, get_app().output.get_size().rows - 1)),
        )
        self.window = VSplit([content], align=HorizontalAlign.LEFT)
        self.visible: list[NodeResources] = []
        self.refilter()

    def update(self, snapshot: ResourceSnapshot):
        self.snapshot, self.notice = snapshot, ""
        if self.resource not in snapshot.resources:
            self.resource = next(iter(snapshot.resources), "")
        self.refilter()

    def refilter(self):
        words = self.search.text.casefold().split()
        nodes = []
        for node in self.snapshot.nodes:
            model = node.model(self.resource)
            if self.model != "All" and model != self.model:
                continue
            text = f"{node.name} {model} {node.state} {' '.join(node.allocatable)}".casefold()
            if not all(word in text for word in words):
                continue
            remaining = node.remaining(self.resource)
            if self.filter in (1, 2, 3) and (
                node.state != "Ready" or remaining is None or remaining < (1, 4, 8)[self.filter - 1]
            ):
                continue
            if self.filter == 4 and any(
                "/" in key and value for key, value in node.allocatable.items()
            ):
                continue
            if self.filter == 5 and node.state == "Ready":
                continue
            nodes.append(node)
        if self.sort == 0:
            nodes.sort(
                key=lambda node: (
                    node.state != "Ready",
                    -(node.remaining(self.resource) or 0),
                    node.name,
                )
            )
        elif self.sort == 2:
            nodes.sort(key=lambda node: (-node.reserved.get(self.resource, 0), node.name))
        else:
            nodes.sort(key=lambda node: node.name)
        self.visible, self.selected = nodes, 0
        self._preferred_widths = list(map(get_cwidth, self.headings))
        for node in nodes:
            for index, value in enumerate(self.node_cells(node)):
                self._preferred_widths[index] = max(
                    self._preferred_widths[index], get_cwidth(value)
                )

    def width(self):
        preferred = 3 + sum(self._preferred_widths) + 3 * (len(self.headings) - 1)
        return min(max(116, preferred), get_app().output.get_size().columns)

    def columns(self, width):
        """Fit content with uniform gutters; compress identifiers before numeric values."""
        widths = self._preferred_widths.copy()
        gaps = len(widths) - 1
        gap = " " * max(1, min(3, (width - 3 - sum(widths)) // gaps))
        minimum = (12, len(self.headings[1]))
        while 3 + sum(widths) + len(gap) * gaps > width:
            index = max(
                (i for i in (0, 1) if widths[i] > minimum[i]),
                key=widths.__getitem__,
                default=None,
            )
            if index is None:
                break
            widths[index] -= 1
        return widths, gap

    def table_line(self, cells, width, *, selected=False, heading=False):
        widths, gap = self.columns(width)
        style = "class:focused" if selected else "class:heading" if heading else ""
        prefix = " › " if selected else "   "
        if 3 + sum(widths) + len(gap) * (len(widths) - 1) > width:
            label = (
                "Widen the terminal for values; Enter opens node details" if heading else cells[0]
            )
            return [(style, prefix + fit(label, width - 3, middle=not heading))]
        line = [(style, prefix)]
        for index, (value, size) in enumerate(zip(cells, widths, strict=True)):
            if index:
                line.append((style, gap))
            cell = fit(value, size, right=index in self.numeric_columns, middle=index == 0)
            if index == 2 and not heading and not selected:
                occupied = cell.count("■")
                line.extend([("class:accent", cell[:occupied]), ("class:muted", cell[occupied:])])
            else:
                line.append((style, cell))
        return line

    def header(self):
        snapshot = self.snapshot
        nodes = snapshot.nodes
        ready = [node for node in nodes if node.state == "Ready"]
        key = self.resource
        capacity = sum(node.allocatable.get(key, 0) for node in nodes)
        reserved = sum(node.reserved.get(key, 0) for node in nodes)
        remaining = self.total(ready, key)
        observed = (
            "reservations unknown"
            if snapshot.namespaces == ()
            else f"{reserved:,} observed reserved"
        )
        units = f"{capacity:,} units · {observed}" if key else "CPU / memory"
        values = (
            f"{self.bound}{amount(remaining, key)} remaining"
            if key and remaining is not None
            else "Remaining unknown"
            if key
            else ""
        )
        unknown = sum(bool(node.issue) for node in nodes)
        return [
            ("class:accent", "  lab / Cluster resources"),
            (
                "class:muted",
                f"   {snapshot.observed_at.astimezone():%H:%M:%S} · {snapshot.scope}\n",
            ),
            ("class:heading", "  " + (key or "No extended resources advertised")),
            ("class:muted", "   g Change resource\n"),
            ("class:heading", f"  {units}    {values}\n"),
            (
                "",
                f"  {len(nodes):,} nodes · {len(ready):,} Ready"
                + (f" · {unknown} incomplete" if unknown else "")
                + f"    CPU {self.total_label(ready, 'cpu')} cores"
                f"    RAM {self.total_label(ready, 'memory')} GiB\n",
            ),
            ("class:muted", self.distribution(ready) + "\n"),
            (
                "class:muted",
                "  "
                + (
                    "Upper bounds: unobserved reservations may reduce availability."
                    if snapshot.partial
                    else "Unreserved snapshot; placement and quotas may restrict access."
                )
                + "\n",
            ),
            (
                "class:error" if self.notice else "class:muted",
                "  "
                + (
                    self.notice
                    or " · ".join(snapshot.issues)
                    or "CPU/RAM are request headroom. DRA devices are not included."
                ),
            ),
        ]

    @property
    def bound(self):
        return "≤" if self.snapshot.partial else ""

    def total(self, nodes, resource):
        relevant = [node for node in nodes if resource in node.allocatable]
        values = [node.remaining(resource) for node in relevant]
        return None if any(value is None for value in values) else sum(values)

    def total_label(self, nodes, resource):
        value = self.total(nodes, resource)
        return (self.bound if value is not None else "") + amount(value, resource)

    def distribution(self, nodes):
        values = [
            node.remaining(self.resource)
            for node in nodes
            if node.allocatable.get(self.resource, 0)
        ]
        values = [value for value in values if value is not None]
        buckets = [
            sum(value == 0 for value in values),
            sum(1 <= value <= 3 for value in values),
            sum(4 <= value <= 7 for value in values),
            sum(value >= 8 for value in values),
        ]
        return "  Ready nodes by remaining units:  " + "   ".join(
            f"{label}: {count}"
            for label, count in zip(("0", "1–3", "4–7", "8+"), buckets, strict=True)
        )

    def toolbar(self):
        pages = max(1, math.ceil(len(self.visible) / self.page_size))
        return [
            ("class:muted", "  f "),
            ("", self.filters[self.filter]),
            ("class:muted", "   t "),
            ("", self.model),
            ("class:muted", "   s "),
            ("", self.sorts[self.sort]),
            ("class:muted", "   c Clear\n"),
            ("class:heading", f"  NODES {len(self.visible):,} of {len(self.snapshot.nodes):,}"),
            (
                "class:muted",
                f"   page {self.selected // self.page_size + 1} of {pages} · remaining headroom\n",
            ),
            *self.table_line(self.headings, self.width(), heading=True),
        ]

    def footer(self):
        return [
            (
                "class:muted",
                "  ■ observed reserved · "
                + ("unverified" if self.snapshot.partial else "unreserved")
                + "   ? unknown   * tainted\n",
            ),
            ("", "  ↑↓ Move  ←→ Page  Enter Details  / Search  r Refresh  n Namespaces\n"),
            ("class:muted", "  g Resource  f Filter  t Model  s Sort  ? Help  Esc Back"),
        ]

    def node_cells(self, node):
        key = self.resource
        total, reserved = node.allocatable.get(key, 0), node.reserved.get(key, 0)
        if not total:
            bar = "—"
        else:
            cells = min(8, total)
            occupied = min(cells, math.ceil(reserved / total * cells))
            bar = "■" * occupied + "·" * (cells - occupied)
        values = []
        for resource in (key, "cpu", "memory"):
            value = node.remaining(resource)
            text = (
                "—"
                if node.state != "Ready" or resource not in node.allocatable
                else (self.bound if value is not None else "") + amount(value, resource)
            )
            values.append(text)
        state = node.state + ("*" if node.taints else "")
        return [node.name, node.model(key) or "—", bar, *values, state]

    def node_line(self, node, width, selected):
        return self.table_line(self.node_cells(node), width, selected=selected)

    def details(self, node):
        rows = [
            ("class:heading", f"  {node.name} · {node.state}"),
            ("class:muted", "  Resource                  Allocatable   Reserved   Remaining"),
        ]
        for key in dict.fromkeys(filter(None, (self.resource, "cpu", "memory"))):
            label = "RAM GiB" if key == "memory" else "CPU cores" if key == "cpu" else key
            left = (
                self.bound + amount(node.remaining(key), key)
                if node.remaining(key) is not None
                else "?"
            )
            rows.append(
                (
                    "",
                    "  "
                    + fit(label, 25)
                    + fit(amount(node.allocatable.get(key), key), 12, True)
                    + fit("?" if node.issue else amount(node.reserved.get(key, 0), key), 11, True)
                    + fit(left if node.state == "Ready" else "Unavailable", 12, True),
                )
            )
        rows.extend(
            [
                (
                    "class:muted",
                    "  "
                    + (
                        node.issue
                        or "Allocatable minus effective requests of visible, assigned pods."
                    ),
                ),
                (
                    "class:muted",
                    "  Units may be physical devices, partitions or shared slots, not device IDs.",
                ),
                ("class:muted", "  Taints: " + (", ".join(node.taints) or "none")),
                (
                    "class:muted",
                    "  "
                    + (
                        "Only selected namespaces are visible."
                        if self.snapshot.partial
                        else "All namespaces are visible."
                    ),
                ),
                (
                    "class:muted",
                    "  DRA devices, queue reservations and workload placement are not evaluated.",
                ),
            ]
        )
        return [[item] for item in rows]

    def create_content(self, width, height):
        lines: list[StyleAndTextTuples]
        if self.page_size != max(1, height):
            self.page_size = max(1, height)
            get_app().invalidate()
        self.selected = max(0, min(self.selected, len(self.visible) - 1))
        if self.help:
            lines = [
                [("", line)]
                for line in (
                    "  Search names, models, resource keys or node states with /.",
                    "  f cycles headroom filters; t cycles advertised model labels.",
                    "  g selects one resource type. Unlike units are never added together.",
                    "  CPU/RAM totals include Ready, uncordoned nodes; taints may restrict use.",
                    "  n adds namespace hints when automatic discovery is restricted.",
                    "  r refreshes. Failed refreshes retain the dated snapshot with a warning.",
                    "  ≤ values are upper bounds, not a promise a job will schedule.",
                    "  Esc returns. Home/End jump to the first/last node.",
                )
            ]
        elif self.detail and self.visible:
            lines = self.details(self.visible[self.selected])
        else:
            start = self.selected // self.page_size * self.page_size
            lines = [
                self.node_line(node, width, start + i == self.selected)
                for i, node in enumerate(self.visible[start : start + self.page_size])
            ]
            if not lines:
                lines = [[("class:muted", "  No matching nodes. Press c to clear filters.")]]
        return UIContent(
            get_line=lambda i: lines[i],
            line_count=len(lines),
            cursor_position=Point(0, 0),
            show_cursor=False,
        )

    def move(self, delta):
        self.selected = max(0, min(len(self.visible) - 1, self.selected + delta))

    def _bindings(self):
        enabled = Condition(lambda: self.enabled())

        def bind(key, function):
            self.keys.add(key, filter=enabled, eager=True)(lambda event: function())

        bind("escape", self.back)
        bind("q", self.back)
        bind("?", lambda: setattr(self, "help", not self.help))
        bind("/", lambda: get_app().layout.focus(self.search))
        bind("tab", lambda: get_app().layout.focus(self.search))
        bind("enter", lambda: setattr(self, "detail", not self.detail))
        bind("home", lambda: setattr(self, "selected", 0))
        bind("end", lambda: setattr(self, "selected", max(0, len(self.visible) - 1)))
        for key, delta in (("up", -1), ("down", 1), ("k", -1), ("j", 1)):
            bind(key, lambda delta=delta: self.move(delta))
        for key, direction in (("left", -1), ("pageup", -1), ("right", 1), ("pagedown", 1)):
            bind(key, lambda direction=direction: self.move(direction * self.page_size))
        for key, action in (("r", "refresh"), ("n", "namespaces")):
            bind(key, lambda action=action: self.respond(action))
        bind("f", lambda: self.cycle("filter", list(range(len(self.filters)))))
        bind("s", lambda: self.cycle("sort", list(range(len(self.sorts)))))
        bind(
            "t",
            lambda: self.cycle(
                "model",
                ["All"]
                + sorted(
                    {
                        node.model(self.resource)
                        for node in self.snapshot.nodes
                        if node.model(self.resource)
                    }
                ),
            ),
        )
        bind("g", lambda: self.cycle("resource", self.snapshot.resources or [""]))
        bind("c", self.clear_filters)
        search_keys = KeyBindings()
        for key in ("enter", "escape", "tab"):
            search_keys.add(key, filter=enabled, eager=True)(
                lambda event: event.app.layout.focus(self)
            )
        self.search.control.key_bindings = search_keys

    def clear_filters(self):
        self.model, self.filter = "All", 0
        self.search.text = ""
        self.refilter()

    def cycle(self, name, values):
        value = getattr(self, name)
        setattr(
            self,
            name,
            values[(values.index(value) + 1) % len(values)] if value in values else values[0],
        )
        if name == "resource":
            self.model = "All"
        self.refilter()

    def back(self):
        if self.help or self.detail:
            self.help, self.detail = False, False
        else:
            self.respond("back")

    def is_focusable(self):
        return self.enabled()

    def get_key_bindings(self):
        return self.keys

    def mouse_handler(self, event):
        if not self.enabled():
            return None
        if event.event_type in {MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN}:
            self.move(-3 if event.event_type == MouseEventType.SCROLL_UP else 3)
        elif event.event_type == MouseEventType.MOUSE_DOWN:
            get_app().layout.focus(self)
            if not self.detail and not self.help:
                self.selected = min(
                    max(0, len(self.visible) - 1),
                    self.selected // self.page_size * self.page_size + event.position.y,
                )
        return None


async def browse_resources(
    api: Kubernetes, screens: Screens, namespaces: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Refresh on request and retain a visibly dated snapshot after a failed refresh."""
    view = ResourceView(
        await screens.work(load_resources(api, namespaces), "Reading cluster resources")
    )
    while True:
        action = await screens.resources(view)
        if action == "back":
            return namespaces
        candidates = namespaces
        try:
            if action == "namespaces":
                values = await screens.form(
                    "Additional namespaces",
                    [Field("namespaces", "Namespace names", value=", ".join(namespaces))],
                    description="Extra names for clusters that hide namespace listings. "
                    "Lab automatically includes every readable namespace it discovers.",
                )
                candidates = tuple(
                    dict.fromkeys(
                        [
                            *namespaces,
                            *(
                                name.strip()
                                for name in values["namespaces"].split(",")
                                if name.strip()
                            ),
                        ]
                    )
                )
            view.update(
                await screens.work(load_resources(api, candidates), "Reading cluster resources")
            )
            namespaces = candidates
        except LabError as error:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            view.notice = "Previous snapshot · " + (
                "refresh cancelled" if error.code == 130 else str(error)
            )
