"""GUI-assisted, read-only initialization of the single 1Password root reference."""

import base64
import os
import sys
from pathlib import Path

import yaml
from cryptography.fernet import Fernet

from lab.config import (
    nonempty,
    parse_index,
    secret_reference,
)
from lab.display import release_display
from lab.errors import LabError
from lab.guidance import SETUP
from lab.history import Repository
from lab.inputs import terminal_required
from lab.secrets import Secrets
from lab.storage import Paths, read_bootstrap, write_bootstrap
from lab.ui import Field, Screens

KEY_PURPOSE = (
    "A dedicated random key encrypts local history and presets. It is separate from your "
    "1Password account password and Service Account Token. Keep the same key to recover "
    "existing history and presets."
)
GUI_HELP = "lab only reads 1Password. Create and edit items in the 1Password app."


async def select_reference(
    secrets: Secrets, purpose: str, field: str, screens: Screens | None = None
) -> str:
    """Browse stable vault/item IDs or accept a complete secret reference."""
    screens = screens or Screens(context="lab / setup")
    while True:
        mode = await screens.menu(
            purpose,
            "Choose an item accessible to your Service Account. Its title can be anything.",
            [
                (
                    "Find the saved item",
                    [
                        ("select", "Browse vaults and items"),
                        ("paste", "Paste a secret reference"),
                    ],
                )
            ],
        )
        try:
            if mode == "paste":
                values = await screens.form(
                    purpose,
                    [Field("reference", "Secret reference", help=SETUP["reference"])],
                    description="Paste the complete op://vault/item/field reference.",
                )
                try:
                    return secret_reference(values["reference"].strip())
                except ValueError:
                    await screens.details(
                        "Check the reference",
                        "Enter an op:// reference with vault, item and field components.",
                        [],
                        [("retry", "Try again")],
                    )
                    continue
            vaults = await screens.work(secrets.vaults(), "Read available vaults")
            if not vaults:
                await screens.details(
                    "No vaults available",
                    "Check the Service Account's vault access.",
                    [],
                    [("back", "Back")],
                )
                continue
            while True:
                vault_id = await screens.choose(
                    "Vault",
                    [(value.id, value.title) for value in vaults],
                    description="Select the vault where you saved the item.",
                )
                try:
                    items = await screens.work(secrets.items(vault_id), "Read saved items")
                    if not items:
                        await screens.details(
                            "No items available",
                            "Save the item in this vault, then try again.",
                            [],
                            [("back", "Back to vaults")],
                        )
                        continue
                    item_id = await screens.choose(
                        purpose,
                        [(value.id, value.title) for value in items],
                        description="Titles help you recognize items; references use stable IDs.",
                    )
                    return f"op://{vault_id}/{item_id}/{field}"
                except LabError as error:
                    if error.code != 130:
                        raise
        except LabError as error:
            if error.code != 130:
                raise


def data_key(value: str) -> bytes:
    try:
        key = value.encode("ascii")
        if len(base64.b64decode(key, altchars=b"-_", validate=True)) != 32:
            raise ValueError
        Fernet(key)
        return key
    except (UnicodeError, ValueError):
        raise LabError(
            "The data key must encode exactly 32 random bytes as URL-safe base64.", 4
        ) from None


def clear_terminal() -> None:
    try:
        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()
    except OSError:
        raise LabError(
            "Terminal clearing failed. Clear this terminal manually before continuing.", 8
        ) from None


def _has_local_state(paths: Paths) -> bool:
    return any(
        candidate.exists()
        for path in (paths.history_path, paths.presets_path)
        for candidate in (path, path.with_name(path.name + ".bak"))
    )


