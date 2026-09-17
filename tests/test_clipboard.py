"""Native clipboard adapters are exercised without changing the user's clipboard."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from lab import clipboard
from lab.errors import LabError


@pytest.mark.parametrize(
    "platform,variables,utilities,expected",
    [
        ("darwin", {}, [], ["/usr/bin/pbcopy"]),
        (
            "linux",
            {"WAYLAND_DISPLAY": "wayland-0"},
            ["wl-copy"],
            ["/tools/wl-copy", "--type", "text/plain;charset=utf-8"],
        ),
        (
            "linux",
            {"DISPLAY": ":0"},
            ["xclip"],
            ["/tools/xclip", "-selection", "clipboard", "-target", "UTF8_STRING"],
        ),
        ("linux", {"DISPLAY": ":0"}, ["xsel"], ["/tools/xsel", "--clipboard", "--input"]),
    ],
)
def test_native_clipboard_selection(monkeypatch, platform, variables, utilities, expected):
    monkeypatch.setattr(clipboard.sys, "platform", platform)
    for name in ("WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        clipboard.shutil, "which", lambda name: "/tools/" + name if name in utilities else None
    )
    assert clipboard.command() == expected


async def test_copy_sends_exact_bytes_only_over_stdin(monkeypatch):
    source = "kubernetes:\n  kubeconfig: |\n    token: synthetic-秘密\n"
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(), kill=Mock(), wait=AsyncMock())
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(clipboard, "command", lambda: ["/synthetic/clipboard"])
    monkeypatch.setattr(clipboard.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "must-not-inherit")
    monkeypatch.setenv("LAB_GRANT_CAPABILITY", "must-not-inherit")
    await clipboard.copy(source)
    process.communicate.assert_awaited_once_with(source.encode("utf-8"))
    assert spawn.call_args.args == ("/synthetic/clipboard",)
    assert "must-not-inherit" not in str(spawn.call_args.kwargs)
    assert spawn.call_args.kwargs["stderr"] == asyncio.subprocess.DEVNULL
    process.kill.assert_not_called()


async def test_failed_utility_is_not_reported_as_copied(monkeypatch):
    process = SimpleNamespace(returncode=1, communicate=AsyncMock())
    monkeypatch.setattr(clipboard, "command", lambda: ["/synthetic/clipboard"])
    monkeypatch.setattr(
        clipboard.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    with pytest.raises(LabError, match="copy failed"):
        await clipboard.copy("synthetic")


async def test_cancelled_utility_is_terminated_and_reaped(monkeypatch):
    started = asyncio.Event()

    async def blocked(data):
        started.set()
        await asyncio.Event().wait()

    process = SimpleNamespace(returncode=None, communicate=blocked, kill=Mock(), wait=AsyncMock())
    monkeypatch.setattr(clipboard, "command", lambda: ["/synthetic/clipboard"])
    monkeypatch.setattr(
        clipboard.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    task = asyncio.create_task(clipboard.copy("synthetic"))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
