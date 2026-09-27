"""Observe node allocatable resources and visible pod reservations without creation policy."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from lab.config import namespace_name, quantity
from lab.errors import LabError
from lab.kubernetes import ApiError, Kubernetes, resource_path

RESERVING_PODS = "status.phase!=Succeeded,status.phase!=Failed,spec.nodeName!="


def amount(value: int | None, resource: str) -> str:
    """Format normalized resource values as cores, GiB or integer units."""
    if value is None:
        return "?"
    divisor = 1000 if resource == "cpu" else 1024**3 if resource == "memory" else 1
    return f"{value / divisor:,.1f}".removesuffix(".0") if divisor != 1 else f"{value:,}"


def quantities(values: dict) -> dict[str, int]:
    """Read API quantities in millicores, bytes, or integer extended-resource units."""
    return {
        name: quantity(str(value), cpu=name == "cpu", allow_zero=True)
        for name, value in values.items()
    }


def combine(*values: dict[str, int], maximum: bool = False) -> dict[str, int]:
    result: dict[str, int] = {}
    for resources in values:
        for name, value in resources.items():
            result[name] = (
                max(result.get(name, 0), value) if maximum else result.get(name, 0) + value
            )
    return result


def pod_requests(pod: dict) -> dict[str, int]:
    """Account for app containers, ordered init/sidecars, pod budgets and overhead.

    Resizing pods cannot be consistently interpreted across Kubernetes versions;
    callers omit those reservations and report upper bounds.
    """
    spec, status = pod.get("spec", {}), pod.get("status", {})
    if spec.get("resourceClaims") or status.get("nodeAllocatableResourceClaimStatuses"):
        raise ValueError("Dynamic Resource Allocation is not included")
    if status.get("resize") or any(
        condition.get("type") in {"PodResizePending", "PodResizeInProgress"}
        and condition.get("status") == "True"
        for condition in status.get("conditions", [])
    ):
        raise ValueError("Pod resources are resizing")
    statuses = {
        item["name"]: item
        for item in status.get("containerStatuses", []) + status.get("initContainerStatuses", [])
    }

    def requests(container: dict, *, check_status=True) -> dict[str, int]:
        desired = quantities(container.get("resources", {}).get("requests", {}))
        current = statuses.get(container.get("name"), {}) if check_status else {}
        for observed in (
            current.get("allocatedResources"),
            current.get("resources", {}).get("requests"),
        ):
            if observed is not None and any(
                quantities(observed).get(key, 0) != desired.get(key, 0) for key in ("cpu", "memory")
            ):
                raise ValueError("Pod resource allocation differs from its request")
        return desired

    running = combine(*(requests(container) for container in spec.get("containers", [])))
    sidecars: dict[str, int] = {}
    peak: dict[str, int] = {}
    for container in spec.get("initContainers", []):
        requested = requests(container, check_status=container.get("restartPolicy") == "Always")
        if container.get("restartPolicy") == "Always":
            sidecars = combine(sidecars, requested)
            running = combine(running, requested)
            requested = sidecars
        else:
            requested = combine(sidecars, requested)
        peak = combine(peak, requested, maximum=True)
    total = combine(running, peak, maximum=True)
    budget = quantities(spec.get("resources", {}).get("requests", {}))
    for observed in (status.get("allocatedResources"), status.get("resources", {}).get("requests")):
        if observed is not None:
            baseline = budget or total
            if any(
                quantities(observed).get(key, 0) != baseline.get(key, 0)
                for key in ("cpu", "memory")
            ):
                raise ValueError("Pod resource allocation differs from its request")
    total.update(
        {
            key: value
            for key, value in budget.items()
            if key in {"cpu", "memory"} or key.startswith("hugepages-")
        }
    )
    return combine(total, quantities(spec.get("overhead", {})))


@dataclass
class NodeResources:
    name: str
    allocatable: dict[str, int]
    labels: dict[str, str]
    state: str
    taints: tuple[str, ...]
    reserved: dict[str, int] = field(default_factory=dict)
    issue: str = ""

    def remaining(self, resource: str) -> int | None:
        if resource not in self.allocatable:
            return None
        return max(0, self.allocatable[resource] - self.reserved.get(resource, 0))

    def model(self, resource: str) -> str:
        return self.labels.get(resource + ".product", "") or (
            self.labels.get("nvidia.com/gpu.product", "")
            if resource.startswith("nvidia.com/")
            else ""
        )


@dataclass
class ResourceSnapshot:
    nodes: list[NodeResources]
    namespaces: tuple[str, ...] | None
    issues: tuple[str, ...]
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def resources(self) -> list[str]:
        return sorted({key for node in self.nodes for key in node.allocatable if "/" in key})

    @property
    def scope(self) -> str:
        if self.namespaces is None:
            return "All namespaces"
        return ", ".join(self.namespaces) if self.namespaces else "No pod data"

    @property
    def partial(self) -> bool:
        return self.namespaces is not None or any(node.issue for node in self.nodes)

    def report(self) -> dict:
        """Export the supported resource counts without raw API objects or credentials."""
        units = {"cpu": "millicores", "memory": "bytes", **{key: "units" for key in self.resources}}
        nodes: list[dict] = []
        for node in self.nodes:
            capacity = {key: value for key, value in node.allocatable.items() if key in units}
            nodes.append(
                {
                    "name": node.name,
                    "state": node.state,
                    "taints": list(node.taints),
                    "models": {
                        key: node.model(key)
                        for key in self.resources
                        if key in capacity and node.model(key)
                    },
                    "allocatable": capacity,
                    "observed_reserved": (
                        {key: node.reserved.get(key, 0) for key in capacity}
                        if self.namespaces != ()
                        else None
                    ),
                    "remaining_upper_bound": (
                        {key: node.remaining(key) for key in capacity}
                        if node.state == "Ready"
                        else None
                    ),
                    "issue": node.issue or None,
                }
            )
        return {
            "observed_at": self.observed_at.isoformat(),
            "scope": {
                "all_namespaces": self.namespaces is None,
                "namespaces": list(self.namespaces) if self.namespaces is not None else None,
            },
            "partial": self.partial,
            "issues": list(self.issues),
            "units": units,
            "summary": {
                "nodes": len(nodes),
                "ready_nodes": sum(node["state"] == "Ready" for node in nodes),
                "allocatable": combine(*(node["allocatable"] for node in nodes)),
                "observed_reserved": (
                    combine(*(node["observed_reserved"] for node in nodes))
                    if self.namespaces != ()
                    else None
                ),
                "remaining_upper_bound": combine(
                    *(node["remaining_upper_bound"] or {} for node in nodes)
                ),
            },
            "nodes": nodes,
        }


def snapshot(nodes: list[dict], pods: list[dict] | None, namespaces, issues=()) -> ResourceSnapshot:
    """Bind non-terminal assigned pods to nodes; never treat missing reads as zero."""
    reservations: dict[str, dict[str, int]] = defaultdict(dict)
    unknown: dict[str, str] = {}
    seen = set()
    for pod in pods or []:
        node = pod.get("spec", {}).get("nodeName")
        if not node or pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        metadata = pod["metadata"]
        identity = metadata.get("uid") or (metadata.get("namespace"), metadata["name"])
        if identity in seen:
            continue
        seen.add(identity)
        try:
            reservations[node] = combine(reservations[node], pod_requests(pod))
        except ValueError as error:
            unknown[node] = str(error)
    result = []
    for node in nodes:
        name = node["metadata"]["name"]
        status, spec = node.get("status", {}), node.get("spec", {})
        issue = "Pod reservations are not readable" if pods is None else unknown.get(name, "")
        try:
            allocatable = quantities(status.get("allocatable", {}))
        except ValueError:
            raise LabError(f"Cannot interpret allocatable quantities for node {name}.", 6) from None
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions", [])
        )
        state = "NotReady" if not ready else "Cordoned" if spec.get("unschedulable") else "Ready"
        reserved = reservations[name]
        if any(value > allocatable.get(key, 0) for key, value in reserved.items()):
            issue = issue or "Observed requests exceed allocatable; refresh the snapshot"
            reserved = {}
        taints = tuple(
            f"{taint['key']}={taint.get('value', '')}:{taint['effect']}"
            for taint in spec.get("taints", [])
        )
        model_labels = {resource + ".product" for resource in allocatable}
        if any(resource.startswith("nvidia.com/") for resource in allocatable):
            model_labels.add("nvidia.com/gpu.product")
        result.append(
            NodeResources(
                name,
                allocatable,
                {
                    key: value
                    for key, value in node["metadata"].get("labels", {}).items()
                    if key in model_labels
                },
                state,
                taints,
                reserved,
                issue,
            )
        )
    return ResourceSnapshot(result, namespaces, tuple(issues))


async def load_resources(api: Kubernetes, namespaces: tuple[str, ...] = ()) -> ResourceSnapshot:
    """Subtract every readable reservation from allocatable capacity, using namespace hints."""
    try:
        namespaces = tuple(dict.fromkeys(namespace_name(name) for name in namespaces))
    except ValueError as error:
        raise LabError(str(error), 2) from None
    nodes = await api.collection("/api/v1/nodes")
    try:
        pods = await api.collection("/api/v1/pods", fieldSelector=RESERVING_PODS)
    except ApiError as error:
        if error.status != 403:
            raise
    else:
        return snapshot(nodes, pods, None)
    candidates = set(namespaces)
    issues = []
    try:
        candidates.update(
            item["metadata"]["name"] for item in await api.collection("/api/v1/namespaces")
        )
    except ApiError as error:
        if error.status != 403:
            raise
        issues.append(
            "Namespace discovery denied; "
            + ("using known namespace names" if candidates else "no namespace names available")
        )

    limit = asyncio.Semaphore(8)

    async def read(namespace):
        async with limit:
            try:
                return (
                    namespace,
                    await api.collection(
                        resource_path(namespace, "pods"), fieldSelector=RESERVING_PODS
                    ),
                    "",
                )
            except LabError as error:
                return namespace, None, f"{namespace}: {error}"

    pods, readable = [], []
    for namespace, observed, failure in await asyncio.gather(
        *(read(name) for name in sorted(candidates))
    ):
        if observed is None:
            issues.append(failure)
        else:
            pods.extend(observed)
            readable.append(namespace)
    if not readable:
        issues.append("Capacity ceilings only; no reservations readable. n adds namespace hints")
    return snapshot(nodes, pods if readable else None, tuple(readable), issues)
