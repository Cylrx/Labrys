"""Register and remove connections through verified, read-only 1Password workflows."""

import asyncio
import os
import sys
from pathlib import Path

import yaml

from lab import clock
from lab.config import Index, parse_cluster, parse_cluster_note, parse_index
from lab.connections import connection_source
from lab.display import release_display
from lab.errors import LabError
from lab.guidance import SETUP
from lab.inputs import terminal_required
from lab.kubernetes import Kubernetes
from lab.policy import load_profile, profile_path, unregistered_profiles
from lab.secrets import Secrets
from lab.session import Session, Tools
from lab.setup import GUI_HELP, clear_terminal, select_reference
from lab.tools import Toolchain
from lab.ui import Field, Screens


def _available(index, cluster_id: str) -> None:
    if any(entry.id == cluster_id for entry in index.clusters):
        raise LabError("This cluster name is already registered in the index.", 4)


async def add_cluster(
    secrets: Secrets,
    bootstrap: dict,
    cluster_id: str | None = None,
    screens: Screens | None = None,
    *,
    authorized_at: float | None = None,
) -> None:
    """Bind an existing profile after read-only connection verification.

    :param authorized_at: Original authorization time shared with the calling command.
    """
    terminal_required()
    authorized_at = clock.now() if authorized_at is None else authorized_at
    screens = screens or Screens(context="lab / cluster add")
    directory = Path(bootstrap["profiles_dir"])
    reference = bootstrap["index_ref"]
    if cluster_id is None:
        cluster_id = await select_profile(secrets, reference, directory, screens)
        if cluster_id is None:
            return
    load_profile(directory, cluster_id)
    selected_path = profile_path(directory, cluster_id)
    index = await read_index(secrets, reference, screens)
    _available(index, cluster_id)
    connection_text, cluster_ref = await connection_source(secrets, screens)
    cluster = parse_cluster(connection_text, cluster_id)
    await screens.details(
        "Confirm profile binding",
        "Confirm that this local profile describes the selected cluster connection.",
        [
            ("Cluster", cluster_id),
            ("Profile", str(selected_path)),
            ("Context", cluster.kubernetes.context),
            ("Endpoint", cluster.connection.server),
            (
                "Route",
                f"SSH via {cluster.transport.ssh_target}"
                if cluster.transport.mode == "ssh"
                else "Direct",
            ),
            ("Connection note", cluster_ref or "New Secure Note"),
        ],
        [("confirm", "Confirm binding")],
    )
    try:
        if cluster_ref is None:
            cluster_ref = await save_connection(secrets, screens, cluster_id, connection_text)
        saved = await screens.work(secrets.read(cluster_ref), "Verify saved connection")
        if parse_cluster_note(saved) != parse_cluster_note(connection_text):
            raise LabError("The saved connection must exactly match the prepared contents.", 4)
        tools = Toolchain.load()
        session = Session(
            cluster.connection,
            cluster.transport,
            index.session.max_age_seconds,
            Tools(tools.ssh, tools.python, tools.credential),
            cluster_id=cluster_id,
            authorized_at=authorized_at,
        )
        async with session:
            await screens.work(
                Kubernetes(session).request("GET", "/version"), "Verify cluster access"
            )
        latest = await read_index(secrets, reference, screens)
        _available(latest, cluster_id)
        document = latest.model_dump(mode="json")
        document["clusters"].append({"id": cluster_id, "config_ref": cluster_ref})
        await save_index(secrets, reference, document, screens)
    except BaseException:
        print(
            "Registration did not complete. The connection note may already exist and remain "
            "unbound. Inspect the saved note and index in 1Password; lab has removed nothing.",
            file=sys.stderr,
        )
        raise
    await screens.details(
        "Cluster registered",
        "The profile binding and saved index are verified.",
        [("Cluster", cluster_id)],
        [("done", "Done")],
    )


async def read_index(secrets: Secrets, reference: str, screens: Screens) -> Index:
    return parse_index(await screens.work(secrets.read(reference), "Read the connection index"))


async def save_index(secrets: Secrets, reference: str, document: dict, screens: Screens) -> None:
    """Present the complete proposed index and verify the user's manual save."""
    text = yaml.safe_dump(document, sort_keys=False)
    expected = parse_index(text)
    action = "review"
    while True:
        if action == "review":
            await screens.details(
                "Update the connection index",
                "Replace the existing index Notes with this complete document and save it. "
                "lab only reads 1Password; Copy does not save the index.",
                [("Index", reference), ("", text)],
                [("saved", "I saved the updated index")],
                copy_text=text,
                help_text=GUI_HELP,
            )
        try:
            saved = await read_index(secrets, reference, screens)
        except LabError as error:
            if error.code == 130:
                raise
            differences = [("Read-back failed", str(error))]
        else:
            if saved == expected:
                return
            differences = _index_differences(expected, saved)
        action = await screens.details(
            "Index verification incomplete",
            "The saved index has not been verified against the complete updated document. "
            "Recheck reads 1Password again. Review shows the same expected document.",
            differences,
            [("recheck", "Recheck"), ("review", "Review expected index"), ("cancel", "Cancel")],
        )
        if action == "cancel":
            raise LabError("Cancelled. The saved index was not verified.", 130)


