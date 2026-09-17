"""Run the remote helper state machine over actual subprocess pipes with synthetic handles."""

import asyncio
import json
import sys
from importlib.resources import files

import pytest

OPERATION = "1" * 32
PROFILE = {
    "commit": "2" * 40,
    "machine": "fixture",
    "sleep_executable": "/usr/bin/sleep",
    "node_options": [],
}


async def helper():
    source = files("lab.remote").joinpath("editor_recovery.py").read_text()
    program = (
        f"namespace={{'__name__':'fixture'}}\nexec({source!r},namespace)\n"
        "import os\nread_fd,write_fd=os.pipe()\nos.close(write_fd)\n"
        "namespace['capabilities']=lambda profile: None\n"
        "namespace['discover']=lambda profile,deadline:(read_fd,42,99)\n"
        "def stop(*args):\n os.write(2,b'ONE_SEND_ATTEMPT\\n')\n return 'OriginalProcessExited'\n"
        "namespace['stop']=stop\nnamespace['main']()\n"
    )
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-B",
        "-c",
        program,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def initial(process, seconds=1):
    process.stdin.write(
        json.dumps(
            {"version": 1, "operation": OPERATION, "profile": PROFILE, "seconds": seconds}
        ).encode()
        + b"\n"
    )
    await process.stdin.drain()
    ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 2))
    assert ready["status"] == "AwaitingConfirmation"


async def result(process):
    raw = await asyncio.wait_for(process.stdout.readline(), 2)
    await asyncio.wait_for(process.wait(), 2)
    errors = await process.stderr.read()
    return json.loads(raw)["status"], errors


async def test_duplicate_proceed_never_reenters_confirmation():
    process = await helper()
    await initial(process)
    proceed = (
        json.dumps({"version": 1, "operation": OPERATION, "action": "proceed"}).encode() + b"\n"
    )
    process.stdin.write(proceed * 2)
    await process.stdin.drain()
    status, errors = await result(process)
    assert status == "OriginalProcessExited"
    assert errors == b"ONE_SEND_ATTEMPT\n"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"version":1,"operation":"' + OPERATION.encode() + b'","action":"stop"}\n',
        b'{"version":1,"operation":"wrong","action":"proceed"}\n',
        b'{"version":1,"version":1,"operation":"' + OPERATION.encode() + b'","action":"proceed"}\n',
        b'{"version":true,"operation":"' + OPERATION.encode() + b'","action":"proceed"}\n',
        b"{" + b"x" * 5000 + b"\n",
    ],
)
async def test_malformed_or_mismatched_frames_never_signal(payload):
    process = await helper()
    await initial(process)
    process.stdin.write(payload)
    await process.stdin.drain()
    status, errors = await result(process)
    assert status == "Refused"
    assert not errors


@pytest.mark.parametrize("disconnect", [True, False])
async def test_confirmation_has_remote_eof_and_deadline(disconnect):
    process = await helper()
    await initial(process, seconds=0.15)
    if disconnect:
        process.stdin.close()
    status, errors = await result(process)
    assert status == "Cancelled"
    assert not errors
