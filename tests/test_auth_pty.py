"""Two real controlling terminals exercise hidden handoff and subsequent shell interaction."""

import contextlib
import os
import pty
import re
import select
import signal
import sys
import time
from pathlib import Path

import pytest

MARKER = "synthetic-pty-auth-token-NOT-A-CREDENTIAL"


class Terminal:
    def __init__(self, program):
        self.output = bytearray()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            environment = {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            os.execve(sys.executable, [sys.executable, "-u", "-c", program], environment)
        os.set_blocking(self.fd, False)

    def until(self, marker):
        deadline = time.monotonic() + 10
        while marker.encode() not in self.output:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                pytest.fail("Synthetic terminal did not reach its expected state")
            try:
                data = os.read(self.fd, 8192)
            except BlockingIOError:
                continue
            except OSError:
                pytest.fail("Synthetic terminal became unavailable before its expected state")
            if not data:
                pytest.fail("Synthetic terminal closed unexpectedly")
            self.output.extend(data)
        return self.output.decode(errors="replace")

    def send(self, value):
        raw = value.encode()
        deadline = time.monotonic() + 10
        while raw:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                pytest.fail("Synthetic terminal input timed out")
            try:
                raw = raw[os.write(self.fd, raw) :]
            except BlockingIOError:
                continue

    def close(self):
        os.close(self.fd)
        with contextlib.suppress(ChildProcessError):
            if not os.waitpid(self.pid, os.WNOHANG)[0]:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(self.pid, signal.SIGKILL)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if os.waitpid(self.pid, os.WNOHANG)[0]:
                        return
                    time.sleep(0.02)
                pytest.fail("Owned synthetic terminal child did not exit within cleanup deadline")


def test_hidden_handoff_preserves_a_real_shell_terminal():
    requester = Terminal(
        "import asyncio, subprocess\nfrom lab.auth import request_token\n"
        "token=asyncio.run(request_token({'command':'notebook','action':'shell',"
        "'cluster':'fixture','namespace':'research','name':'fixture'}, lifetime=30))\n"
        f"assert token == {MARKER!r}\ntoken=''\n"
        "print('SHELL_READY', flush=True)\n"
        "with open('/dev/tty','r+b', buffering=0) as tty:\n"
        " subprocess.run(['/bin/sh'], stdin=tty, stdout=tty, stderr=tty, check=True)\n"
        "print('SHELL_FINISHED', flush=True)\n"
    )
    sender = None
    try:
        output = requester.until("request.json")
        match = re.search(r"(/[^\s']+/request\.json)", output)
        assert match
        path = Path(match.group(1))
        assert MARKER not in path.read_text()
        sender = Terminal(
            "import sys\nfrom lab.cli import main\n"
            f"sys.argv=['lab','auth','--request',{str(path)!r}]\nmain()\n"
            "print('SENDER_FINISHED',flush=True)\n"
        )
        sender.until("(hidden):")
        sender.send(MARKER + "\n")
        sender.until("SENDER_FINISHED")
        requester.until("SHELL_READY")
        requester.send("printf 'PTY_%s\\n' WORKS\nexit\n")
        requester.until("PTY_WORKS")
        requester.until("SHELL_FINISHED")
        assert MARKER.encode() not in requester.output + sender.output
        assert not path.parent.exists()
    finally:
        if sender is not None:
            sender.close()
        requester.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_hidden_input_timeout_or_cancel_flushes_and_restores_terminal(cancel):
    terminal = Terminal(
        "import os,termios,time\nfrom lab.auth import hidden_token\n"
        "from lab.errors import LabError\n"
        "fd=os.open('/dev/tty',os.O_RDWR)\nbefore=termios.tcgetattr(fd)\n"
        "try:\n hidden_token(time.monotonic()+0.6)\n"
        "except (LabError,KeyboardInterrupt):\n pass\n"
        "assert termios.tcgetattr(fd)==before\n"
        "print('RESTORED',flush=True)\n"
        "assert input()=='next-command'\nprint('QUEUE_CLEAN',flush=True)\n"
    )
    try:
        terminal.until("(hidden):")
        terminal.send(MARKER)
        if cancel:
            terminal.send("\x03")
        terminal.until("RESTORED")
        terminal.send("next-command\n")
        terminal.until("QUEUE_CLEAN")
        assert MARKER.encode() not in terminal.output
    finally:
        terminal.close()


def test_auth_cli_ctrl_c_never_completes_handoff():
    requester = Terminal(
        "import asyncio\nfrom lab.auth import request_token\nfrom lab.errors import LabError\n"
        "try:\n asyncio.run(request_token({'command':'notebook','action':'list',"
        "'cluster':'fixture','namespace':'research'},lifetime=10))\n"
        "except LabError:\n print('REQUEST_CLOSED_WITHOUT_TOKEN',flush=True)\n"
        "else:\n raise AssertionError('Unexpected token delivery')\n"
    )
    sender = None
    try:
        output = requester.until("request.json")
        path = re.search(r"(/[^\s']+/request\.json)", output).group(1)
        sender = Terminal(
            "import os,sys,termios\nfrom lab.cli import main\n"
            "fd=os.open('/dev/tty',os.O_RDWR)\nbefore=termios.tcgetattr(fd)\n"
            f"sys.argv=['lab','auth','--request',{path!r}]\n"
            "try:\n main()\nexcept SystemExit:\n pass\n"
            "assert termios.tcgetattr(fd)==before\nprint('CLI_CANCELLED',flush=True)\n"
            "assert input()=='next-command'\nprint('INPUT_FLUSHED',flush=True)\n"
        )
        sender.until("(hidden):")
        sender.send(MARKER)
        sender.send("\x03")
        sender.until("CLI_CANCELLED")
        sender.send("next-command\n")
        sender.until("INPUT_FLUSHED")
        requester.until("REQUEST_CLOSED_WITHOUT_TOKEN")
        assert MARKER.encode() not in requester.output + sender.output
        assert not Path(path).exists()
    finally:
        if sender is not None:
            sender.close()
        requester.close()


@pytest.mark.parametrize("length", [2000, 16380])
def test_auth_cli_accepts_long_hidden_tokens_without_terminal_overflow(length):
    marker = "SYNTHETIC-" + "x" * (length - 10) + "Z"
    requester = Terminal(
        "import asyncio\nfrom lab.auth import request_token\n"
        "token=asyncio.run(request_token({'command':'notebook','action':'list',"
        "'cluster':'fixture','namespace':'research'},lifetime=30))\n"
        f"assert token=={marker!r}\nprint('LONG_TOKEN_RECEIVED',flush=True)\n"
    )
    sender = None
    try:
        output = requester.until("request.json")
        path = re.search(r"(/[^\s']+/request\.json)", output).group(1)
        sender = Terminal(
            "import sys\nfrom lab.cli import main\n"
            f"sys.argv=['lab','auth','--request',{path!r}]\nmain()\n"
            "print('LONG_SENDER_FINISHED',flush=True)\n"
        )
        sender.until("(hidden):")
        sender.send(marker + "\n")
        sender.until("LONG_SENDER_FINISHED")
        requester.until("LONG_TOKEN_RECEIVED")
        assert b"SYNTHETIC-" not in sender.output + requester.output
        assert b"\x07" not in sender.output
    finally:
        if sender is not None:
            sender.close()
        requester.close()


def test_hidden_input_supports_backspace_and_line_clear():
    terminal = Terminal(
        "import time\nfrom lab.auth import hidden_token\n"
        "assert hidden_token(time.monotonic()+5)=='synthetic-final'\n"
        "print('EDITING_ACCEPTED',flush=True)\n"
    )
    try:
        terminal.until("(hidden):")
        terminal.send("discard-this\x15synthetic-finaX\x7fl\n")
        terminal.until("EDITING_ACCEPTED")
        assert b"discard-this" not in terminal.output
        assert b"synthetic-final" not in terminal.output
    finally:
        terminal.close()


def test_oversized_paste_is_drained_without_echo():
    terminal = Terminal(
        "import time\nfrom lab.auth import hidden_token\nfrom lab.errors import LabError\n"
        "try:\n hidden_token(time.monotonic()+5)\n"
        "except LabError:\n print('OVERSIZE_REFUSED',flush=True)\n"
        "else:\n raise AssertionError('Accepted oversized token')\n"
    )
    try:
        terminal.until("(hidden):")
        terminal.send("SYNTHETIC-" * 2000 + "\n")
        terminal.until("OVERSIZE_REFUSED")
        assert b"SYNTHETIC-" not in terminal.output
    finally:
        terminal.close()
