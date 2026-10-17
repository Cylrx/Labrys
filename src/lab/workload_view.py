"""Browse who holds cluster resources: owners, then their workloads, then a workload's pods."""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from prompt_toolkit.application import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
    HorizontalAlign,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

from lab.resource_view import fit
from lab.resources import PodRecord, ResourceSnapshot, amount
from lab.workloads import Workload, group

Cell = str | tuple[str, str]


@dataclass(frozen=True)
class Column:
    heading: str
    key: Callable[[Any], Any] = lambda row: None  # sort value; None sorts last either way
    right: bool = False
    shrink: int = 0  # narrowest width when the terminal is tight; 0 keeps the full width
    accent: bool = False


def table(
    columns: Sequence[Column],
    rows: list[list[Cell]],
    width: int,
    selected: int | None = None,
    muted=(),
) -> tuple[list[StyleAndTextTuples], list[tuple[int, int]]]:
    """Render headings and rows, and return each column's horizontal span for clicks.

    A (used, requested) cell lines up on its slash.
    """
    halves = {}
    for index in range(len(columns)):
        pairs = [row[index] for row in rows if isinstance(row[index], tuple)]
        if pairs:
            halves[index] = tuple(max(get_cwidth(pair[side]) for pair in pairs) for side in (0, 1))

    def text(index: int, value: Cell) -> str:
        if isinstance(value, str):
            return value
        left, right = halves[index]
        return fit(value[0], left, right=True) + " / " + fit(value[1], right)

    cells = [[text(index, value) for index, value in enumerate(row)] for row in rows]
    widths = [
        max([get_cwidth(column.heading)] + [get_cwidth(row[index]) for row in cells])
        for index, column in enumerate(columns)
    ]
    gap = 3
    while 2 + sum(widths) + gap * (len(widths) - 1) > width:
        if gap > 2:
            gap -= 1
            continue
        tight = [
            i for i, column in enumerate(columns) if column.shrink and widths[i] > column.shrink
        ]
        if not tight:
            break
        widths[max(tight, key=widths.__getitem__)] -= 1

    def line(values, style, prefix="  ", heading=False) -> StyleAndTextTuples:
        out: StyleAndTextTuples = [(style, prefix)]
        for index, (column, value, size) in enumerate(zip(columns, values, widths, strict=True)):
            if index:
                out.append((style, " " * gap))
            accent = column.accent and not heading and not style
            out.append(
                (
                    "class:accent" if accent else style,
                    fit(
                        value,
                        size,
                        right=column.right or index in halves,
                        middle=bool(column.shrink),
                    ),
                )
            )
        return out

    lines = [line([column.heading for column in columns], "class:muted", heading=True)]
    for index, values in enumerate(cells):
        if index == selected:
            lines.append(line(values, "class:focused", "› "))
        else:
            lines.append(line(values, "class:muted" if index in muted else ""))
    spans, start = [], 2
    for size in widths:
        spans.append((start, start + size))
        start += size + gap
    return lines, spans


def fuzzy(tokens: list[str], fields: list[str]) -> int:
    """Score folded fields against every token; 0 means some token matched nothing.

    A token scores 3 at the start of a word, 2 elsewhere in a field, and 1 when its
    letters only appear in order, so typing "ts1" still finds "train-sweep-1".
    """
    total = 0
    for token in tokens:
        best = 0
        for text in fields:
            at = text.find(token)
            if at >= 0:
                best = max(best, 3 if at == 0 or not text[at - 1].isalnum() else 2)
            elif not best:
                letters = iter(text)
                best = int(all(letter in letters for letter in token))
            if best == 3:
                break
        if not best:
            return 0
        total += best
    return total


def age(start: datetime | None, now: datetime) -> str:
    if start is None:
        return "—"
    seconds = max(0, int((now - start).total_seconds()))
    days, hours, minutes = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
    return f"{days}d{hours}h" if days else f"{hours}h{minutes}m" if hours else f"{minutes}m"


