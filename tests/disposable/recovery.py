"""Run only inside the disposable image; exercise real pidfds with synthetic servers."""

import importlib.util
import json
import os
import select
import shutil
import subprocess
import sys
from pathlib import Path

COMMIT = "1" * 40
ROOT = Path("/root/.vscode-server/bin") / COMMIT


def read(process):
    if not select.select([process.stdout], [], [], 10)[0]:
        raise AssertionError("Fixture helper response timed out")
    return json.loads(process.stdout.readline())


def main():
    if (
        sys.platform != "linux"
        or os.environ.get("LAB_DISPOSABLE_RECOVERY_FIXTURE") != "1"
        or Path("/proc/1/cmdline").read_bytes() != b"sleep\0infinity\0"
    ):
        raise SystemExit("Run this fixture only in its isolated disposable container.")
    ROOT.mkdir(parents=True)
    (ROOT / "out").mkdir()
    shutil.copyfile(Path(sys.executable).resolve(), ROOT / "node")
    (ROOT / "node").chmod(0o755)
    entry = ROOT / "out/server-main.js"
    entry.write_text(
        "import signal,sys,time\n"
        "if 'ignore' in sys.argv: signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\ntime.sleep(300)\n"
    )
    profile = {
        "commit": COMMIT,
        "machine": os.uname().machine,
        "sleep_executable": os.readlink("/proc/1/exe"),
        "node_options": [],
    }
    helper_source = Path("/fixture/helper.py").read_text()
    spec = importlib.util.spec_from_file_location("fixture_helper", "/fixture/helper.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    children = []

    def server(ignore=False):
        process = subprocess.Popen(
            [str(ROOT / "node"), str(entry), *(["ignore"] if ignore else [])],
            env={**os.environ, "PYTHONHOME": sys.prefix},
            stdout=subprocess.PIPE,
        )
        assert process.stdout.readline() == b"ready\n"
        children.append(process)
        return process

    def start(interpreter=sys.executable):
        process = subprocess.Popen(
            [interpreter, "-I", "-S", "-B", "-c", helper_source],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        children.append(process)
        process.stdin.write(
            json.dumps(
                {"version": 1, "operation": "a" * 32, "profile": profile, "seconds": 10}
            ).encode()
            + b"\n"
        )
        process.stdin.flush()
        return process

    def proceed(process):
        process.stdin.write(
            json.dumps({"version": 1, "operation": "a" * 32, "action": "proceed"}).encode() + b"\n"
        )
        process.stdin.flush()
        return read(process)["status"]

    sentinel = subprocess.Popen(["sleep", "300"])
    children.append(sentinel)
    try:
        original = server()
        process = start()
        ready = read(process)
        assert ready["status"] == "AwaitingConfirmation" and ready["pid"] == original.pid
        assert proceed(process) == "OriginalProcessExited"
        original.wait(3)
        assert sentinel.poll() is None

        original = server()
        process = start()
        assert read(process)["status"] == "AwaitingConfirmation"
        original.terminate()
        original.wait(3)
        replacement = server()
        assert proceed(process) == "AlreadyExited"
        assert replacement.poll() is None
        replacement.terminate()
        replacement.wait(3)

        first, second = server(), server()
        process = start()
        assert read(process)["status"] == "Refused"
        assert first.poll() is None and second.poll() is None
        first.terminate()
        second.terminate()
        first.wait(3)
        second.wait(3)

        ignored = server(ignore=True)
        descriptor = os.pidfd_open(ignored.pid, 0)
        try:
            start_time = helper.identity(ignored.pid, profile)
            assert (
                helper.stop(descriptor, ignored.pid, start_time, profile, timeout=0.1)
                == "StopTimedOut"
            )
            assert ignored.poll() is None and sentinel.poll() is None
        finally:
            os.close(descriptor)
        ignored.kill()
        ignored.wait(3)

        original = server()
        process = start()
        assert read(process)["status"] == "AwaitingConfirmation"
        process.stdin.close()
        assert read(process)["status"] == "Cancelled"
        assert original.poll() is None
        original.terminate()
        original.wait(3)

        # Copy an isolated interpreter tree with missing caches; never alter the source runtime.
        runtime = Path("/fixture/cache-audit")
        (runtime / "bin").mkdir(parents=True)
        shutil.copy2(Path(sys.executable).resolve(), runtime / "bin/python3")
        library = f"python{sys.version_info.major}.{sys.version_info.minor}"
        shutil.copytree(
            Path(sys.base_prefix) / "lib" / library,
            runtime / "lib" / library,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "site-packages"),
        )
        assert not list(runtime.rglob("*.pyc"))
        process = start(str(runtime / "bin/python3"))
        assert read(process)["status"] == "NoServer"
        process.wait(3)
        assert not list(runtime.rglob("*.pyc"))
        assert not list(ROOT.rglob("*.pyc"))
        print(
            "PASS: real pidfd stop, retained exit/replacement, ambiguity, "
            "ignored SIGTERM, EOF, sentinel, and cache checks"
        )
        print(
            "Synthetic process evidence only; "
            "actual VS Code lifecycle qualification remains required."
        )
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(3)


if __name__ == "__main__":
    main()
