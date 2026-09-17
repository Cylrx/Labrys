"""Manual editor stop using one retained remote helper and an explicit release profile."""

import asyncio
import json
import re
import uuid
from dataclasses import asdict, dataclass
from importlib.resources import files

from lab.editor import SUPPORTED_CONTAINERS, SUPPORTED_EDITOR, _output, stop_client
from lab.errors import LabError
from lab.kubernetes import pod_state, resource_path
from lab.notebooks import require_running_request
from lab.tools import client_environment

WARNING = (
    "Restart this container's VS Code Server? All windows using this server will disconnect. "
    "Unsaved work and tasks in VS Code terminals may be interrupted. Save or finish that work "
    "and coordinate with anyone sharing this container before continuing."
)
CODES = {
    "OriginalProcessExited": 0,
    "AlreadyExited": 0,
    "NoServer": 0,
    "InvalidInput": 2,
    "AuthorizationUnavailable": 4,
    "OutcomeUnknown": 5,
    "Refused": 6,
    "StopTimedOut": 7,
    "Unsupported": 9,
    "Cancelled": 130,
}
MESSAGES = {
    "OriginalProcessExited": "Original VS Code Server process exited.",
    "AlreadyExited": "Original process already exited; no signal was needed.",
    "NoServer": "No running supported server found.",
    "InvalidInput": "Invalid recovery selector or option.",
    "AuthorizationUnavailable": "Authorization is unavailable; no stop was attempted.",
    "OutcomeUnknown": (
        "Stop outcome unknown. Inspect the original operation; do not retry automatically."
    ),
    "Refused": "Editor stop refused: target identity or prerequisites could not be established.",
    "StopTimedOut": (
        "Original process exit was not observed within 15 seconds. No escalation was attempted."
    ),
    "Unsupported": "This image, editor build, or helper capability is not release-qualified.",
    "Cancelled": "Cancelled before proceed transmission; no stop was attempted.",
}


@dataclass(frozen=True)
class Profile:
    image_id: str
    machine: str
    editor_arch: str
    commit: str
    sleep_executable: str
    node_options: tuple[str, ...] = ()
    data_mounts: tuple[tuple[str, str], ...] = ()

    def remote(self):
        return {
            key: value
            for key, value in asdict(self).items()
            if key in {"machine", "commit", "sleep_executable", "node_options"}
        }


# Entries require the disposable acceptance evidence described in docs/editor-recovery.rst.
QUALIFIED_PROFILES: tuple[Profile, ...] = ()


@dataclass(frozen=True)
class Target:
    cluster: str
    namespace: str
    notebook: str
    notebook_uid: str
    pod: str
    pod_uid: str
    container: str
    container_id: str
    restart_count: int
    image_id: str


@dataclass
class Result:
    status: str
    target: Target | None = None
    warning: str | None = None
    detail: str | None = None

    def data(self):
        return {
            "outcome": self.status,
            "message": self.detail or MESSAGES[self.status],
            "cleanup_warning": self.warning,
        }


def failure(status, message=None):
    return LabError(message or MESSAGES[status], CODES[status], data={"outcome": status})


def controller(item, uid, name, kind, version):
    references = item.get("metadata", {}).get("ownerReferences", [])
    owners = [reference for reference in references if reference.get("controller") is True]
    return len(owners) == 1 and all(
        owners[0].get(key) == value
        for key, value in {
            "uid": uid,
            "name": name,
            "kind": kind,
            "apiVersion": version,
        }.items()
    )