async def _new_index(secrets: Secrets, paths: Paths, screens: Screens):
    mode = await screens.menu(
        "Local data encryption key",
        KEY_PURPOSE,
        [
            (
                "Step 1 of 2 · Password item",
                [
                    ("existing", "Use a saved local data encryption key"),
                    ("generate", "Generate a random key to save in 1Password"),
                ],
            )
        ],
        help_text=GUI_HELP,
    )
    expected_key = None
    if mode == "generate":
        if read_bootstrap(paths) or _has_local_state(paths):
            raise LabError("Existing local data requires its original encryption key.", 4)
        if not os.environ.get("TERM", "").startswith(("xterm", "screen", "tmux", "ghostty")):
            raise LabError(
                "Terminal scrollback clearing is unverified here. Use an existing key field.", 9
            )
        values = await screens.form(
            "Create the encryption key item",
            [Field("title", "Item title", "lab · Local data encryption key", help=SETUP["title"])],
            description=KEY_PURPOSE,
            submit_label="Review key instructions",
        )
        await screens.details(
            "Save a dedicated encryption key",
            "Create this Password item in a vault your Service Account can read. "
            "Next, reveal the complete key and copy it into the Password field.",
            [
                ("Title", values["title"]),
                ("Category", "Password"),
                ("Field", "password (Password)"),
                ("Notes", KEY_PURPOSE),
            ],
            [("reveal", "Reveal the key to copy")],
            help_text="The terminal is cleared afterward; terminal recording may retain a copy.",
        )
        expected_key = Fernet.generate_key()
        try:
            await screens.details(
                "Copy the entire encryption key",
                "Copy this complete value into the Password field and save the item in "
                "1Password. lab reads the Password field back to verify an exact match.",
                [("", expected_key.decode())],
                [("saved", "I saved the Password item")],
                copy_text=expected_key.decode(),
                help_text="Use terminal text selection (Shift-drag if needed) to copy the key.",
            )
        finally:
            await release_display()
            clear_terminal()
    key_ref = await select_reference(secrets, "Select the saved data key", "password", screens)
    saved_key = data_key(await screens.work(secrets.read(key_ref), "Verify the saved data key"))
    if expected_key is not None and saved_key != expected_key:
        raise LabError("The saved Password field does not match the generated key.", 4)
    root = {
        "schema_version": 1,
        "data_key_ref": key_ref,
        "session": {"max_age_seconds": 604800},
        "clusters": [],
    }
    root_text = yaml.safe_dump(root, sort_keys=False)
    parse_index(root_text)
    values = await screens.form(
        "Connection index · Step 2 of 2",
        [Field("title", "Item title", "lab · Connection index", help=SETUP["title"])],
        description="This empty index references the verified encryption key. "
        "Register clusters independently with lab cluster add.",
        submit_label="View the complete index",
    )
    await screens.details(
        "Save the connection index Secure Note",
        "Create a Secure Note in a permitted vault. Copy this complete YAML into Notes "
        "and save the item. Titles can change later; references determine identity.",
        [
            ("Title", values["title"]),
            ("Category", "Secure Note"),
            ("Field", "notesPlain (Notes)"),
            ("", root_text),
        ],
        [("saved", "I saved the index item")],
        copy_text=root_text,
        help_text=GUI_HELP,
    )
    return saved_key, parse_index(root_text)


async def _verify_index(secrets, paths, screens, reference, expected):
    index = parse_index(await screens.work(secrets.read(reference), "Verify the root index"))
    key = data_key(await screens.work(secrets.read(index.data_key_ref), "Verify the data key"))
    if expected is not None and (key, index) != expected:
        raise LabError("The saved index does not match the complete index just prepared.", 4)
    Repository(paths, key).verify()
    reread = parse_index(await screens.work(secrets.read(reference), "Recheck the root index"))
    final_key = data_key(
        await screens.work(secrets.read(reread.data_key_ref), "Recheck the data key")
    )
    if reread != index or final_key != key:
        raise LabError("The index or data key changed during setup. Verify them again.", 4)
    Repository(paths, final_key).verify()


async def initialize(secrets: Secrets, paths: Paths, screens: Screens | None = None) -> dict:
    """Verify the root index and encryption key, then save device configuration."""
    terminal_required()
    screens = screens or Screens(context="lab / setup")
    existing = read_bootstrap(paths)
    choices = [("existing", "Connect an existing index or recover this device")]
    if not existing and not _has_local_state(paths):
        choices.append(("new", "Set up two 1Password items with a guide"))
    mode = await screens.menu("Set up lab", KEY_PURPOSE, [("Connect", choices)], help_text=GUI_HELP)
    expected = await _new_index(secrets, paths, screens) if mode == "new" else None
    reference = await select_reference(secrets, "Select the root index", "notesPlain", screens)
    await _verify_index(secrets, paths, screens, reference, expected)
    values = await screens.form(
        "Local profiles directory",
        [
            Field(
                "profiles_dir",
                "Directory path",
                existing["profiles_dir"] if existing else "",
                placeholder="/path/to/profiles",
            )
        ],
        description="Type an existing directory path. Profile files can be added after setup.",
    )
    try:
        directory = Path(nonempty(values["profiles_dir"])).expanduser().resolve(strict=True)
        if not directory.is_dir() or not os.access(directory, os.R_OK | os.X_OK):
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise LabError("Select an existing readable profiles directory.", 4) from None
    await screens.details(
        "Confirm device configuration",
        "Save this index reference and absolute profiles directory on this device.",
        [("Index", reference), ("Profiles directory", str(directory))],
        [("save", "Save device configuration")],
    )
    write_bootstrap(paths, reference, directory)
    await release_display()
    clear_terminal()
    return {"schema_version": 2, "index_ref": reference, "profiles_dir": str(directory)}
