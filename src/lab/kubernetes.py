"""Namespace-explicit Kubernetes requests over the current session's TLS route."""

import asyncio
import json
from urllib.parse import quote

import aiohttp

from lab.errors import LabError
from lab.session import Session


class ApiError(LabError):
    def __init__(self, status: int, action: str) -> None:
        labels = {
            401: "authentication rejected",
            403: "access denied",
            404: "not found",
            409: "conflict",
            422: "validation rejected",
            429: "rate limited",
        }
        super().__init__(
            f"Kubernetes {action}: {labels.get(status, 'request failed')} (HTTP {status}).", 6
        )
        self.status = status


class ResponseTooLarge(LabError):
    """Bound response memory while allowing list requests to reduce page size."""

    def __init__(self, method: str, path: str) -> None:
        super().__init__(f"Kubernetes {method} {path}: response exceeds the 8 MiB limit.", 6)


class Kubernetes:
    """Use explicit session credentials; never consult ambient kubeconfig or proxies."""

    def __init__(self, session: Session) -> None:
        self.session = session

    async def request(self, method: str, path: str, *, body=None, params=None):
        self.session.check()
        headers = {"Authorization": "Bearer " + self.session.token.get_secret_value()}
        if method == "PATCH":
            headers["Content-Type"] = "application/json-patch+json"
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15), trust_env=False
                ) as client,
                client.request(
                    method,
                    self.session.endpoint + path,
                    json=body,
                    params=params,
                    headers=headers,
                    ssl=self.session.ssl_context,
                    server_hostname=self.session.tls_name,
                    allow_redirects=False,
                ) as response,
            ):
                if not 200 <= response.status < 300:
                    raise ApiError(response.status, method)
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 8 * 1024 * 1024:
                        raise ResponseTooLarge(method, path)

                try:
                    return json.loads(raw)
                except (ValueError, UnicodeError):
                    raise LabError("Kubernetes returned an invalid JSON response.", 6) from None
        except (aiohttp.ClientError, TimeoutError, OSError):
            await self.session.check_health()
            message = (
                "Kubernetes status query was interrupted. No cluster changes were requested."
                if method == "GET"
                else "The Kubernetes request was interrupted; its outcome may be unknown."
            )
            raise LabError(message, 5) from None

    async def collection(self, path: str, **params) -> list[dict]:
        result = []
        seen = set()
        limit = int(params.pop("limit", 200))
        if limit < 1:
            raise LabError("Kubernetes list page size must be positive.", 2)
        while True:
            try:
                page = await self.request("GET", path, params={**params, "limit": str(limit)})
            except ResponseTooLarge:
                if limit == 1:
                    raise
                limit = max(1, limit // 2)
                continue
            result.extend(page.get("items", []))
            continuation = page.get("metadata", {}).get("continue")
            if not continuation:
                return result
            if continuation in seen:
                raise LabError("Kubernetes repeated a pagination cursor.", 6)
            seen.add(continuation)
            params["continue"] = continuation


def resource_path(namespace: str, resource: str, name: str | None = None) -> str:
    base = "/apis/kubeflow.org/v1" if resource == "notebooks" else "/api/v1"
    if resource == "statefulsets":
        base = "/apis/apps/v1"
    path = f"{base}/namespaces/{quote(namespace, safe='')}/{resource}"
    return path + ("/" + quote(name, safe="") if name else "")


async def owned_pods(api: Kubernetes, notebook: dict) -> list[dict]:
    namespace = notebook["metadata"]["namespace"]
    uid = notebook["metadata"]["uid"]
    name = notebook["metadata"]["name"]
    # Kubeflow creates a same-named StatefulSet and labels its Pods notebook-name.
    parents = await api.collection(
        resource_path(namespace, "statefulsets"), fieldSelector=f"metadata.name={name}"
    )
    pods = await api.collection(
        resource_path(namespace, "pods"), labelSelector=f"notebook-name={name}"
    )
    return group_owned_pods([notebook], parents, pods)[uid]


def group_owned_pods(
    notebooks: list[dict], parents: list[dict], pods: list[dict]
) -> dict[str, list[dict]]:
    """Index runtime observations by Notebook UID, including its same-named StatefulSet."""
    names = {item["metadata"]["uid"]: item["metadata"]["name"] for item in notebooks}
    owners = {uid: uid for uid in names}
    for parent in parents:
        metadata = parent["metadata"]
        for reference in metadata.get("ownerReferences", []):
            uid = reference.get("uid")
            if uid in names and metadata.get("name") == names[uid]:
                owners[metadata["uid"]] = uid
    grouped: dict[str, list[dict]] = {uid: [] for uid in names}
    for pod in pods:
        targets = {
            owners[reference["uid"]]
            for reference in pod["metadata"].get("ownerReferences", [])
            if reference.get("uid") in owners
        }
        for uid in targets:
            grouped[uid].append(pod)
    return grouped


def pod_state(pod: dict) -> str:
    status = pod.get("status", {})
    if pod.get("metadata", {}).get("deletionTimestamp"):
        return "Terminating"
    for item in status.get("containerStatuses", []):
        reason = item.get("state", {}).get("waiting", {}).get("reason")
        if reason in {
            "ImagePullBackOff",
            "ErrImagePull",
            "CrashLoopBackOff",
            "CreateContainerConfigError",
            "CreateContainerError",
        }:
            return reason
    if any(
        item.get("type") == "Ready" and item.get("status") == "True"
        for item in status.get("conditions", [])
    ):
        return "Ready"
    phase = status.get("phase", "Pending")
    return phase if phase in {"Pending", "Running", "Succeeded", "Failed", "Unknown"} else "Unknown"


async def wait_pod(api: Kubernetes, notebook: dict, timeout: float) -> list[dict]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        api.session.check()
        pods = await owned_pods(api, notebook)
        if any(pod_state(pod) == "Ready" for pod in pods):
            return pods
        failures = {pod_state(pod) for pod in pods} & {
            "Failed",
            "Succeeded",
            "ImagePullBackOff",
            "ErrImagePull",
            "CrashLoopBackOff",
            "CreateContainerConfigError",
            "CreateContainerError",
        }
        if failures:
            raise LabError("Notebook runtime: " + ", ".join(sorted(failures)), 7)
        if asyncio.get_running_loop().time() >= deadline:
            raise LabError("Readiness wait timed out. The Notebook remains on the cluster.", 7)
        await asyncio.sleep(min(2, max(0, deadline - asyncio.get_running_loop().time())))