@dataclass
class Owner:
    name: str
    workloads: list[Workload]


Row = Owner | Workload | PodRecord


def identity(row: Row) -> Any:
    if isinstance(row, Owner):
        return row.name
    if isinstance(row, Workload):
        return (row.namespace, row.kind, row.name)
    return (row.namespace, row.name)


@dataclass
class Level:
    """One table of the drill-down: what it lists, the row it was opened from, its search."""

    kind: str  # "owners", "workloads" or "pods"
    parent: Any = None  # identity of the owner or workload this level was opened from
    query: str = ""


USAGE = ("CPU cores used / req", "RAM GiB used / req")
ARROWS = {False: " ↑", True: " ↓"}


class WorkloadView(UIControl):
    """Every level is the same sortable, searchable, paged table; only its rows differ.

    Levels keep identities, never row lists, so each refilter derives rows from the snapshot.
    """

    title = "Cluster workloads"

    def __init__(self, snapshot: ResourceSnapshot, owner_key: str, context: str):
        self.owner_key, self.context = owner_key, context
        self.levels = [Level("owners" if owner_key else "workloads")]
        # Per kind: (column index, descending), or None for the default order.
        self.order: dict[str, tuple[int, bool] | None] = dict.fromkeys(
            ("owners", "workloads", "pods")
        )
        self.help = self.everything = False
        self.visible: list = []
        self.spans: list[tuple[int, int]] = []
        self.selected, self.page_size = 0, 8
        self.notice = ""
        self.respond: Callable[[str], None] = lambda value: None
        self.enabled: Callable[[], bool] = lambda: True
        self.search = TextArea(
            height=1,
            prompt="/ ",
            multiline=False,
            wrap_lines=False,
            input_processors=[
                ConditionalProcessor(
                    AfterInput([("class:muted", "search workloads, owners, nodes…")]),
                    Condition(lambda: not self.search.text),
                )
            ],
        )
        self.search.buffer.on_text_changed += self.searched
        self.keys = KeyBindings()
        self._bindings()
        search_box = VSplit(
            [Window(width=1), self.search, Window(width=1)],
            width=Dimension(preferred=56, max=56),
            style=lambda: (
                "class:filter-focused"
                if get_app().layout.has_focus(self.search)
                else "class:filter"
            ),
        )
        content = HSplit(
            [
                Window(
                    FormattedTextControl(self.top),
                    height=lambda: Dimension.exact(len(self.top_lines())),
                    wrap_lines=False,
                ),
                ConditionalContainer(
                    VSplit([Window(width=2), search_box]),
                    filter=Condition(lambda: not self.help),
                ),
                Window(FormattedTextControl(self.section), height=2, wrap_lines=False),
                Window(self, always_hide_cursor=True),
                Window(FormattedTextControl(self.footer), height=3, wrap_lines=False),
            ],
            width=lambda: Dimension.exact(self.width()),
            height=lambda: Dimension.exact(max(18, get_app().output.get_size().rows - 1)),
        )
        self.window = VSplit([content], align=HorizontalAlign.LEFT)
        self.resource = ""
        self.update(snapshot)

    @property
    def level(self) -> Level:
        return self.levels[-1]

    def update(self, snapshot: ResourceSnapshot):
        self.snapshot, self.notice = snapshot, ""
        if self.resource not in snapshot.resources:
            self.resource = next(
                (key for key in snapshot.resources if key.endswith("/gpu")),
                next(iter(snapshot.resources), ""),
            )
        self.items = group(snapshot.pods or [])
        self.refilter(self.current())

    # -- data

    def used(self, pods: list[PodRecord], resource: str) -> int | None:
        """Current use from metrics.k8s.io; None when any placed pod has no sample."""
        usage = self.snapshot.usage or {}
        samples = [
            usage.get((pod.namespace, pod.name), {}).get(resource) for pod in pods if pod.node
        ]
        return None if not samples or None in samples else sum(value or 0 for value in samples)

    def usage(self, pods: list[PodRecord], resource: str) -> Cell:
        requested = sum((pod.requests or {}).get(resource, 0) for pod in pods if pod.node)
        if not requested:
            return "—"
        return (amount(self.used(pods, resource), resource), amount(requested, resource))

    def held(self, items: list[Workload]) -> int | None:
        """Units held by these workloads; None when some request cannot be interpreted."""
        if any(item.unknown for item in items):
            return None
        return sum(item.held(self.resource) for item in items)

    def relevant(self, item: Workload) -> bool:
        return (
            self.everything
            or not self.resource
            or item.unknown
            or bool(item.held(self.resource) or item.waiting(self.resource))
        )

    def scope(self) -> list[Workload]:
        """Workloads under the rows each level was opened from."""
        items = self.items
        for level in self.levels[1:]:
            if level.kind == "workloads":
                items = [item for item in items if item.owner == level.parent]
            else:
                items = [item for item in items if identity(item) == level.parent]
        return items

    def rows(self) -> list:
        """The current level's rows in their default order."""
        resource, kind = self.resource, self.level.kind
        if kind == "pods":
            return sorted((pod for item in self.scope() for pod in item.pods), key=identity)
        items = [item for item in self.scope() if self.relevant(item)]
        if kind == "workloads":
            return sorted(
                items, key=lambda item: (-item.held(resource), -item.waiting(resource), item.name)
            )
        grouped: dict[str, list[Workload]] = {}
        for item in items:
            grouped.setdefault(item.owner, []).append(item)
        return sorted(
            (Owner(name, found) for name, found in grouped.items()),
            key=lambda owner: (
                -sum(item.held(resource) for item in owner.workloads),
                owner.name or "~",
            ),
        )

    def columns(self, kind: str) -> tuple[Column, ...]:
        resource, now = self.resource, self.snapshot.observed_at

        def used(key: str) -> Callable[[Row], int | None]:
            return lambda row: self.used(self.pods(row), key)

        usage = tuple(
            Column(heading, used(key), right=True)
            for heading, key in zip(USAGE, ("cpu", "memory"), strict=True)
        )
        if kind == "owners":
            return (
                Column("Owner", lambda owner: owner.name.casefold() or None, shrink=10),
                Column("Held", lambda owner: self.held(owner.workloads), right=True, accent=True),
                Column(
                    "Waiting",
                    lambda owner: sum(item.waiting(resource) for item in owner.workloads),
                    right=True,
                ),
                Column("Workloads", lambda owner: len(owner.workloads), right=True),
                Column(
                    "Nodes",
                    lambda owner: len({node for item in owner.workloads for node in item.nodes()}),
                    right=True,
                ),
                *usage,
            )
        if kind == "workloads":
            return (
                Column("Workload", lambda item: item.name.casefold(), shrink=12),
                Column("Kind", lambda item: item.kind.casefold()),
                Column("Owner", lambda item: item.owner.casefold() or None, shrink=6),
                Column("Held", lambda item: self.held([item]), right=True, accent=True),
                Column("Node", lambda item: " ".join(item.nodes()) or None, shrink=8),
                *usage,
                Column(
                    "Age",
                    lambda item: now - item.started() if item.started() else None,
                    right=True,
                ),
                Column("State", lambda item: item.state(resource)),
            )
        return (
            Column("Pod", lambda pod: pod.name, shrink=12),
            Column("Node", lambda pod: pod.node or None, shrink=8),
            Column(
                "Held",
                lambda pod: None if pod.requests is None else pod.requests.get(resource, 0),
                right=True,
                accent=True,
            ),
            *usage,
            Column("Age", lambda pod: now - pod.started if pod.started else None, right=True),
            Column("State", lambda pod: pod.phase if pod.node else "Waiting"),
        )

    @staticmethod
    def pods(row: Row) -> list[PodRecord]:
        if isinstance(row, Owner):
            return [pod for item in row.workloads for pod in item.pods]
        return row.pods if isinstance(row, Workload) else [row]

    def cells(self, row: Row) -> list[Cell]:
        resource, now = self.resource, self.snapshot.observed_at

        def count(value: int | None) -> str:
            return "?" if value is None else f"{value:,}"

        usage = [self.usage(self.pods(row), "cpu"), self.usage(self.pods(row), "memory")]
        if isinstance(row, Owner):
            waiting = sum(item.waiting(resource) for item in row.workloads)
            return [
                row.name or "no label",
                count(self.held(row.workloads)),
                f"{waiting:,}" if waiting else "—",
                f"{len(row.workloads):,}",
                f"{len({node for item in row.workloads for node in item.nodes()}):,}",
                *usage,
            ]
        if isinstance(row, Workload):
            nodes = row.nodes()
            return [
                row.name,
                row.kind,
                row.owner or "—",
                count(self.held([row])),
                nodes[0] if len(nodes) == 1 else f"{len(nodes)} nodes" if nodes else "—",
                *usage,
                age(row.started(), now),
                row.state(resource),
            ]
        return [
            row.name,
            row.node or "—",
            count(None if row.requests is None else row.requests.get(resource, 0)),
            *usage,
            age(row.started, now),
            row.phase if row.node else "Waiting",
        ]

    def muted(self, row: Row) -> bool:
        """Rows that hold nothing of the selected resource are dimmed."""
        if isinstance(row, PodRecord):
            return not row.node
        return self.held(row.workloads if isinstance(row, Owner) else [row]) == 0

    @staticmethod
    def fields(row: Row) -> list[str]:
        """Text a search can match: what the row shows, folded once per search."""
        if isinstance(row, Owner):
            values = [row.name or "no label", *(item.name for item in row.workloads)]
        elif isinstance(row, Workload):
            values = [row.name, row.owner, row.kind, row.namespace, *row.nodes()]
        else:
            values = [row.name, row.node, row.phase]
        return [value.casefold() for value in values]

    def current(self) -> Any:
        return identity(self.visible[self.selected]) if self.visible else None

    def refilter(self, keep: Any = None):
        """Derive, search and order the current level's rows; keep the given row selected."""
        rows = self.rows()
        tokens = self.level.query.casefold().split()
        if tokens:
            scores = {id(row): fuzzy(tokens, self.fields(row)) for row in rows}
            rows = [row for row in rows if scores[id(row)]]
        order = self.order[self.level.kind]
        if order is not None:
            key = self.columns(self.level.kind)[order[0]].key
            known = [row for row in rows if key(row) is not None]
            known.sort(key=key, reverse=order[1])
            rows = known + [row for row in rows if key(row) is None]
        elif tokens:
            rows.sort(key=lambda row: -scores[id(row)])  # stable: ties keep the default order
        self.visible = rows
        found = [identity(row) for row in rows]
        self.selected = found.index(keep) if keep in found else 0

    # -- page skeleton: context, title, description, summary, search, section

    def width(self) -> int:
        return max(60, get_app().output.get_size().columns - 2)

    def top_lines(self) -> list[StyleAndTextTuples]:
        width = self.width()
        clock = f"{self.snapshot.observed_at.astimezone():%H:%M:%S}"
        lines: list[StyleAndTextTuples] = [
            [],
            [
                ("class:accent", f"  {self.context}"),
                ("class:muted", " " * max(1, width - 2 - get_cwidth(self.context) - len(clock))),
                ("class:muted", clock),
            ],
            [],
        ]
        if self.help:
            return lines + [[("class:heading", "  About Cluster workloads")], []]
        level = self.level
        extra: list[StyleAndTextTuples] = []
        if level.kind == "pods":
            namespace, kind, name = level.parent
            found = self.scope()
            owner = found[0].owner if found else ""
            state = found[0].state(self.resource) if found else "finished"
            title = f"  {name}"
            description = f"  {kind} in {namespace} · " + (
                f"owner {owner}" if owner else "no owner label"
            )
            description += f" · {state}"
            reasons = dict.fromkeys(pod.waiting for item in found for pod in item.pods)
            extra = [
                [("class:error", "  " + fit(reason, width - 2))] for reason in reasons if reason
            ]
        elif level.parent is not None:
            title = f"  {level.parent or 'Pods without an owner label'}"
            description = (
                f"  Workloads whose pods carry the label {self.owner_key}={level.parent}."
                if level.parent
                else f'  Workloads whose pods have no "{self.owner_key}" label.'
            )
        else:
            title = "  Cluster workloads"
            scope = (
                self.snapshot.scope if self.snapshot.namespaces is not None else "all namespaces"
            )
            description = f"  Who holds {self.resource or 'cluster resources'} in {scope}."
            if self.snapshot.partial:
                description += " Namespaces you cannot read are not counted."
            if not self.owner_key:
                description += " Owners are unknown without a creation profile."
        return lines + [
            [("class:heading", title)],
            [("class:muted", description)],
            *extra,
            [("class:error", "  " + self.notice)] if self.notice else [],
            self.summary(width),
            [],
        ]

    def summary(self, width: int) -> StyleAndTextTuples:
        items = self.scope()
        capacity = sum(node.allocatable.get(self.resource, 0) for node in self.snapshot.nodes)
        held = sum(item.held(self.resource) for item in items)
        waiting = sum(item.waiting(self.resource) for item in items)
        if not capacity:
            return [("class:muted", "  No extended resources advertised by readable nodes.")]
        cells = max(1, min(capacity, width - 44))
        filled = min(cells, round(held / capacity * cells))
        unknown = any(item.unknown for item in items)
        return [
            ("class:accent", "  " + "■" * filled),
            ("class:muted", "·" * (cells - filled)),
            (
                "class:heading",
                f"   {'≥' if unknown else ''}{held:,} of {capacity:,} held"
                + (f" · {waiting:,} waiting" if waiting else ""),
            ),
            ("class:muted", " · some requests unreadable" if unknown else ""),
            ("class:muted", "   g " + self.resource),
        ]

    def top(self) -> StyleAndTextTuples:
        out: StyleAndTextTuples = []
        for index, line in enumerate(self.top_lines()):
            out += ([("", "\n")] if index else []) + line
        return out

    def section(self) -> StyleAndTextTuples:
        if self.help:
            return []
        kind = self.level.kind
        if len(self.levels) > 1 or not self.owner_key:
            tabs: StyleAndTextTuples = [("class:muted", "  " + kind.upper())]
        else:
            tabs = [
                ("class:heading" if kind == tab else "class:muted", "  " + tab.upper())
                for tab in ("owners", "workloads")
            ]
        details = [f"{len(self.visible):,} shown"]
        if kind != "pods":
            resource = self.resource.rsplit("/", 1)[-1]
            everything = self.everything or not resource
            details.append("all workloads" if everything else "using " + resource)
        order = self.order[kind]
        if order is not None:
            details.append("by " + self.columns(kind)[order[0]].heading + ARROWS[order[1]])
        elif self.level.query.strip():
            details.append("best match")
        return [("", "\n"), *tabs, ("class:muted", "     " + " · ".join(details))]

    def footer(self) -> StyleAndTextTuples:
        kind = self.level.kind
        if self.help:
            rows = [[("Esc", "Back")]]
        else:
            first = [("↑↓", "Move")] + ([("Enter", "Open")] if kind != "pods" else [])
            if len(self.levels) == 1 and self.owner_key:
                first.append(("Tab", "Owners / Workloads"))
            second = [("f", "Filter")] if kind != "pods" else []
            rows = [
                first + [("/", "Search"), ("Esc", "Back")],
                second
                + [("s S", "Sort"), ("g", "Resource"), ("r", "Refresh")]
                + [("n", "Namespaces"), ("?", "Help")],
            ]
        line: StyleAndTextTuples = []
        for keys in rows:
            line.append(("", "\n "))
            for key, label in keys:
                line += [("", " " + key + " "), ("class:muted", label + "  ")]
        return line

    # -- body

    def help_lines(self) -> list[StyleAndTextTuples]:
        owner = (
            f'Owner is the pod label "{self.owner_key}" from the cluster profile.'
            if self.owner_key
            else "Owners need a creation profile that names the owner label."
        )
        return [
            [("", "  " + text)] if text else []
            for text in (
                "OWNERS lists everyone holding or waiting for the resource; Enter opens their",
                "workloads. WORKLOADS lists the pods grouped by controller, such as a job, a",
                "Notebook or a ReplicaSet; Enter lists its pods and the nodes they run on.",
                "",
                "Held counts the selected resource requested by pods already placed on a node.",
                "A waiting workload holds nothing yet; its pods show the scheduler's reason.",
                "? marks requests Lab cannot interpret, such as Dynamic Resource Allocation.",
                "f also shows workloads that neither hold nor wait for the resource.",
                "",
                "The search bar matches any shown text by words, parts or letters in order.",
                "Click a heading to sort ascending, descending, then by default; s and S too.",
                "",
                owner,
                "CPU and RAM show current use from metrics.k8s.io, which kubectl top reads,",
                "beside the request. ? means no usage sample was available. GPU activity is",
                "not part of the Kubernetes API, so it is not shown.",
                "",
                "Pods in namespaces you cannot read are missing, so their devices look free.",
                "n adds namespace names when the cluster hides the namespace list.",
            )
        ]

    def page(self) -> tuple[int, list[Row]]:
        start = self.selected // self.page_size * self.page_size
        return start, self.visible[start : start + self.page_size]

    def create_content(self, width, height):
        self.selected = max(0, min(self.selected, len(self.visible) - 1))
        lines: list[StyleAndTextTuples]
        if self.help:
            lines = self.help_lines()
        elif self.snapshot.pods is None:
            lines = [
                [("class:muted", "  Pods are not readable. n adds namespace names you can read.")]
            ]
        else:
            if self.page_size != max(1, height - 2):
                self.page_size = max(1, height - 2)
                get_app().invalidate()
            start, page = self.page()
            columns = list(self.columns(self.level.kind))
            if order := self.order[self.level.kind]:
                column = columns[order[0]]
                columns[order[0]] = replace(column, heading=column.heading + ARROWS[order[1]])
            if page:
                lines, self.spans = table(
                    columns,
                    [self.cells(row) for row in page],
                    width,
                    selected=self.selected - start,
                    muted={index for index, row in enumerate(page) if self.muted(row)},
                )
            else:
                lines = [[("class:muted", "  Nothing matches. c clears the search and filter.")]]
            if len(self.visible) > self.page_size:
                pages = math.ceil(len(self.visible) / self.page_size)
                lines.append(
                    [
                        (
                            "class:muted",
                            f"  page {start // self.page_size + 1} of {pages} · ←→ to turn",
                        )
                    ]
                )
        return UIContent(
            get_line=lambda i: lines[i] if i < len(lines) else [],
            line_count=len(lines),
            cursor_position=Point(0, 0),
            show_cursor=False,
        )

    # -- input

    def searched(self, buffer):
        self.level.query = self.search.text
        self.refilter()

    def move(self, delta):
        self.selected = max(0, min(len(self.visible) - 1, self.selected + delta))

    def open(self):
        if not self.visible or self.level.kind == "pods":
            return
        row = self.visible[self.selected]
        self.levels.append(Level("workloads" if isinstance(row, Owner) else "pods", identity(row)))
        self.search.text = ""
        self.refilter()

    def choose(self):
        get_app().layout.focus(self)
        self.open()

    def back(self):
        if self.help:
            self.help = False
        elif len(self.levels) > 1:
            left = self.levels.pop()
            self.search.text = self.level.query
            self.refilter(left.parent)
        else:
            self.respond("back")

    def switch(self):
        if len(self.levels) == 1 and self.owner_key:
            kind = "workloads" if self.level.kind == "owners" else "owners"
            self.levels = [Level(kind, query=self.level.query)]
            self.refilter()

    def clear(self):
        self.everything = False
        self.search.text = ""
        self.refilter()

    def _bindings(self):
        enabled = Condition(lambda: self.enabled())
        browsing = enabled & Condition(lambda: not self.help)

        def bind(key, function, condition=browsing):
            self.keys.add(key, filter=condition, eager=True)(lambda event: function())

        bind("escape", self.back, enabled)
        bind("q", self.back, enabled)
        bind("?", lambda: setattr(self, "help", not self.help), enabled)
        bind("enter", self.open)
        bind("tab", self.switch)
        bind("/", lambda: get_app().layout.focus(self.search))
        bind("home", lambda: setattr(self, "selected", 0))
        bind("end", lambda: setattr(self, "selected", max(0, len(self.visible) - 1)))
        for key, delta in (("up", -1), ("down", 1), ("k", -1), ("j", 1)):
            bind(key, lambda delta=delta: self.move(delta))
        for key, direction in (("left", -1), ("pageup", -1), ("right", 1), ("pagedown", 1)):
            bind(key, lambda direction=direction: self.move(direction * self.page_size))
        for key, action in (("r", "refresh"), ("n", "namespaces")):
            bind(key, lambda action=action: self.respond(action))
        bind("f", self.toggle_everything)
        bind("s", self.next_sort)
        bind("S", self.reverse_sort)
        bind("g", self.next_resource)
        bind("c", self.clear)
        search_keys = KeyBindings()

        def search_key(key, function):
            search_keys.add(key, filter=enabled, eager=True)(lambda event: function())

        # Typing filters; arrows move the list underneath, so a match opens without leaving.
        search_key("up", lambda: self.move(-1))
        search_key("down", lambda: self.move(1))
        search_key("enter", self.choose)
        for key in ("escape", "tab"):
            search_key(key, lambda: get_app().layout.focus(self))
        self.search.control.key_bindings = search_keys

    def toggle_everything(self):
        self.everything = not self.everything
        self.refilter(self.current())

    def sort_by(self, index: int):
        """Cycle a column through ascending, descending and the default order."""
        kind = self.level.kind
        order = self.order[kind]
        if order is None or order[0] != index:
            self.order[kind] = (index, False)
        elif not order[1]:
            self.order[kind] = (index, True)
        else:
            self.order[kind] = None
        self.refilter()

    def next_sort(self):
        kind = self.level.kind
        order = self.order[kind]
        index = 0 if order is None else order[0] + 1
        self.order[kind] = (index, False) if index < len(self.columns(kind)) else None
        self.refilter()

    def reverse_sort(self):
        kind = self.level.kind
        if (order := self.order[kind]) is not None:
            self.order[kind] = (order[0], not order[1])
            self.refilter()

    def next_resource(self):
        values = self.snapshot.resources
        if values:
            index = values.index(self.resource) + 1 if self.resource in values else 0
            self.resource = values[index % len(values)]
            self.refilter(self.current())

    def is_focusable(self):
        return self.enabled()

    def get_key_bindings(self):
        return self.keys

    def mouse_handler(self, event):
        if not self.enabled() or self.help:
            return None
        if event.event_type in {MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN}:
            self.move(-3 if event.event_type == MouseEventType.SCROLL_UP else 3)
        elif event.event_type == MouseEventType.MOUSE_DOWN:
            get_app().layout.focus(self)
            row = event.position.y - 1  # line 0 holds the column headings
            if row < 0:
                x = event.position.x
                index = next((i for i, (a, b) in enumerate(self.spans) if a <= x < b), None)
                if index is not None and self.visible:
                    self.sort_by(index)
            else:
                start, page = self.page()
                if row < len(page):  # the page indicator below the rows is not a row
                    if start + row == self.selected:
                        self.open()
                    else:
                        self.selected = start + row
        return None
