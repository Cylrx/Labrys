"""Consistent, non-recording terminal presentation for human-readable output."""

from collections.abc import Sequence

from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from lab.notebook_status import pod_rows, state_label, status_description
from lab.resources import amount

THEME = Theme({"accent": "cyan bold", "muted": "dim", "good": "green", "bad": "red"})
_CONTROL_ESCAPES = {
    code: f"\\x{code:02x}" for code in (*range(32), *range(127, 160)) if chr(code) not in "\n\t"
}


def _text(value: str, style: str = "") -> Text:
    # Escape controls before Rich measures and wraps text; retain layout whitespace.
    return Text(value.translate(_CONTROL_ESCAPES), style=style)


def console(*, stderr: bool = False) -> Console:
    """Honor terminal colors and redirection without retaining rendered content."""
    return Console(stderr=stderr, theme=THEME, record=False, markup=False, highlight=False)


def heading(
    title: str, description: str = "", step: str | None = None, *, stderr: bool = False
) -> None:
    screen = console(stderr=stderr)
    screen.print()
    brand = Text.assemble(("◆ lab", "accent"), ("  /  ", "muted"), _text(title, "bold"))
    if step:
        brand.append(_text(f"    {step}", "muted"))
    screen.print(brand)
    if description:
        screen.print(Padding(_text(description, "muted"), (0, 0, 0, 2)))
    screen.print()


def note(message: str, *, stderr: bool = False) -> None:
    console(stderr=stderr).print(_text(message, "muted"))


def resources(cluster: str, data: dict) -> None:
    """Print one finite table from the same snapshot exported by the JSON command."""
    screen = console()
    scope = data["scope"]
    coverage = (
        "All namespaces"
        if scope["all_namespaces"]
        else ", ".join(scope["namespaces"]) or "No pod data"
    )
    heading("Cluster resources", f"{cluster} · {data['observed_at']} · {coverage}")
    note(
        "Remaining values are upper bounds; quotas and placement rules can further restrict access."
    )
    if data["partial"]:
        note("Partial snapshot: only observed reservations are subtracted.")
    for issue in data["issues"]:
        note(issue)

    def bound(value, key):
        return "?" if value is None else "≤" + amount(value, key)

    def headroom(node, key):
        left = node["remaining_upper_bound"]
        return "—" if left is None or key not in left else bound(left[key], key)

    remaining = data["summary"]["remaining_upper_bound"]
    note(
        f"{data['summary']['nodes']} nodes · {data['summary']['ready_nodes']} Ready · "
        f"CPU {bound(remaining.get('cpu'), 'cpu')} cores · "
        f"RAM {bound(remaining.get('memory'), 'memory')} GiB"
    )
    table = Table(box=box.SIMPLE, show_edge=False, padding=(0, 1))
    table.add_column("Node", style="bold", overflow="fold")
    table.add_column("State")
    table.add_column("CPU cores\nremaining", justify="right")
    table.add_column("RAM GiB\nremaining", justify="right")
    extended = [key for key in data["units"] if key not in {"cpu", "memory"}]
    for key in extended:
        observed = data["summary"]["observed_reserved"]
        reserved = amount(observed.get(key), key) if observed is not None else "?"
        total = amount(data["summary"]["allocatable"].get(key), key)
        note(
            f"{key}: {reserved} / {total} observed reserved · "
            f"{bound(remaining.get(key), key)} remaining"
        )
        table.add_column(
            _text(key + "\nreserved / total · remaining"), justify="right", overflow="fold"
        )
    for node in data["nodes"]:
        cells = [
            node["name"],
            node["state"] + ("*" if node["taints"] else ""),
            headroom(node, "cpu"),
            headroom(node, "memory"),
        ]
        for key in extended:
            if key not in node["allocatable"]:
                cells.append("—")
                continue
            observed = node["observed_reserved"]
            reserved = amount(observed.get(key), key) if observed is not None else "?"
            cells.append(
                f"{reserved} / {amount(node['allocatable'][key], key)} · {headroom(node, key)}"
            )
        table.add_row(*(_text(cell) for cell in cells))
    screen.print(table)
    note(
        "Reserved counts are observed only. * marks tainted nodes. "
        "Use --json for models and per-node warnings."
    )


