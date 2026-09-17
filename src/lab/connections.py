"""Read connection sources without executing imported configuration or saving credentials."""

import os
import stat
from pathlib import Path
from textwrap import indent

import yaml

from lab.config import MAX_DOCUMENT_BYTES, nonempty, parse_cluster_note, safe_document
from lab.errors import LabError
from lab.guidance import SETUP
from lab.secrets import Secrets
from lab.setup import select_reference
from lab.ui import Field, Screens


async def connection_source(secrets: Secrets, screens: Screens) -> tuple[str, str | None]:
    """Return connection contents and their existing reference, if already saved."""
    source = await screens.menu(
        "Connection source",
        "Reuse a saved lab connection, paste kubeconfig, or import a file.",
        [
            (
                "Source",
                [
                    ("existing", "Use an existing 1Password connection"),
                    ("paste", "Paste kubeconfig"),
                    ("file", "Import kubeconfig file"),
                ],
            )
        ],
    )
    if source == "existing":
        reference = await select_reference(
            secrets, "Select the saved connection", "notesPlain", screens
        )
        text = await screens.work(secrets.read(reference), "Read the saved connection")
        parse_cluster_note(text)
        return text, reference
    if source == "paste":
        kubeconfig = await screens.paste(
            "Paste kubeconfig",
            "Paste the complete kubeconfig YAML. Its contents are hidden and kept in this "
            "process only. Select Import when finished.",
            max_bytes=MAX_DOCUMENT_BYTES,
        )
    else:
        values = await screens.form(
            "Import kubeconfig",
            [Field("path", "File path", str(Path.home() / ".kube/config"), help=SETUP["path"])],
            description=(
                "Read this file to assemble the cluster item. The source file stays unchanged."
            ),
            submit_label="Import",
        )
        kubeconfig = read_kubeconfig(values["path"])
    return await connection_note(screens, kubeconfig), None


def read_kubeconfig(path: str) -> str:
    """Read only the explicitly selected regular file, with a bounded size."""
    try:
        descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError
            raw = source.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ValueError
        return raw.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        raise LabError("Select a readable UTF-8 kubeconfig file no larger than 1 MiB.", 4) from None


async def connection_note(screens: Screens, kubeconfig: str) -> str:
    """Choose context and route, then assemble a validated connection-only note."""
    document = safe_document(kubeconfig)
    try:
        contexts = [nonempty(item["name"]) for item in document["contexts"]]
        if not contexts or len(set(contexts)) != len(contexts):
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError):
        raise LabError("The kubeconfig must contain unique named contexts.", 4) from None
    current = document.get("current-context")
    contexts.sort(key=lambda name: name != current)
    context = (
        contexts[0]
        if len(contexts) == 1
        else await screens.choose("Select connection", [(name, name) for name in contexts])
    )
    mode = await screens.choose(
        "Connection route",
        [("ssh", "Through an existing SSH host"), ("direct", "Direct network access")],
        description="Use the route that reaches this cluster from this computer.",
    )
    transport = {"mode": mode}
    if mode == "ssh":
        values = await screens.form(
            "SSH connection",
            [Field("target", "SSH host alias", help=SETUP["target"])],
            description="Use a host name already configured for your ssh command.",
        )
        transport["ssh_target"] = values["target"]
    text = (
        yaml.safe_dump({"kubernetes": {"context": context}}, sort_keys=False)
        + "  kubeconfig: |\n"
        + indent(kubeconfig.rstrip("\n"), "    ")
        + "\n"
        + yaml.safe_dump({"transport": transport}, sort_keys=False)
    )
    parse_cluster_note(text)
    return text
