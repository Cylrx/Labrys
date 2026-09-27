"""Terminate an independently owned SSH process group on parent EOF or session expiry."""

import argparse
import contextlib
import os
import resource
import selectors
import signal
import subprocess

from lab.clock import now


def main() -> int:
    """Run the private forwarding supervisor with no credential input or output."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent-fd", type=int, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--ssh", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--local-port", type=int, required=True)
    args = parser.parse_args()
    host = f"[{args.host}]" if ":" in args.host else args.host
    command = [
        args.ssh,
        "-N",
        "-T",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ControlPersist=no",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ForwardAgent=no",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "Tunnel=no",
        "-L",
        f"127.0.0.1:{args.local_port}:{host}:{args.port}",
        "--",
        args.target,
    ]
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    process: subprocess.Popen[bytes] | None = None
    try:
        os.set_inheritable(args.parent_fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(args.parent_fd, selectors.EVENT_READ)
            if now() >= args.deadline or selector.select(0):
                return 1
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            while not stopping and now() < args.deadline and process.poll() is None:
                if selector.select(min(0.2, max(0, args.deadline - now()))) and not os.read(
                    args.parent_fd, 1
                ):
                    break
    except (OSError, ValueError):
        return 1
    finally:
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=1)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
        os.close(args.parent_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
