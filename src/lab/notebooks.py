"""Notebook operations, with durable creation identity and explicit recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from lab.config import Cluster
from lab.errors import LabError
from lab.history import Repository
from lab.kubernetes import (
    ApiError,
    Kubernetes,
    group_owned_pods,
    owned_pods,
    pod_state,
    resource_path,
    wait_pod,
)
from lab.manifest import NotebookInput, compile_notebook, normalized_inputs
from lab.notebook_status import notebook_state
from lab.policy import Profile, load_profile

STOP_ANNOTATION = "kubeflow-resource-stopped"
OPERATION_LABEL = "lab.operations/id"


def now() -> str:
    return datetime.now(UTC).isoformat()


def require_running_request(notebook: dict) -> None:
    """Reject new connections after a Notebook requests shutdown."""
    if STOP_ANNOTATION in notebook["metadata"].get("annotations", {}):
        raise LabError("The Notebook is stopping or stopped. Start it before connecting.", 7)


def status_snapshot(notebook: dict, pods: list[dict]) -> dict:
    stopped = STOP_ANNOTATION in notebook["metadata"].get("annotations", {})
    observations = [{"name": pod["metadata"]["name"], "state": pod_state(pod)} for pod in pods]
    return {
        "uid": notebook["metadata"]["uid"],
        "stopped": stopped,
        "state": notebook_state(stopped, observations),
        "pods": observations,
    }


class Notebooks:
    """Apply the same operations from interactive menus and standalone commands."""

    def __init__(
        self, api: Kubernetes, cluster: Cluster, history: Repository, profiles_dir: Path
    ) -> None:
        self.api, self.cluster, self.history = api, cluster, history
        self.profiles_dir = profiles_dir

    def profile(self) -> Profile:
        """Read the current cluster's creation rules only when they are needed."""
        return load_profile(self.profiles_dir, self.cluster.cluster_id)

    async def list_notebooks(self, namespace: str) -> list[dict]:
        return await self.api.collection(resource_path(namespace, "notebooks"))

    async def get(self, namespace: str, name: str) -> dict:
        return await self.api.request("GET", resource_path(namespace, "notebooks", name))

    async def status(self, namespace: str, name: str) -> dict:
        notebook = await self.get(namespace, name)
        pods = await owned_pods(self.api, notebook)
        return status_snapshot(notebook, pods)

    async def statuses(self, namespace: str, notebooks: list[dict]) -> dict[str, dict]:
        """Read one namespace's runtime collections concurrently and bind results to listed UIDs."""
        if not notebooks:
            return {}
        if any(item["metadata"]["namespace"] != namespace for item in notebooks):
            raise LabError("Notebook status lookup cannot cross namespaces.", 4)
        queries = [
            asyncio.create_task(self.api.collection(resource_path(namespace, "statefulsets"))),
            asyncio.create_task(
                self.api.collection(resource_path(namespace, "pods"), labelSelector="notebook-name")
            ),
        ]
        try:
            parents, pods = await asyncio.gather(*queries)
        finally:
            for query in queries:
                query.cancel()
            await asyncio.gather(*queries, return_exceptions=True)
        grouped = group_owned_pods(notebooks, parents, pods)
        return {
            item["metadata"]["uid"]: status_snapshot(item, grouped[item["metadata"]["uid"]])
            for item in notebooks
        }

    def prepare(self, inputs: NotebookInput, profile: Profile) -> dict:
        inputs = normalized_inputs(profile, inputs)
        operation_id = str(uuid4())
        manifest = compile_notebook(profile, inputs, operation_id)
        receipt = {
            "operation_id": operation_id,
            "cluster_id": str(self.cluster.cluster_id),
            "namespace": inputs.namespace,
            "name": inputs.name,
            "operation": "create",
            "inputs": inputs.model_dump(mode="json"),
            "manifest": manifest,
            "request_digest": self.digest(manifest),
            "state": "Prepared",
            "created_at": now(),
            "updated_at": now(),
        }
        self.history.record_operation(receipt)
        return receipt

    @staticmethod
    def digest(manifest: dict) -> str:
        return hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    async def submit(self, receipt: dict) -> dict:
        operation_id = receipt["operation_id"]
        self.history.update_operation(operation_id, state="Sending", updated_at=now())
        try:
            notebook = await self.api.request(
                "POST", resource_path(receipt["namespace"], "notebooks"), body=receipt["manifest"]
            )
        except ApiError as error:
            state = "Unknown" if error.status >= 500 or error.status == 409 else "Rejected"
            error.data = {"operation_id": operation_id, "state": state}
            self.history.update_operation(operation_id, state=state, updated_at=now())
            if error.status == 409:
                return await self.reconcile(operation_id)
            raise
        except LabError:
            self.history.update_operation(operation_id, state="Unknown", updated_at=now())
            raise LabError(
                f"Creation outcome unknown. Inspect operation {operation_id}; "
                "do not create a new name.",
                5,
                data={"operation_id": operation_id, "state": "Unknown"},
            ) from None
        except asyncio.CancelledError:
            raise LabError(
                "Creation was interrupted. Inspect its operation before retrying.",
                130,
                data={"operation_id": operation_id, "state": "Unknown"},
            ) from None
        await self.accept(receipt, notebook)
        return notebook

    async def accept(self, receipt: dict, notebook: dict) -> None:
        try:
            self.history.update_operation(
                receipt["operation_id"],
                state="Accepted",
                uid=notebook["metadata"]["uid"],
                updated_at=now(),
            )
            self.history.remember(receipt["cluster_id"], receipt["namespace"], receipt["inputs"])
        except LabError:
            raise LabError(
                f"Notebook {receipt['namespace']}/{receipt['name']} was created "
                f"(UID {notebook['metadata']['uid']}); local recording failed. "
                f"Inspect operation {receipt['operation_id']} before retrying.",
                8,
                data={
                    "operation_id": receipt["operation_id"],
                    "state": "Accepted",
                    "uid": notebook["metadata"]["uid"],
                },
            ) from None

    async def reconcile(self, operation_id: str) -> dict:
        receipt = self.receipt(operation_id)
        notebook = await self.get(receipt["namespace"], receipt["name"])
        if notebook["metadata"].get("labels", {}).get(OPERATION_LABEL) != operation_id:
            raise LabError(
                "The target name belongs to a different operation. Nothing was changed.", 6
            )
        await self.accept(receipt, notebook)
        return notebook

    def receipt(self, operation_id: str) -> dict:
        receipt = self.history.operation(operation_id)
        if receipt["cluster_id"] != str(self.cluster.cluster_id):
            raise LabError("This operation belongs to a different cluster.", 4)
        return receipt

    async def retry(self, operation_id: str) -> dict:
        receipt = self.receipt(operation_id)
        try:
            return await self.reconcile(operation_id)
        except ApiError as error:
            if error.status != 404:
                raise
        inputs = NotebookInput.model_validate(receipt["inputs"])
        current = compile_notebook(self.profile(), inputs, operation_id)
        if self.digest(current) != receipt["request_digest"]:
            raise LabError(
                "Cluster policy changed. Review a new creation instead of replaying this receipt.",
                4,
            )
        return await self.submit(receipt)

    async def wait(self, receipt: dict, notebook: dict, timeout: float) -> list[dict]:
        try:
            pods = await wait_pod(self.api, notebook, timeout)
        except LabError as error:
            if error.code == 7 and "timed out" not in str(error):
                self.history.update_operation(
                    receipt["operation_id"], state="RuntimeFailed", updated_at=now()
                )
            raise
        self.history.update_operation(receipt["operation_id"], state="Ready", updated_at=now())
        return pods

    async def change(self, notebook: dict, action: str) -> dict:
        metadata = notebook["metadata"]
        path = resource_path(metadata["namespace"], "notebooks", metadata["name"])
        if action == "delete":
            return await self.api.request(
                "DELETE",
                path,
                body={
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": metadata["uid"]},
                    "propagationPolicy": "Background",
                },
            )
        annotations = metadata.get("annotations", {})
        stopped = STOP_ANNOTATION in annotations
        if (action == "stop") == stopped:
            return {"status": "Stop already requested" if stopped else "Start already requested"}
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": metadata["resourceVersion"],
            },
        ]
        if action == "stop":
            patch.append(
                {
                    "op": "add",
                    "path": "/metadata/annotations",
                    "value": {**annotations, STOP_ANNOTATION: now()},
                }
            )
        elif action == "start":
            patch.append({"op": "remove", "path": "/metadata/annotations/" + STOP_ANNOTATION})
        else:
            raise LabError("Unsupported Notebook action.", 2)
        return await self.api.request("PATCH", path, body=patch)
