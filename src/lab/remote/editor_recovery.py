"""Retain one supported process instance and accept at most one normal stop request."""

import contextlib
import json
import os
import re
import select
import signal
import stat
import sys
import time

LIMIT = 4096


class Refused(Exception):
    pass


class Unsupported(Exception):
    pass


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused
        result[key] = value
    return result


def frame_read(deadline):
    raw = bytearray()
    while len(raw) < LIMIT:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
            raise TimeoutError
        value = os.read(0, 1)
        if not value:
            raise EOFError
        raw.extend(value)
        if value == b"\n":
            result = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(result, dict):
                raise Refused
            return result
    raise Refused


def frame_write(value, deadline):
    raw = json.dumps(value, separators=(",", ":")).encode() + b"\n"
    if len(raw) > LIMIT:
        raise Refused
    os.set_blocking(1, False)
    while raw:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [1], [], remaining)[1]:
            raise TimeoutError
        try:
            raw = raw[os.write(1, raw) :]
        except BlockingIOError:
            continue


def bounded(path, limit=LIMIT):
    with open(path, "rb", buffering=0) as source:
        raw = source.read(limit + 1)
    if not raw or len(raw) > limit:
        raise Refused
    return raw


def prefix(path, count):
    # Read one byte at a time: server arguments after the entry may contain secrets.
    with open(path, "rb", buffering=0) as source:
        items = []
        for _ in range(count):
            value = bytearray()
            while len(value) < LIMIT:
                byte = source.read(1)
                if byte == b"\0":
                    items.append(value.decode("utf-8"))
                    break
                if not byte:
                    raise Refused
                value.extend(byte)
            else:
                raise Refused
    return items


def regular(path):
    current = "/"
    for part in path.strip("/").split("/"):
        current = os.path.join(current, part)
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise Refused
    if not stat.S_ISREG(info.st_mode):
        raise Refused


def namespaces(pid):
    return tuple(os.readlink(f"/proc/{pid}/ns/{kind}") for kind in ("pid", "mnt"))


def process_state(pid):
    raw = bounded(f"/proc/{pid}/stat").decode()
    end = raw.rfind(")")
    values = raw[end + 2 :].split()
    if end < 0 or len(values) < 20 or values[0] in {"Z", "X", "x"}:
        raise Refused
    return int(values[19])


def exited(fd):
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(0))


def identity(pid, profile):
    root = "/root/.vscode-server/bin/" + profile["commit"]
    executable, entry = root + "/node", root + "/out/server-main.js"
    if pid <= 1 or os.stat(f"/proc/{pid}").st_uid != 0:
        raise Refused
    if os.readlink(f"/proc/{pid}/exe") != executable:
        raise Refused
    regular(executable)
    regular(entry)
    expected = [executable, *profile["node_options"], entry]
    if prefix(f"/proc/{pid}/cmdline", len(expected)) != expected:
        raise Refused
    if namespaces(pid) != namespaces("self"):
        raise Refused
    if bounded(f"/proc/{pid}/cgroup") != bounded("/proc/self/cgroup"):
        raise Refused
    return process_state(pid)


def capabilities(profile):
    if (
        sys.version_info < (3, 9)
        or not hasattr(os, "pidfd_open")
        or not hasattr(signal, "pidfd_send_signal")
    ):
        raise Unsupported
    if sys.platform != "linux" or os.uname().machine != profile["machine"]:
        raise Unsupported
    try:
        descriptor = os.pidfd_open(os.getpid(), 0)
    except OSError:
        raise Unsupported from None
    try:
        signal.pidfd_send_signal(descriptor, 0, None, 0)
        exited(descriptor)
    except OSError:
        raise Unsupported from None
    finally:
        os.close(descriptor)
    if os.getuid() != 0 or namespaces(1) != namespaces("self"):
        raise Refused
    if bounded("/proc/1/cgroup") != bounded("/proc/self/cgroup"):
        raise Refused
    if prefix("/proc/1/cmdline", 2) != ["sleep", "infinity"]:
        raise Refused
    if os.readlink("/proc/1/exe") != profile["sleep_executable"]:
        raise Refused
    regular(profile["sleep_executable"])


