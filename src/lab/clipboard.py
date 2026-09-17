"""Copy explicit user-selected text through the native desktop clipboard utility."""

import asyncio
import os
import shutil
import sys
from contextlib import suppress

from lab.errors import LabError


def command() -> list[str]:
    if sys.platform == "darwin":
        return ["/usr/bin/pbcopy"]
    if sys.platform.startswith("linux"):
        if os.environ.get("WAYLAND_DISPLAY") and (path := shutil.which("wl-copy")):
            return [path, "--type", "text/plain;charset=utf-8"]
        if os.environ.get("DISPLAY"):
            if path := shutil.which("xclip"):
                return [path, "-selection", "clipboard", "-target", "UTF8_STRING"]
            if path := shutil.which("xsel"):
                return [path, "--clipboard", "--input"]
    raise LabError(
        "Clipboard unavailable. Use desktop clipboard tools "
        "(wl-copy on Wayland, xclip or xsel on X11), or select the text manually.",
        9,
    )


async def copy(text: str) -> None:
    """Write exact UTF-8 bytes through stdin, without files or shell arguments."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "LANG", "LC_CTYPE"}
    }
    environment["LC_CTYPE"] = "UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    try:
        process = await asyncio.create_subprocess_exec(
            *command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
        )
    except OSError:
        raise LabError(
            "Could not start the clipboard utility. Select the text manually.", 8
        ) from None
    try:
        async with asyncio.timeout(5):
            await process.communicate(text.encode("utf-8"))
        if process.returncode != 0:
            raise LabError("Clipboard copy failed. Select the text manually or try again.", 8)
    except TimeoutError:
        raise LabError(
            "Clipboard copy timed out. Try again or select the text manually.", 8
        ) from None
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