def _index_differences(expected: Index, saved: Index) -> list[tuple[str, str]]:
    wanted = {entry.id: entry.config_ref for entry in expected.clusters}
    current = {entry.id: entry.config_ref for entry in saved.clusters}
    differences = []
    missing = wanted.keys() - current.keys()
    unexpected = current.keys() - wanted.keys()
    changed = {key for key in wanted.keys() & current.keys() if wanted[key] != current[key]}
    if missing:
        differences.append(("Missing cluster IDs", ", ".join(sorted(missing))))
    if unexpected:
        differences.append(("Unexpected cluster IDs", ", ".join(sorted(unexpected))))
    if changed:
        differences.append(("Changed config_ref for cluster IDs", ", ".join(sorted(changed))))
    if saved.data_key_ref != expected.data_key_ref:
        differences.append(("data_key_ref", "Changed"))
    if saved.session != expected.session:
        differences.append(("session.max_age_seconds", "Changed"))
    if not differences and saved.clusters != expected.clusters:
        differences.append(("Cluster order", "Changed"))
    return differences


async def select_profile(
    secrets: Secrets, reference: str, directory: Path, screens: Screens
) -> str | None:
    """Refresh local files and registered names together before presenting choices."""
    while True:
        index = await read_index(secrets, reference, screens)
        try:
            entries = await screens.work(
                asyncio.to_thread(
                    unregistered_profiles, directory, {entry.id for entry in index.clusters}
                ),
                "Checking profiles",
            )
        except LabError as error:
            action = await screens.details(
                "Profiles unavailable",
                str(error),
                [("Directory", str(directory))],
                [("refresh", "Refresh"), ("back", "Back")],
            )
            if action == "back":
                return None
            continue
        choices = {f"profile:{entry.path.name}": entry for entry in entries}
        action = await screens.table(
            "Select profile",
            "Select an available profile. Registered profiles are excluded. "
            "Select an unavailable entry to see why.",
            [(key, entry.label, entry.status) for key, entry in choices.items()],
            [("refresh", "Refresh"), ("back", "Back")],
            name_label="Profile",
            empty_message=(
                "No unregistered profiles found. Prepare a named .yaml profile, then Refresh."
            ),
        )
        if action == "back":
            return None
        if action == "refresh":
            continue
        entry = choices[action]
        if entry.error is not None:
            await screens.details(
                "Profile unavailable",
                "Correct the file, then refresh the profile list.",
                [("File", str(entry.path)), ("Reason", entry.error)],
                [("back", "Back")],
            )
            continue
        return entry.path.stem


async def remove_cluster(
    secrets: Secrets,
    bootstrap: dict,
    cluster_id: str | None = None,
    screens: Screens | None = None,
) -> None:
    """Remove one registration, retaining remote resources, notes and local files."""
    terminal_required()
    screens = screens or Screens(context="lab / cluster remove")
    reference = bootstrap["index_ref"]
    index = await read_index(secrets, reference, screens)
    if cluster_id is None:
        if not index.clusters:
            await screens.details(
                "No registered clusters",
                "There is no connection to remove.",
                [],
                [("back", "Back")],
            )
            return
        cluster_id = await screens.choose(
            "Select cluster to remove",
            [(entry.id, entry.id) for entry in sorted(index.clusters, key=lambda entry: entry.id)],
            description="Only the lab registration will be removed.",
        )
    selected = next((entry for entry in index.clusters if entry.id == cluster_id), None)
    if selected is None:
        raise LabError("Cluster name is not in the selected root index.", 4)
    confirmed = await screens.confirm(
        "Remove cluster registration?",
        "Remove this connection from lab's index. Remote Notebooks, local profiles, "
        "history, presets and the original connection Secure Note are retained. "
        "Existing lab sessions are not revoked.",
        [("Cluster", selected.id), ("Connection note", selected.config_ref)],
    )
    if not confirmed:
        return
    latest = await read_index(secrets, reference, screens)
    current = next((entry for entry in latest.clusters if entry.id == selected.id), None)
    if current != selected:
        raise LabError("The selected registration changed. Run removal again to review it.", 4)
    document = latest.model_dump(mode="json")
    document["clusters"] = [
        entry.model_dump(mode="json") for entry in latest.clusters if entry.id != selected.id
    ]
    try:
        await save_index(secrets, reference, document, screens)
    except BaseException:
        print(
            "Removal was not verified. Check the index in 1Password before retrying. "
            "lab has not deleted any profiles, connection notes or remote resources.",
            file=sys.stderr,
        )
        raise
    await screens.details(
        "Cluster registration removed",
        "The saved index was read back and verified.",
        [("Cluster", selected.id)],
        [("done", "Done")],
    )


async def save_connection(
    secrets: Secrets, screens: Screens, cluster_id: str, connection_text: str
) -> str:
    """Guide an explicit credential reveal and select the user's newly saved Note."""
    if not os.environ.get("TERM", "").startswith(("xterm", "screen", "tmux", "ghostty")):
        raise LabError("Use a terminal that supports clearing before displaying credentials.", 9)
    values = await screens.form(
        "Cluster connection",
        [Field("title", "Item title", f"lab · {cluster_id}", help=SETUP["title"])],
        description="Choose the display title of the Secure Note you will save in 1Password.",
    )
    await screens.details(
        "Save the cluster connection",
        "Create a Secure Note in a permitted vault. Its complete contents include credentials.",
        [("Title", values["title"]), ("Category", "Secure Note"), ("Field", "notesPlain (Notes)")],
        [("reveal", "Reveal the contents to copy")],
        help_text=GUI_HELP,
    )
    try:
        await screens.details(
            "Copy the cluster connection",
            "Copy the entire contents into Notes and save the item in 1Password.",
            [("", connection_text)],
            [("saved", "I saved the cluster item")],
            copy_text=connection_text,
        )
    finally:
        await release_display()
        clear_terminal()
    return await select_reference(secrets, "Select the cluster entry", "notesPlain", screens)