async def snapshot(service, notebook, pod_name=None, container_name=None):
    metadata = notebook["metadata"]
    namespace, name = metadata["namespace"], metadata["name"]
    fresh = await service.get(namespace, name)
    require_running_request(fresh)
    if fresh["metadata"].get("uid") != metadata.get("uid") or fresh["metadata"].get(
        "deletionTimestamp"
    ):
        raise failure("Refused")
    parents = await service.api.collection(
        resource_path(namespace, "statefulsets"), fieldSelector=f"metadata.name={name}"
    )
    parents = [
        parent
        for parent in parents
        if controller(parent, metadata["uid"], name, "Notebook", "kubeflow.org/v1")
        and not parent["metadata"].get("deletionTimestamp")
    ]
    if len(parents) != 1:
        raise failure("Refused")
    parent = parents[0]["metadata"]
    pods = await service.api.collection(
        resource_path(namespace, "pods"), labelSelector=f"notebook-name={name}"
    )
    pods = [
        pod
        for pod in pods
        if controller(pod, parent["uid"], parent["name"], "StatefulSet", "apps/v1")
        and pod_state(pod) == "Ready"
        and (not pod_name or pod["metadata"]["name"] == pod_name)
    ]
    if len(pods) != 1:
        raise failure("Refused")
    pod = pods[0]
    specification = pod["spec"]
    containers = [
        item
        for item in specification["containers"]
        if not container_name or item["name"] == container_name
    ]
    if len(containers) != 1:
        raise failure("Refused")
    container = containers[0]
    status = [
        item
        for item in pod.get("status", {}).get("containerStatuses", [])
        if item["name"] == container["name"]
    ]
    if len(status) != 1 or not status[0].get("state", {}).get("running"):
        raise failure("Refused")
    status = status[0]
    if (
        any(
            specification.get(key)
            for key in ("hostPID", "hostIPC", "hostNetwork", "shareProcessNamespace")
        )
        or any(
            item.get("securityContext", {}).get("privileged")
            or item.get("securityContext", {}).get("capabilities", {}).get("add")
            for item in specification["containers"]
        )
        or container.get("livenessProbe")
        or container.get("startupProbe")
        or container.get("command") != ["sleep", "infinity"]
        or container.get("args")
        or not status.get("containerID")
        or not status.get("imageID")
        or type(status.get("restartCount")) is not int
    ):
        raise failure("Refused")
    host_paths = {
        volume["name"]: volume["hostPath"]["path"]
        for volume in specification.get("volumes", [])
        if "hostPath" in volume
    }
    mounts = tuple(
        sorted(
            (host_paths[mount["name"]], mount["mountPath"])
            for item in specification["containers"]
            for mount in item.get("volumeMounts", [])
            if mount["name"] in host_paths
        )
    )
    profiles = [profile for profile in QUALIFIED_PROFILES if profile.image_id == status["imageID"]]
    if (
        mounts
        and profiles
        and not any(tuple(sorted(profile.data_mounts)) == mounts for profile in profiles)
    ):
        raise failure("Refused", "Host data mounts differ from the qualified image profile.")
    for item in specification["containers"]:
        for mount in item.get("volumeMounts", []):
            path = mount["mountPath"].rstrip("/")
            if any(
                path == root or root.startswith(path + "/") or path.startswith(root + "/")
                for root in ("/proc", "/sys", "/dev", "/root/.vscode-server")
            ):
                raise failure("Refused")
    return Target(
        str(service.cluster.cluster_id),
        namespace,
        name,
        metadata["uid"],
        pod["metadata"]["name"],
        pod["metadata"]["uid"],
        container["name"],
        status["containerID"],
        status["restartCount"],
        status["imageID"],
    )


async def qualify(tools, selected):
    profiles = [profile for profile in QUALIFIED_PROFILES if profile.image_id == selected.image_id]
    if not profiles:
        raise failure(
            "Unsupported",
            "No release-qualified recovery profile exists for this image digest. "
            "Disposable lifecycle, pidfd, and actual editor acceptance evidence is required.",
        )
    lines = (await _output(tools.require("code"), "--version")).splitlines()
    if (
        len(lines) != 3
        or lines[0] != SUPPORTED_EDITOR
        or not re.fullmatch("[0-9a-f]{40}", lines[1])
    ):
        raise failure("Unsupported")
    extensions = (
        await _output(tools.require("code"), "--list-extensions", "--show-versions")
    ).splitlines()
    profiles = [
        profile
        for profile in profiles
        if profile.commit == lines[1] and profile.editor_arch == lines[2]
    ]
    if (
        len(profiles) != 1
        or "ms-vscode-remote.remote-containers@" + SUPPORTED_CONTAINERS not in extensions
    ):
        raise failure("Unsupported")
    return profiles[0]


async def read_frame(process, operation, deadline):
    assert process.stdout is not None
    async with asyncio.timeout_at(deadline):
        raw = await process.stdout.readline()
    if len(raw) > 4096:
        raise ValueError

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    frame = json.loads(raw, object_pairs_hook=unique)
    if (
        not isinstance(frame, dict)
        or type(frame.get("version")) is not int
        or frame.get("version") != 1
        or frame.get("operation") != operation
        or frame.get("status") not in {*CODES, "AwaitingConfirmation"}
    ):
        raise ValueError
    if frame["status"] == "AwaitingConfirmation":
        if set(frame) != {"version", "operation", "status", "pid", "start"}:
            raise ValueError
        if type(frame["pid"]) is not int or frame["pid"] <= 1 or type(frame["start"]) is not int:
            raise ValueError
    elif set(frame) != {"version", "operation", "status"}:
        raise ValueError
    return frame


async def drain_errors(process):
    assert process.stderr is not None
    while await process.stderr.read(4096):
        pass


