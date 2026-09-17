"""Consistent, non-recording terminal presentation for human-readable output."""

import sys
from collections.abc import Sequence

from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from lab.notebook_status import pod_rows, state_label, status_description

THEME = Theme(
    {"accent": "cyan bold", "muted": "dim", "label": "dim bold", "good": "green", "bad": "red"}
)


def console(*, stderr: bool = False) -> Console:
    """Honor terminal colors and redirection without retaining rendered content."""
    return Console(stderr=stderr, theme=THEME, record=False, markup=False, highlight=False)


def heading(
    title: str, description: str = "", step: str | None = None, *, stderr: bool = False
) -> None:
    screen = console(stderr=stderr)
    screen.print()
    brand = Text.assemble(("◆ lab", "accent"), ("  /  ", "muted"), (title, "bold"))
    if step:
        brand.append(f"    {step}", style="muted")
    screen.print(brand)
    if description:
        screen.print(Padding(Text(description, style="muted"), (0, 0, 0, 2)))
    screen.print()


def note(message: str, *, stderr: bool = False) -> None:
    console(stderr=stderr).print(Text(message, style="muted"))


def question(title: str, description: str = "") -> None:
    screen = console()
    screen.print()
    screen.print(Text.assemble(("  ◇ ", "accent"), (title, "bold")))
    if description:
        screen.print(Padding(Text(description, style="muted"), (0, 0, 0, 4)))


def details(rows: Sequence[tuple[str, str]], title: str | None = None, *, stderr=False) -> None:
    table = Table.grid(padding=(0, 3))
    table.add_column(style="muted", no_wrap=True)
    table.add_column(overflow="fold")
    for name, value in rows:
        table.add_row(Text(name), Text(value))
    screen = console(stderr=stderr)
    if title:
        screen.print(Text(title, style="bold"))
    screen.print(Padding(table, (0, 0, 1, 2)))


def item_guide(
    *, purpose: str, description: str, title: str, category: str, field: str, notes: str = ""
) -> None:
    screen = console()
    screen.print(
        Panel(
            Text(description),
            title=Text(purpose, style="accent"),
            title_align="left",
            box=box.ROUNDED,
            border_style="dim",
            padding=(1, 2),
            width=min(screen.width, 88),
        )
    )
    details(
        [
            ("In 1Password", "Create a new item in the vault your Service Account can read"),
            ("Category", category),
            ("Title", title),
            ("Field", field),
        ]
    )
    if notes:
        screen.print(Text("Optional item notes", style="label"))
        screen.print(Text(notes, style="muted"))


def code_block(value: str, language: str = "yaml") -> None:
    console().print(
        Syntax(
            value,
            language,
            theme="ansi_dark",
            background_color="default",
            word_wrap=False,
            line_numbers=False,
        )
    )


def secret_value(value: str) -> None:
    """Print an exact copyable value without Rich recording, markup or truncation."""
    console().print(Text("Copy this value into the password field", style="accent"))
    sys.stdout.write(value + "\n")
    sys.stdout.flush()


def success(message: str) -> None:
    console().print(Text.assemble(("✓ ", "good"), (message, "bold")))


def error(message: str) -> None:
    console(stderr=True).print(Text.assemble(("✕ ", "bad"), (message, "")))


def result(operation: str, status: str, target: dict | None, data) -> None:
    screen = console()
    screen.print(
        Text.assemble(("✓ ", "good"), (operation.title(), "bold"), (f"  {status}", "muted"))
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
                Text(item["name"]),
                state_label(item["state"]),
                Text(item.get("uid", "")),
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


def session_header(cluster_id: str, editor_only: bool = False) -> None:
    heading(cluster_id, "One authorized session. Remote Notebooks keep running when you leave.")
    rows = [("Session", "status · reconnect · disconnect")]
    if not editor_only:
        rows = [
            ("Inspect", "list · notebook status"),
            ("Work", "create · shell · open"),
            ("Manage", "start · stop · delete · retry · save-preset"),
            *rows,
        ]
    details(rows)
    note("Type a command, or press Tab to browse. Add --help to see its options.")
