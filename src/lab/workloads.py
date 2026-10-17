"""Group visible pods into workloads and attach optional metrics.k8s.io usage."""

import asyncio
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from urllib.parse import quote

from lab.config import quantity
from lab.errors import LabError
from lab.kubernetes import Kubernetes
from lab.resources import PodRecord, ResourceSnapshot, load_resources

METRICS = "/apis/metrics.k8s.io/v1beta1"


@dataclass
class Workload:
    """Pods that share a controller, such as a job, a Notebook or a ReplicaSet."""

    namespace: str
    kind: str
    name: str
    owner: str
    pods: list[PodRecord] = field(default_factory=list)

    def held(self, resource: str) -> int:
        """Requests of pods already placed on a node; waiting pods hold nothing yet."""
        return sum((pod.requests or {}).get(resource, 0) for pod in self.pods if pod.node)

    def waiting(self, resource: str) -> int:
        return sum((pod.requests or {}).get(resource, 0) for pod in self.pods if not pod.node)

    @property
    def unknown(self) -> bool:
        """Some pod's requests cannot be interpreted, so held and waiting are lower bounds."""
        return any(pod.requests is None for pod in self.pods)

    def nodes(self) -> list[str]:
        return sorted({pod.node for pod in self.pods if pod.node})

    def started(self) -> datetime | None:
        return min((pod.started for pod in self.pods if pod.started), default=None)

    def state(self, resource: str) -> str:
        waiting = self.waiting(resource)
        if waiting and not self.held(resource):
            return f"Waiting for {waiting:,}"
        phases = Counter(pod.phase for pod in self.pods)
        if len(phases) == 1:
            return next(iter(phases))
        return f"{phases['Running']}/{len(self.pods)} Running"


def group(pods: list[PodRecord]) -> list[Workload]:
    grouped: dict[tuple[str, str, str], Workload] = {}
    for pod in pods:
        kind, name = pod.workload
        key = (pod.namespace, kind, name)
        grouped.setdefault(key, Workload(pod.namespace, kind, name, pod.owner)).pods.append(pod)
    return list(grouped.values())


def usage_quantity(value: str, cpu: bool) -> int:
    # metrics.k8s.io reports nanocores; usage is a sample, so millicores are rounded down.
    if cpu and value[-1:] in {"n", "u"}:
        return int(value[:-1]) // (1_000_000 if value[-1] == "n" else 1_000)
    return quantity(value, cpu=cpu, allow_zero=True)


def pod_usage(items: list[dict]) -> dict[tuple[str, str], dict[str, int]]:
    """Sum container usage per pod; a pod with an unreadable sample is left out."""
    result = {}
    for item in items:
        metadata = item.get("metadata", {})
        total = {"cpu": 0, "memory": 0}
        try:
            for container in item.get("containers", []):
                for resource in total:
                    total[resource] += usage_quantity(
                        str(container["usage"][resource]), resource == "cpu"
                    )
        except (KeyError, ValueError, TypeError):
            continue
        result[(metadata.get("namespace", ""), metadata.get("name", ""))] = total
    return result


async def load_usage(api: Kubernetes, namespaces: tuple[str, ...] | None) -> dict | None:
    """Read current usage for the same scope as the pods; None when no scope is readable."""
    paths = (
        [METRICS + "/pods"]
        if namespaces is None
        else [f"{METRICS}/namespaces/{quote(name, safe='')}/pods" for name in namespaces]
    )

    limit = asyncio.Semaphore(8)

    async def read(path):
        try:
            async with limit:
                return await api.collection(path)
        except LabError as error:
            # Usage is optional: 5 is this request failing on a healthy connection, 6 an API
            # refusal or unusable reply. A lost connection raises its own error and propagates.
            if error.code not in {5, 6}:
                raise
            return None

    pages = await asyncio.gather(*(read(path) for path in paths))
    readable = [items for items in pages if items is not None]
    if not readable:
        return None
    return pod_usage([item for items in readable for item in items])


async def load_workloads(
    api: Kubernetes, namespaces: tuple[str, ...] = (), owner_key: str = ""
) -> ResourceSnapshot:
    resources = await load_resources(api, namespaces, owner_key)
    if resources.pods is None:
        return resources
    return replace(resources, usage=await load_usage(api, resources.namespaces))