def discover(profile, deadline):
    selected = []
    try:
        count = 0
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                count += 1
                if count > 32768 or time.monotonic() >= deadline:
                    raise Refused
                pid = int(entry.name)
                if pid <= 1:
                    continue
                try:
                    executable = os.readlink(f"/proc/{pid}/exe")
                except FileNotFoundError:
                    continue
                expected = "/root/.vscode-server/bin/" + profile["commit"] + "/node"
                if executable != expected:
                    continue
                fd = getattr(os, "pidfd_open")(pid, 0)  # noqa: B009 - optional Linux API
                try:
                    # Other Node roles in this installation are not server candidates.
                    argv = prefix(f"/proc/{pid}/cmdline", len(profile["node_options"]) + 2)
                    if argv[1:-1] != profile["node_options"] or argv[-1].startswith("-"):
                        raise Refused
                    if argv != [
                        expected,
                        *profile["node_options"],
                        expected.removesuffix("node") + "out/server-main.js",
                    ]:
                        continue
                    start = identity(pid, profile)
                    if exited(fd):
                        continue
                    selected.append((fd, pid, start))
                    fd = -1
                finally:
                    if fd != -1:
                        os.close(fd)
        if len(selected) > 1:
            raise Refused
        result = selected.pop() if selected else None
        return result
    finally:
        for fd, _, _ in selected:
            os.close(fd)


def stop(fd, pid, start, profile, timeout=15):
    """Signal one held process handle, then report only that instance's exit."""
    if exited(fd):
        return "AlreadyExited"
    if identity(pid, profile) != start or exited(fd):
        if exited(fd):
            return "AlreadyExited"
        raise Refused
    try:
        getattr(signal, "pidfd_send_signal")(fd, signal.SIGTERM, None, 0)  # noqa: B009
    except ProcessLookupError:
        return "AlreadyExited"
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return "OriginalProcessExited" if poller.poll(int(min(timeout, 15) * 1000)) else "StopTimedOut"


def main():
    fd = None
    operation = None
    committed = False
    try:
        initial = frame_read(time.monotonic() + 10)
        if (
            set(initial) != {"version", "operation", "profile", "seconds"}
            or type(initial["version"]) is not int
            or initial["version"] != 1
        ):
            raise Refused
        operation = initial["operation"]
        if not isinstance(operation, str) or not re.fullmatch("[0-9a-f]{32}", operation):
            raise Refused
        seconds = initial["seconds"]
        if type(seconds) not in {int, float} or not 0 < seconds <= 120:
            raise Refused
        deadline = time.monotonic() + seconds
        profile = initial["profile"]
        if (
            not isinstance(profile, dict)
            or set(profile) != {"commit", "machine", "sleep_executable", "node_options"}
            or not re.fullmatch("[0-9a-f]{40}", profile["commit"])
            or not isinstance(profile["node_options"], list)
            or any(
                not isinstance(option, str) or not option.startswith("--")
                for option in profile["node_options"]
            )
        ):
            raise Refused
        capabilities(profile)
        selected = discover(profile, deadline)
        if selected is None:
            outcome = "NoServer"
        else:
            fd, pid, start = selected
            frame_write(
                {
                    "version": 1,
                    "operation": operation,
                    "status": "AwaitingConfirmation",
                    "pid": pid,
                    "start": start,
                },
                deadline,
            )
            proceed = frame_read(deadline)
            if type(proceed.get("version")) is not int or proceed != {
                "version": 1,
                "operation": operation,
                "action": "proceed",
            }:
                raise Refused
            if time.monotonic() >= deadline:
                raise TimeoutError
            committed = True
            outcome = stop(fd, pid, start, profile)
    except Unsupported:
        outcome = "Unsupported"
    except (TimeoutError, EOFError):
        outcome = "OutcomeUnknown" if committed else "Cancelled"
    except (OSError, ValueError, TypeError, KeyError, Refused):
        outcome = "OutcomeUnknown" if committed else "Refused"
    finally:
        if fd is not None:
            os.close(fd)
    with contextlib.suppress(OSError, TimeoutError):
        frame_write({"version": 1, "operation": operation, "status": outcome}, time.monotonic() + 2)


if __name__ == "__main__":
    main()