def details(rows: Sequence[tuple[str, str]], title: str | None = None, *, stderr=False) -> None:
    table = Table.grid(padding=(0, 3))
    table.add_column(style="muted", no_wrap=True)
    table.add_column(overflow="fold")
    for name, value in rows:
        table.add_row(_text(name), _text(value))
    screen = console(stderr=stderr)
    if title:
        screen.print(_text(title, "bold"))
    screen.print(Padding(table, (0, 0, 1, 2)))


def success(message: str) -> None:
    console().print(Text.assemble(("✓ ", "good"), _text(message, "bold")))


def error(message: str) -> None:
    console(stderr=True).print(Text.assemble(("✕ ", "bad"), _text(message)))


def result(operation: str, status: str, target: dict | None, data) -> None:
    screen = console()
    screen.print(
        Text.assemble(
            ("✓ ", "good"), _text(operation.title(), "bold"), _text(f"  {status}", "muted")
        )
    )
    if target:
        details([(key.title(), str(value)) for key, value in target.items() if value is not None])
    if isinstance(data, list):
        if not data:
            note("No Notebooks found in this namespace.")
            return
        table = Table(box=box.SIMPLE, show_edge=False, padding=(0, 2))
        table.add_column("Name", style="bold")
        table.add_column("Status")
        table.add_column("UID", style="muted", overflow="fold")
        for item in data:
            table.add_row(
                _text(item["name"]),
                _text(state_label(item["state"])),
                _text(item.get("uid", "")),
            )
        screen.print(table)
    elif isinstance(data, dict) and {"stopped", "pods", "state"} <= data.keys():
        details(
            [
                ("UID", data["uid"]),
                ("State", data["state"]),
                *pod_rows(data["pods"]),
                ("Connection", status_description(data["state"])),
            ]
        )
    elif isinstance(data, dict):
        rows: list[tuple[str, str]] = []
        for key, value in data.items():
            if key == "pods":
                rows.extend((pod["name"], pod["state"]) for pod in value)
            else:
                rows.append((key.replace("_", " ").title(), str(value)))
        details(rows)


def preview(manifest: dict) -> None:
    spec = manifest["spec"]["template"]["spec"]
    container = spec["containers"][0]
    rows = [
        ("Image", container["image"]),
        (
            "Resources",
            " · ".join(
                f"{key}: {value}" for key, value in container["resources"]["requests"].items()
            ),
        ),
        ("Working directory", container.get("workingDir", "/")),
        (
            "Placement",
            ", ".join(f"{key}={value}" for key, value in spec.get("nodeSelector", {}).items())
            or "Scheduler-selected",
        ),
    ]
    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    for mount in container["volumeMounts"]:
        volume = volumes[mount["name"]]
        source = volume.get("hostPath", {}).get("path")
        if source is None:
            ephemeral = volume["emptyDir"]
            source = (
                f"Temporary {ephemeral.get('medium', 'disk')} · {ephemeral.get('sizeLimit', '')}"
            )
        mode = "read-only" if mount.get("readOnly") else "read/write"
        rows.append(("Storage", f"{source} → {mount['mountPath']} ({mode})"))
    if "command" in container:
        import shlex

        rows.append(("Startup", shlex.join(container["command"] + container.get("args", []))))
    else:
        rows.append(("Startup", "Image default"))
    rows.append(
        (
            "Registry credentials",
            ", ".join(secret["name"] for secret in spec.get("imagePullSecrets", [])) or "Anonymous",
        )
    )
    details(rows, "Notebook preview", stderr=True)