async def restart(
    service,
    notebook,
    tools,
    confirm,
    pod_name=None,
    container_name=None,
    helper_python="/usr/bin/python3",
    *,
    work=None,
) -> Result:
    """Stop one approved original instance without reopening an editor or closing the Session."""

    async def phase(awaitable, title):
        return await work(awaitable, title) if work is not None else await awaitable

    session = service.api.session
    key = (notebook["metadata"]["namespace"], notebook["metadata"]["uid"])
    active = getattr(session, "editor_recoveries", None)
    if active is None:
        active = session.editor_recoveries = set()
    if key in active:
        raise failure("Refused", "An editor recovery is already active for this target.")
    active.add(key)
    process = grant = stderr = selected = result = None
    transmitted = False
    interrupted = False
    try:
        session.check()
        selected = await phase(
            snapshot(service, notebook, pod_name, container_name),
            "Checking Notebook identity and lifecycle",
        )
        profile = await phase(qualify(tools, selected), "Checking editor recovery support")
        seconds = min(120, session.remaining - 2)
        if seconds <= 0:
            raise failure("AuthorizationUnavailable")
        deadline = asyncio.get_running_loop().time() + seconds
        grant = await session.create_grant()
        operation = uuid.uuid4().hex
        source = files("lab.remote").joinpath("editor_recovery.py").read_text()
        process = await asyncio.create_subprocess_exec(
            str(tools.require("kubectl")),
            "--kubeconfig",
            str(grant.config_path),
            "--namespace",
            selected.namespace,
            "exec",
            "-i",
            selected.pod,
            "--container",
            selected.container,
            "--",
            helper_python,
            "-I",
            "-S",
            "-B",
            "-c",
            source,
            env=client_environment(grant.environment),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4096,
        )
        stderr = asyncio.create_task(drain_errors(process))
        assert process.stdin is not None
        session.check()
        seconds = min(120, session.remaining - 12)
        if seconds <= 0:
            raise failure("AuthorizationUnavailable")
        deadline = asyncio.get_running_loop().time() + seconds
        process.stdin.write(
            json.dumps(
                {
                    "version": 1,
                    "operation": operation,
                    "profile": profile.remote(),
                    "seconds": seconds,
                }
            ).encode()
            + b"\n"
        )
        async with asyncio.timeout_at(deadline):
            await process.stdin.drain()
            frame = await phase(
                read_frame(process, operation, deadline),
                "Inspecting the original VS Code Server process",
            )
            if frame["status"] != "AwaitingConfirmation":
                result = Result(frame["status"], selected)
            elif not await confirm(selected, frame):
                result = Result("Cancelled", selected)
            else:
                current = await phase(
                    snapshot(service, notebook, selected.pod, selected.container),
                    "Revalidating the selected container",
                )
                if current != selected:
                    raise failure(
                        "Refused", "Notebook, Pod, or container identity changed before proceed."
                    )
                session.check()
                transmitted = True
                process.stdin.write(
                    json.dumps({"version": 1, "operation": operation, "action": "proceed"}).encode()
                    + b"\n"
                )
                await process.stdin.drain()
        if transmitted:
            frame = await phase(
                read_frame(process, operation, asyncio.get_running_loop().time() + 18),
                "Waiting for the original process to exit",
            )
            if frame["status"] == "AwaitingConfirmation":
                raise ValueError
            result = Result(frame["status"], selected)
    except LabError as error:
        status = (error.data or {}).get("outcome") or {
            2: "InvalidInput",
            3: "AuthorizationUnavailable",
            4: "AuthorizationUnavailable",
            6: "Refused",
            9: "Unsupported",
            130: "Cancelled",
        }.get(error.code, "Refused")
        result = Result(
            "OutcomeUnknown" if transmitted else status,
            selected,
            detail=None if transmitted else str(error),
        )
    except asyncio.CancelledError:
        interrupted = True
        result = Result("OutcomeUnknown" if transmitted else "Cancelled", selected)
    except (OSError, ValueError, TimeoutError):
        result = Result("OutcomeUnknown" if transmitted else "Unsupported", selected)
    finally:
        if result is None:
            result = Result("OutcomeUnknown" if transmitted else "Refused", selected)
        cleanup = asyncio.create_task(cleanup_helper(process, stderr, session, grant, result))
        try:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
                    # Finish owned cleanup, then unwind with the known remote outcome.
                    continue
            await cleanup
        finally:
            active.discard(key)
    if interrupted:
        raise LabError(MESSAGES[result.status], CODES[result.status], data=result.data())
    return result


async def cleanup_helper(process, stderr, session, grant, result):
    """Bound cleanup of owned resources without replacing a known remote outcome."""
    try:
        async with asyncio.timeout(7):
            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                await stop_client(process)
    except (Exception, asyncio.CancelledError):
        result.warning = "The local helper client could not be fully cleaned up."
    finally:
        if stderr is not None:
            stderr.cancel()
            await asyncio.gather(stderr, return_exceptions=True)
    try:
        async with asyncio.timeout(5):
            if grant is not None:
                await session.revoke_grant(grant)
    except (Exception, asyncio.CancelledError):
        result.warning = "The helper grant cleanup failed; the known stop outcome is unchanged."


async def revalidate(service, notebook, selected):
    """Require a fresh ordinary selection when an incarnation changed before Open."""
    if await snapshot(service, notebook, selected.pod, selected.container) != selected:
        raise failure("Refused", "Target changed before editor launch. Select the Notebook again.")
