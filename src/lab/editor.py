"""Direct VS Code attachment to a Kubernetes container using a session grant."""

import asyncio
import json
import os
from urllib.parse import quote

from lab.errors import LabError
from lab.kubernetes import owned_pods, pod_state
from lab.notebooks import Notebooks, require_running_request
from lab.tools import Toolchain, client_environment

SUPPORTED_EDITOR = "1.137.0"
SUPPORTED_CONTAINERS = "0.469.0"


async def target(
    service: Notebooks, notebook: dict, pod_name=None, container_name=None
) -> tuple[dict, str]:
    require_running_request(notebook)
    pods = [pod for pod in await owned_pods(service.api, notebook) if pod_state(pod) == "Ready"]
    if pod_name:
        pods = [pod for pod in pods if pod["metadata"]["name"] == pod_name]
    if len(pods) != 1:
        raise LabError("Select one Ready, owned Pod with --pod; no remote command was executed.", 7)
    pod = pods[0]
    containers = [value["name"] for value in pod["spec"]["containers"]]
    if container_name:
        containers = [name for name in containers if name == container_name]
    if len(containers) != 1:
        raise LabError("Select one container with --container; no remote command was executed.", 2)
    return pod, containers[0]


def remote_uri(
    context: str, namespace: str, pod: str, container: str, image: str, folder: str
) -> str:
    metadata = {
        "context": context,
        "podname": pod,
        "namespace": namespace,
        "name": container,
        "image": image,
    }
    authority = json.dumps(metadata, separators=(",", ":"), ensure_ascii=False).encode().hex()
    return f"vscode-remote://k8s-container+{authority}{quote(folder, safe='/')}"


async def _output(*args) -> str:
    process = await asyncio.create_subprocess_exec(
        *map(str, args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=client_environment(),
    )
    try:
        async with asyncio.timeout(15):
            stdout, _ = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise LabError("Editor compatibility check timed out.", 9) from None
    if process.returncode:
        raise LabError("Editor compatibility check failed.", 9)
    return stdout.decode("utf-8", errors="replace")


async def open_editor(
    service: Notebooks, notebook: dict, tools: Toolchain, pod_name=None, container_name=None
) -> None:
    code = tools.require("code")
    kubectl = tools.require("kubectl")
    version = (await _output(code, "--version")).splitlines()[0]
    extensions = (await _output(code, "--list-extensions", "--show-versions")).splitlines()
    if version != SUPPORTED_EDITOR or (
        "ms-vscode-remote.remote-containers@" + SUPPORTED_CONTAINERS not in extensions
    ):
        raise LabError(
            f"This adapter is tested with VS Code {SUPPORTED_EDITOR} and Dev Containers "
            f"{SUPPORTED_CONTAINERS}. Validate the installed combination before enabling it.",
            9,
        )
    pod, container = await target(service, notebook, pod_name, container_name)
    specification = next(value for value in pod["spec"]["containers"] if value["name"] == container)
    session = service.api.session
    grant = await session.create_grant()
    process = None
    try:
        await prerequisites(kubectl, grant, notebook, pod, container, specification)
        # The public context is unique to this grant.
        import yaml

        context = yaml.safe_load(grant.config_path.read_text())["current-context"]
        uri = remote_uri(
            context,
            notebook["metadata"]["namespace"],
            pod["metadata"]["name"],
            container,
            specification["image"],
            specification.get("workingDir", "/"),
        )
        process = await asyncio.create_subprocess_exec(
            str(code),
            "--new-window",
            "--folder-uri",
            uri,
            env=client_environment(
                {
                    **grant.environment,
                    "KUBECONFIG": str(grant.config_path),
                    "PATH": str(kubectl.parent) + os.pathsep + os.environ.get("PATH", ""),
                }
            ),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(30):
            result = await process.wait()
        if result:
            raise LabError("VS Code could not open the remote window.", 9)
    except BaseException as error:
        if process is not None:
            await stop_client(process)
        await session.revoke_grant(grant)
        if isinstance(error, (TimeoutError, OSError)):
            raise LabError(
                "The editor launch could not complete. Its grant was revoked.", 9
            ) from None
        raise


async def prerequisites(kubectl, grant, notebook, pod, container, specification) -> None:
    """Check existing runtime tools and the remote folder without installing anything."""
    script = (
        '[ -d "$1" ] || exit 2; '
        'for tool in tar gzip uname; do command -v "$tool" >/dev/null || exit 3; done'
    )
    process = await asyncio.create_subprocess_exec(
        str(kubectl),
        "--kubeconfig",
        str(grant.config_path),
        "--namespace",
        notebook["metadata"]["namespace"],
        "exec",
        pod["metadata"]["name"],
        "--container",
        container,
        "--",
        "/bin/sh",
        "-c",
        script,
        "lab-check",
        specification.get("workingDir", "/"),
        env=client_environment(grant.environment),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(15):
            code = await process.wait()
        if code:
            raise LabError(
                "Editor prerequisites failed. Check container access, its working directory, "
                "and /bin/sh, tar, gzip and uname. No tools were installed.",
                9,
            )
    finally:
        await stop_client(process)


async def open_shell(
    service: Notebooks, notebook: dict, tools: Toolchain, pod_name=None, container_name=None
) -> int:
    pod, container = await target(service, notebook, pod_name, container_name)
    grant = await service.api.session.create_grant()
    process = None
    waiter = None
    try:
        with open("/dev/tty", "r+b", buffering=0) as terminal:
            process = await asyncio.create_subprocess_exec(
                str(tools.require("kubectl")),
                "--kubeconfig",
                str(grant.config_path),
                "--namespace",
                notebook["metadata"]["namespace"],
                "exec",
                "-it",
                pod["metadata"]["name"],
                "--container",
                container,
                "--",
                "/bin/sh",
                env=client_environment({**grant.environment, "KUBECONFIG": str(grant.config_path)}),
                stdin=terminal,
                stdout=terminal,
                stderr=terminal,
            )
            waiter = asyncio.create_task(process.wait())
            while not waiter.done():
                service.api.session.check()
                try:
                    return await asyncio.wait_for(asyncio.shield(waiter), timeout=0.5)
                except TimeoutError:
                    continue
            return waiter.result()
    except OSError:
        raise LabError(
            "A controlling terminal and kubectl are required for a Notebook shell.", 2
        ) from None
    finally:
        if process is not None:
            await stop_client(process)
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)
        await service.api.session.revoke_grant(grant)


async def stop_client(process: asyncio.subprocess.Process) -> None:
    """Bound termination of a lab-owned client without targeting editor windows."""
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=3)
