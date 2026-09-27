"""Interactive Notebook workflows over the current authorized session."""

from __future__ import annotations

import asyncio
import shlex
from contextlib import suppress
from dataclasses import dataclass, field

import yaml
from pydantic import ValidationError

from lab import editor_recovery
from lab.config import namespace_name
from lab.display import display_session, release_display
from lab.editor import open_editor, open_shell
from lab.errors import LabError
from lab.guidance import NOTEBOOK, SETUP
from lab.kubernetes import owned_pods, pod_state
from lab.manifest import NotebookInput, compile_notebook, normalized_inputs
from lab.notebook_status import (
    pod_rows,
    resource_summary,
    state_label,
    status_actions,
    status_description,
)
from lab.notebooks import STOP_ANNOTATION, Notebooks, require_running_request
from lab.policy import Profile
from lab.resource_view import browse_resources
from lab.tools import Toolchain
from lab.ui import Field, Screens

FIELDS = (
    ("name", "Notebook name", "Identity"),
    ("namespace", "Namespace", "Identity"),
    ("owner", "Owner", "Identity"),
    ("image", "Image", "Identity"),
    ("gpu_type", "GPU type", "Compute"),
    ("gpus", "GPU count (0 = CPU)", "Compute"),
    ("cpu", "CPU (cores or m)", "Compute"),
    ("memory", "Memory (Gi / Mi)", "Compute"),
    ("node", "Node (optional)", "Compute"),
    ("storage_source", "Storage source", "Storage"),
    ("mount_path", "Mount path", "Storage"),
    ("workdir", "Working directory", "Storage"),
)


@dataclass
class Interactive:
    """Present real operations without outliving the surrounding authorization.

    :param screens: Optional screen driver for terminal-independent testing.
    """

    service: Notebooks
    tools: Toolchain
    screens: Screens | None = None
    outcome: dict = field(default_factory=dict, init=False)
    result: dict | None = field(default=None, init=False)
    originals: dict = field(default_factory=dict, init=False)

    def __post_init__(self):
        if self.screens is None:
            self.screens = Screens(context=f"lab / {self.cluster_id}")

    @property
    def ui(self) -> Screens:
        assert self.screens is not None
        return self.screens

    @property
    def cluster_id(self) -> str:
        return str(self.service.cluster.cluster_id)

    def _record(self, action: str, status: str, data: dict | list | None = None) -> None:
        if data is not None:
            self.outcome["data"] = data
        self.result = {
            "schema_version": 1,
            "operation": action,
            "status": status,
            "target": self.outcome.get("target"),
            "data": self.outcome.get("data"),
        }

    def _target(self, namespace: str, name: str | None = None) -> None:
        self.outcome["target"] = {"cluster": self.cluster_id, "namespace": namespace, "name": name}

    def _identity(self, receipt: dict) -> None:
        self._target(receipt["namespace"], receipt["name"])
        self.outcome["data"] = {
            key: receipt[key] for key in ("operation_id", "uid", "state") if receipt.get(key)
        }

    def _failure(self, error: LabError) -> LabError:
        error.target = error.target or self.outcome.get("target")
        known = self.outcome.get("data")
        if isinstance(known, dict):
            error.data = {**known, **(error.data or {})}
        self.result = {
            "schema_version": 1,
            "operation": self.result.get("operation", "interactive")
            if self.result
            else "interactive",
            "status": "cancelled" if error.code == 130 else "error",
            "target": error.target,
            "data": error.data,
        }
        return error

    async def run(self, editor_only: bool = False) -> None:
        """Run menus until explicit exit or cancellation of the session task."""
        try:
            async with display_session():
                await self._run(editor_only)
        except asyncio.CancelledError:
            raise self._failure(
                LabError("Session interrupted. Remote Notebooks remain on the cluster.", 130)
            ) from None
        except LabError as error:
            raise self._failure(error) from None

    async def _run(self, editor_only: bool) -> None:
        session = self.service.api.session
        pending = ""
        while session.state not in {"closing", "closed"}:
            if not pending:
                self.outcome = {}
                sections = []
                if not editor_only:
                    sections = [
                        (
                            "NOTEBOOKS",
                            [
                                ("list", "Browse Notebooks"),
                                ("create", "Create Notebook"),
                                ("inspect", "Notebook status"),
                                ("shell", "Notebook shell"),
                                ("open", "Open in VS Code"),
                                ("editor-restart", "Restart VS Code Server…"),
                            ],
                        ),
                        (
                            "CLUSTER",
                            [("resources", "Cluster resources")],
                        ),
                        (
                            "MANAGE",
                            [
                                ("start", "Start Notebook"),
                                ("stop", "Stop Notebook"),
                                ("delete", "Delete Notebook"),
                                ("retry", "Retry operation"),
                                ("save-preset", "Save preset"),
                            ],
                        ),
                    ]
                sections.append(
                    (
                        "SESSION",
                        [
                            ("status", "Session status"),
                            ("reconnect", "Reconnect"),
                            ("exit", "Disconnect"),
                        ],
                    )
                )
                try:
                    pending = await self.ui.menu(
                        "Session menu",
                        self.cluster_id + " · " + session.state,
                        sections,
                        command=True,
                        help_text="Commands use this authorization. Ending it disconnects clients; "
                        "remote Notebooks remain on the cluster.",
                    )
                except LabError as error:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise self._failure(error) from None
                    if error.code == 130:
                        return
                    raise
            command, pending = pending, ""
            try:
                if command.startswith("command:"):
                    command = command.removeprefix("command:").strip()
                if command in {"exit", "disconnect", "back"}:
                    return
                if command == "status":
                    await self.session_status()
                elif command == "reconnect":
                    await self.ui.work(session.reconnect(), "Reconnecting")
                elif editor_only:
                    raise LabError("Available actions: status, reconnect, disconnect.", 2)
                elif command == "resources":
                    await self.resources()
                elif command.startswith("operation:"):
                    await self.operation(command.removeprefix("operation:"))
                elif command == "operations":
                    await self.operation()
                elif command == "retry":
                    await self.operation(retry=True)
                elif command == "save-preset":
                    await self.operation(save=True)
                elif command:
                    await self.dispatch("notebook status" if command == "inspect" else command)
            except LabError as error:
                error = self._failure(error)
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise error
                if error.code == 130 and not any(
                    (error.data or {}).get(key) for key in ("operation_id", "uid")
                ):
                    continue
                rows = [
                    (key.replace("_", " ").title(), str(value))
                    for key, value in (error.data or {}).items()
                ]
                operation_id = (error.data or {}).get("operation_id")
                actions = [("back", "Session menu")]
                if operation_id:
                    actions.insert(0, ("operation:" + operation_id, "Inspect operation"))
                try:
                    choice = await self.details(
                        "Operation interrupted"
                        if error.code == 130
                        else "Unable to complete action",
                        str(error),
                        rows,
                        actions,
                    )
                    if choice.startswith("operation:"):
                        pending = choice
                except LabError as dismissed:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise self._failure(dismissed) from None
                    if dismissed.code != 130:
                        raise
            except asyncio.CancelledError:
                raise self._failure(
                    LabError("Session interrupted. Remote Notebooks remain on the cluster.", 130)
                ) from None

    async def details(self, *args, **kwargs) -> str:
        """Treat dismissal of a read-only page as navigation back."""
        try:
            return await self.ui.details(*args, **kwargs)
        except LabError as error:
            current = asyncio.current_task()
            if error.code != 130 or (current is not None and current.cancelling()):
                raise
            return "back"

    async def confirm(self, *args, **kwargs) -> bool:
        """Keep cancellation equivalent to declining the proposed mutation."""
        try:
            return await self.ui.confirm(*args, **kwargs)
        except LabError as error:
            current = asyncio.current_task()
            if error.code != 130 or (current is not None and current.cancelling()):
                raise
            return False

    async def session_status(self) -> None:
        session = self.service.api.session
        await self.details(
            "Session status",
            "Keep this session open to retain editor connections.",
            [
                ("Cluster", self.cluster_id),
                ("Connection", session.state),
                ("Time remaining", f"{session.remaining / 3600:.1f} hours"),
            ],
            [("back", "Session menu")],
        )

    async def resources(self) -> None:
        """Seed discovery from this cluster's existing settings and remember additional names."""
        namespace = await self.namespace(
            "Enter a namespace you can read. This helps discovery; "
            "resource totals still include every readable namespace Lab finds."
        )
        self.service.history.remember(self.cluster_id, namespace, {})
        namespaces = self.service.history.candidates(self.cluster_id, "", "namespace")
        with suppress(LabError):
            namespaces.extend(self.service.profile().namespace_rules)
        if configured := self.service.cluster.kubernetes.namespace:
            namespaces.append(configured)
        known = tuple(dict.fromkeys(namespaces))
        remembered = await browse_resources(self.service.api, self.ui, known)
        for namespace in remembered:
            if namespace not in known:
                self.service.history.remember(self.cluster_id, namespace, {})

    async def namespace(
        self, description: str = "History belongs to this cluster. Enter any configured namespace."
    ) -> str:
        values = await self.ui.form(
            "Namespace",
            [
                Field(
                    "namespace",
                    "Namespace",
                    help=NOTEBOOK["namespace"],
                    candidates=self.service.history.candidates(self.cluster_id, "", "namespace"),
                )
            ],
            description=description,
        )
        if not values["namespace"].strip():
            raise LabError("A namespace is required.", 2)
        try:
            return namespace_name(values["namespace"].strip())
        except ValueError as error:
            raise LabError(str(error), 2) from None

    async def dispatch(self, command: str) -> None:
        """Parse typed commands with the production parser and current scope."""
        from lab.cli import parser, validate_arguments

        self.outcome = {}
        try:
            tokens = shlex.split(command)
        except ValueError:
            raise LabError("Invalid command syntax.", 2) from None
        if tokens[:1] == ["lab"]:
            tokens.pop(0)
        if not tokens or any(flag in tokens for flag in ("-h", "--help")) or tokens == ["help"]:
            await self.details(
                "Notebook commands",
                "Menu actions and commands use the same session.",
                [
                    (
                        "Actions",
                        "list · create · status · shell · open · editor-restart · "
                        "start · stop · delete · retry",
                    ),
                    ("Scope", "--namespace NAME; --cluster must match " + self.cluster_id),
                    ("Recovery", "status --operation-id ID; retry --operation-id ID"),
                    ("Access", "shell NAME --pod POD --container CONTAINER; open NAME"),
                    ("Create", "create --preset NAME; editable fields and preview follow"),
                    ("Output", "Use a standalone command for --json."),
                ],
                [("back", "Session menu")],
            )
            return
        if "--version" in tokens:
            raise LabError("Run lab --version outside the interactive session.", 2)
        if tokens[0] != "notebook":
            tokens.insert(0, "notebook")
        args = parser().parse_args(tokens)
        validate_arguments(args, interactive=True)
        if args.cluster and args.cluster != self.cluster_id:
            raise LabError("End this session and authorize another cluster before switching.", 2)
        if args.token_stdin:
            raise LabError(
                "This session is already authorized; --token-stdin is standalone only.", 2
            )
        self._record(args.action, "Pending")
        try:
            operation_id = getattr(args, "operation_id", None)
            if operation_id:
                await self.operation(
                    operation_id,
                    retry=args.action == "retry",
                    reconcile=args.action == "status",
                    yes=args.yes,
                )
                return
            namespace = args.namespace or await self.namespace()
            self._target(namespace, getattr(args, "name", None))
            if args.action == "create":
                args.namespace = namespace
                await self.create(args)
            elif args.action == "list":
                await self.browse(namespace)
            else:
                name = getattr(args, "name", None) or await self.select_notebook(namespace)
                if name:
                    await self.notebook(
                        namespace,
                        name,
                        args.action,
                        getattr(args, "pod", None),
                        getattr(args, "container", None),
                        yes=args.yes,
                        helper_python=getattr(args, "helper_python", "/usr/bin/python3"),
                    )
        except LabError as error:
            raise self._failure(error) from None
        except asyncio.CancelledError:
            raise self._failure(
                LabError("Interrupted. Inspect the operation before retrying.", 130)
            ) from None

    async def discovery(self, namespace: str, values: dict) -> tuple[dict, str]:
        try:
            nodes = await self.ui.work(
                self.service.api.collection("/api/v1/nodes"), "Discovering placement"
            )
        except LabError as error:
            if error.code in {3, 130}:
                raise
            return {}, f"Discovery unavailable: {error}. All fields remain editable."
        try:
            policy = self.service.profile().namespace(namespace)
        except LabError as error:
            return {}, str(error)
        selectors = [policy.placement.cpu_required_selector, policy.placement.gpu_required_selector]
        if str(values.get("gpus", "")).isdigit():
            selectors = [selectors[1 if int(values["gpus"]) > 0 else 0]]
        labels = [
            node["metadata"].get("labels", {})
            for node in nodes
            if any(
                all(
                    node["metadata"].get("labels", {}).get(key) == value
                    for key, value in selector.items()
                )
                for selector in selectors
            )
        ]
        gpu_types = {
            gpu.value
            for gpu in policy.gpu_types
            if any(
                all(label.get(key) == value for key, value in gpu.selector.items())
                for label in labels
            )
        }
        if policy.gpu_defaults:
            key = policy.gpu_defaults.type_label
            gpu_types.update(label[key] for label in labels if key in label)
        return {
            "gpu_type": sorted(gpu_types),
            "node": sorted(
                {
                    label["kubernetes.io/hostname"]
                    for label in labels
                    if "kubernetes.io/hostname" in label
                }
            ),
        }, "Suggestions come from scoped history and cluster placement. Every field is editable."

    async def creation(self, args) -> tuple[NotebookInput, dict | None, Profile] | None:
        namespace = args.namespace
        self.service.profile().namespace(namespace)
        values: dict = {name: getattr(args, name, None) for name, _, _ in FIELDS}
        presets = self.service.history.presets(self.cluster_id, namespace)
        preset_id = args.preset
        if presets and not preset_id:
            preset_id = await self.ui.choose(
                "Creation template",
                [
                    ("", "Empty form with history suggestions"),
                    *[(item["id"], item["name"]) for item in presets],
                ],
                description=f"{self.cluster_id} / {namespace}",
            )
        original = None
        if preset_id:
            original = next(
                (item for item in presets if preset_id in {item["id"], item["name"]}), None
            )
            if original is None:
                raise LabError("Preset not found in this cluster and namespace.", 4)
            values = {
                **original["editable_fields"],
                **{key: value for key, value in values.items() if value is not None},
            }
        while True:
            candidates, description = await self.discovery(namespace, values)
            definitions = []
            for name, label, group in FIELDS:
                history = self.service.history.candidates(self.cluster_id, namespace, name)
                suggestions = list(dict.fromkeys([*history, *candidates.get(name, [])]))
                definitions.append(
                    Field(
                        name,
                        label,
                        str(values[name]) if values.get(name) is not None else "",
                        candidates=suggestions,
                        numeric=name in {"gpus", "cpu", "memory"},
                        group=group,
                        help=NOTEBOOK[name],
                    )
                )
            values = await self.ui.form(
                "Create Notebook",
                definitions,
                description=description,
                submit_label="Review manifest",
            )
            if values.get("namespace") != namespace:
                namespace = values.get("namespace", "")
                original = None
                continue
            fields: dict = {
                key: value for key, value in values.items() if value is not None and value != ""
            }
            try:
                if "gpus" not in fields:
                    raise ValueError
                fields["gpus"] = int(fields["gpus"])
            except ValueError:
                raise LabError(
                    "Enter an integer GPU count explicitly, including zero for CPU.", 2
                ) from None
            try:
                inputs = NotebookInput.model_validate(fields)
            except ValidationError as error:
                locations = sorted({".".join(map(str, item["loc"])) for item in error.errors()})
                raise LabError(
                    "Invalid or missing creation fields: " + ", ".join(locations), 2
                ) from None
            profile = self.service.profile()
            inputs = normalized_inputs(profile, inputs)
            self._target(inputs.namespace, inputs.name)
            if args.yes:
                return inputs, original, profile
            preview = compile_notebook(profile, inputs, "00000000-0000-4000-8000-000000000001")
            choice = await self.details(
                "Create this Notebook?",
                "Review before submission. Back to fields preserves your entries.",
                [("Manifest", yaml.safe_dump(preview, sort_keys=False))],
                [
                    ("back", "Back to fields"),
                    ("create", "Create Notebook"),
                    ("cancel", "Cancel creation"),
                ],
            )
            if choice == "create":
                return inputs, original, profile
            if choice == "cancel":
                return None

    async def create(self, args) -> None:
        draft = await self.creation(args)
        if draft is None:
            return
        inputs, original, profile = draft
        receipt = self.service.prepare(inputs, profile)
        self.originals[receipt["operation_id"]] = original
        self._identity(receipt)
        notebook = await self.ui.work(
            self.service.submit(receipt), "Creating Notebook", rows=self.receipt_rows(receipt)
        )
        self.outcome["data"].update(uid=notebook["metadata"]["uid"], state="Accepted")
        self._record("create", "Accepted")
        await self.wait(receipt, notebook, args.timeout)
        await self.save_preset(self.service.receipt(receipt["operation_id"]), original)
        await self.operation(receipt["operation_id"])

    async def wait(self, receipt: dict, notebook: dict, timeout: float = 300) -> None:
        self._identity({**receipt, "uid": notebook["metadata"]["uid"], "state": "Accepted"})
        await self.ui.work(
            self.service.wait(receipt, notebook, timeout),
            "Waiting for Ready",
            description="Stopping this watch leaves the Notebook on the cluster.",
            rows=self.receipt_rows({**receipt, **self.outcome["data"]}),
            cancel_label="Stop watching",
        )
        self.outcome["data"]["state"] = "Ready"
        self._record("create", "Ready")

    async def save_preset(
        self, receipt: dict, original: dict | None = None, *, explicit: bool = False
    ) -> None:
        try:
            await self._save_preset(receipt, original, explicit=explicit)
        except LabError as error:
            current = asyncio.current_task()
            if error.code != 130 or (current is not None and current.cancelling()):
                raise

    async def _save_preset(self, receipt: dict, original: dict | None, *, explicit: bool) -> None:
        if receipt["state"] != "Ready":
            raise LabError("A Notebook must become Ready before saving its configuration.", 7)
        inputs, namespace = receipt["inputs"], receipt["namespace"]
        repository = self.service.history
        if not explicit and not repository.mark_seen(self.cluster_id, namespace, inputs):
            return
        choices = [("skip", "Do not save"), ("new", "Save as a new preset")]
        if original and original["namespace"] == namespace:
            choices.append(("update", "Update original: " + original["name"]))
        choice = await self.ui.choose(
            "Save this configuration?",
            choices,
            description="Notebook Ready. Presets exclude the instance name.",
        )
        if choice == "skip":
            return
        if choice == "update":
            assert original is not None
            name, preset_id = original["name"], original["id"]
        else:
            fields = await self.ui.form(
                "Save preset", [Field("name", "Preset name", help=SETUP["preset"])]
            )
            name, preset_id = fields["name"].strip(), None
            if not name:
                raise LabError("A preset name is required.", 2)
        repository.save_preset(self.cluster_id, namespace, name, inputs, preset_id)
        await self.details(
            "Preset saved",
            "Reuse it in this cluster and namespace.",
            [("Name", name), ("Namespace", namespace)],
            [("back", "Continue")],
        )

    async def select_notebook(self, namespace: str) -> str | None:
        notebooks = await self.ui.work(self.service.list_notebooks(namespace), "Loading Notebooks")
        if not notebooks:
            await self.details(
                "Notebooks",
                "No Notebooks in this namespace.",
                [("Namespace", namespace)],
                [("back", "Session menu")],
            )
            return None
        return await self.ui.choose(
            "Notebook",
            [(item["metadata"]["name"], item["metadata"]["name"]) for item in notebooks],
            description=namespace,
        )

    async def browse(self, namespace: str) -> None:
        while True:
            notebooks = await self.ui.work(
                self.service.list_notebooks(namespace), "Loading Notebooks"
            )
            self._record(
                "list",
                "ok",
                [
                    {
                        "name": item["metadata"]["name"],
                        "uid": item["metadata"]["uid"],
                        "stopped": STOP_ANNOTATION in item["metadata"].get("annotations", {}),
                    }
                    for item in notebooks
                ],
            )
            by_uid = {item["metadata"]["uid"]: item for item in notebooks}

            async def load(notebooks=notebooks):
                snapshots = await self.service.statuses(namespace, notebooks)
                return {uid: state_label(snapshot["state"]) for uid, snapshot in snapshots.items()}

            choice = await self.ui.status_list(
                "Notebooks",
                f"{self.cluster_id} / {namespace}",
                [(uid, item["metadata"]["name"]) for uid, item in by_uid.items()],
                load,
                [("refresh", "Refresh"), ("back", "Session menu")],
            )
            if choice == "back":
                return
            if choice != "refresh":
                await self.notebook(
                    namespace, by_uid[choice]["metadata"]["name"], expected_uid=choice
                )

    async def notebook(
        self,
        namespace: str,
        name: str,
        action: str = "status",
        pod_name: str | None = None,
        container_name: str | None = None,
        *,
        yes: bool = False,
        receipt: dict | None = None,
        helper_python: str = "/usr/bin/python3",
        expected_uid: str | None = None,
    ) -> None:
        self.outcome = {}
        if receipt is not None:
            self._identity(receipt)
        self._target(namespace, name)
        expected_uid = receipt.get("uid") if receipt else expected_uid
        while action != "back":
            notebook = await self.ui.work(self.service.get(namespace, name), "Loading Notebook")
            uid = notebook["metadata"]["uid"]
            if (expected_uid is not None and uid != expected_uid) or (
                receipt is not None
                and notebook["metadata"].get("labels", {}).get("lab.operations/id")
                != receipt["operation_id"]
            ):
                raise LabError(
                    "This name now belongs to a different Notebook. Return to the list "
                    "to select it explicitly. Nothing was changed.",
                    6,
                )
            expected_uid = uid
            known = self.outcome.get("data", {})
            identity = {
                key: known[key]
                for key in ("operation_id", "state")
                if isinstance(known, dict) and key in known
            }
            self.outcome["data"] = {**identity, "uid": uid}
            if action in {"start", "stop", "delete"}:
                if yes or await self.confirm(
                    action.title() + " Notebook?",
                    f"{namespace}/{name}",
                    [("UID", uid), ("Action", action)],
                ):
                    self.outcome["data"]["action"] = action
                    result = await self.ui.work(
                        self.service.change(notebook, action),
                        action.title() + " Notebook",
                        rows=[("UID", uid)],
                    )
                    self._record(
                        action,
                        "Accepted",
                        {**identity, "uid": uid, "result": result.get("status", "Updated")},
                    )
                    if action == "delete":
                        await self.details(
                            "Deletion accepted",
                            f"{namespace}/{name}",
                            [("UID", uid)],
                            [("back", "Continue")],
                        )
                        return
            elif action == "editor-restart":
                pod, container = await self.target(notebook, pod_name, container_name)

                async def confirm(selected, frame, accepted=yes):
                    return accepted or await self.confirm(
                        "Restart VS Code Server?",
                        editor_recovery.WARNING,
                        [
                            ("Notebook", f"{namespace}/{name}"),
                            ("Container", selected.container),
                            ("VS Code", editor_recovery.SUPPORTED_EDITOR),
                            ("Original PID", str(frame["pid"])),
                        ],
                    )

                recovery = await editor_recovery.restart(
                    self.service,
                    notebook,
                    self.tools,
                    confirm,
                    pod,
                    container,
                    helper_python,
                    work=self.ui.work,
                )
                self._record(action, recovery.status, {**identity, **recovery.data()})
                choices = [("back", "Notebook status")]
                if recovery.status in {"OriginalProcessExited", "AlreadyExited", "NoServer"}:
                    choices.append(("open", "Open in VS Code"))
                choice = await self.details(
                    "VS Code Server",
                    recovery.data()["message"],
                    [("Outcome", recovery.status)]
                    + ([("Cleanup warning", recovery.warning)] if recovery.warning else []),
                    choices,
                )
                if choice == "open" and recovery.target is not None:
                    await editor_recovery.revalidate(self.service, notebook, recovery.target)
                    await self.ui.work(
                        open_editor(self.service, notebook, self.tools, pod, container),
                        "Opening VS Code",
                    )
                    await self.details(
                        "Editor launch requested",
                        "Keep this lab session open to retain its connection.",
                        [],
                        [("back", "Continue")],
                    )
                    await self._run(editor_only=True)
                    return
                if recovery.status == "OutcomeUnknown":
                    return
            elif action in {"shell", "open"}:
                pod, container = await self.target(notebook, pod_name, container_name)
                self.outcome["data"].update(pod=pod, container=container)
                if action == "shell":
                    await release_display()
                    code = await open_shell(self.service, notebook, self.tools, pod, container)
                    if code:
                        raise LabError(f"Notebook shell exited with code {code}.", 7)
                    self._record(action, "ok")
                else:
                    await self.ui.work(
                        open_editor(self.service, notebook, self.tools, pod, container),
                        "Opening VS Code",
                    )
                    self._record(action, "Requested")
                    await self.details(
                        "VS Code window requested",
                        "Keep this lab session open to retain its connection.",
                        [
                            ("Notebook", f"{namespace}/{name}"),
                            ("UID", uid),
                            ("Pod / container", f"{pod} / {container}"),
                        ],
                        [("back", "Notebook status")],
                    )
                pod_name = container_name = None
            status = await self.ui.work(self.service.status(namespace, name), "Inspecting Notebook")
            if status["uid"] != expected_uid:
                raise LabError(
                    "The Notebook was replaced while inspecting it. Return to the list.", 6
                )
            self.outcome["data"] = {**identity, **status}
            self._record("status", "ok")
            containers = (
                notebook.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            )
            rows = [
                ("Namespace", namespace),
                ("Name", name),
                ("UID", status["uid"]),
                ("State", state_label(status["state"])),
            ]
            for container in containers:
                rows.extend(
                    [
                        ("Image", container.get("image", "")),
                        ("Working directory", container.get("workingDir", "/")),
                        (
                            "Startup resources",
                            resource_summary(container.get("resources", {}).get("requests", {})),
                        ),
                    ]
                )
            rows.extend(pod_rows(status["pods"]))
            rows.append(("Connection", status_description(status["state"])))
            actions, disabled = status_actions(status["state"])
            yes = False
            action = await self.details(
                "Notebook status",
                f"{namespace}/{name}",
                rows,
                actions,
                disabled=disabled,
            )

    async def target(
        self, notebook: dict, pod_name: str | None, container_name: str | None
    ) -> tuple[str, str]:
        require_running_request(notebook)
        pods = await self.ui.work(
            owned_pods(self.service.api, notebook), "Finding Ready containers"
        )
        choices = [
            (pod["metadata"]["name"], container["name"])
            for pod in pods
            if pod_state(pod) == "Ready"
            and (pod_name is None or pod["metadata"]["name"] == pod_name)
            for container in pod["spec"]["containers"]
            if container_name is None or container["name"] == container_name
        ]
        if not choices:
            raise LabError("No matching Ready, owned Pod/container. Inspect Notebook status.", 7)
        if len(choices) == 1:
            return choices[0]
        selected = await self.ui.choose(
            "Choose container",
            [
                (str(index), f"{pod} / {container}")
                for index, (pod, container) in enumerate(choices)
            ],
        )
        return choices[int(selected)]

    @staticmethod
    def receipt_rows(receipt: dict) -> list[tuple[str, str]]:
        return [
            ("Operation ID", receipt["operation_id"]),
            ("Namespace", receipt["namespace"]),
            ("Name", receipt["name"]),
            ("State", receipt["state"]),
            ("UID", receipt.get("uid") or "Not yet known"),
        ]

    async def operation(
        self,
        operation_id: str | None = None,
        *,
        retry: bool = False,
        reconcile: bool = False,
        save: bool = False,
        yes: bool = False,
    ) -> None:
        if operation_id is None:
            receipts = [
                item
                for item in self.service.history.history.read()["operations"].values()
                if item["cluster_id"] == self.cluster_id
            ]
            if not receipts:
                await self.details(
                    "Operations",
                    "No creation receipts in this cluster.",
                    [],
                    [("back", "Session menu")],
                )
                return
            operation_id = await self.ui.choose(
                "Creation operation",
                [
                    (
                        item["operation_id"],
                        f"{item['namespace']}/{item['name']} · "
                        f"{item['state']} · {item['operation_id']}",
                    )
                    for item in reversed(receipts)
                ],
            )
        action = "retry" if retry else "reconcile" if reconcile else "save" if save else "inspect"
        while action != "back":
            receipt = self.service.receipt(operation_id)
            self._identity(receipt)
            if action == "retry":
                if yes or await self.confirm(
                    "Retry this operation?",
                    "Reconcile first. Re-submit only if absent and policy still matches.",
                    [
                        *self.receipt_rows(receipt),
                        ("Manifest", yaml.safe_dump(receipt["manifest"], sort_keys=False)),
                    ],
                ):
                    notebook = await self.ui.work(
                        self.service.retry(operation_id),
                        "Reconciling and retrying",
                        rows=self.receipt_rows(receipt),
                    )
                    self.outcome["data"].update(uid=notebook["metadata"]["uid"], state="Accepted")
                    self._record("retry", "Accepted")
            elif action in {"reconcile", "wait"}:
                notebook = await self.ui.work(
                    self.service.reconcile(operation_id),
                    "Reconciling operation",
                    rows=self.receipt_rows(receipt),
                )
                self.outcome["data"].update(uid=notebook["metadata"]["uid"], state="Accepted")
                self._record("status", "Accepted")
                if action == "wait":
                    await self.wait(self.service.receipt(operation_id), notebook)
                    await self.save_preset(
                        self.service.receipt(operation_id), self.originals.get(operation_id)
                    )
            elif action == "save":
                await self.save_preset(receipt, self.originals.get(operation_id), explicit=True)
            elif action == "status":
                await self.notebook(receipt["namespace"], receipt["name"], receipt=receipt)
            receipt = self.service.receipt(operation_id)
            self._identity(receipt)
            actions = [
                ("reconcile", "Reconcile identity"),
                ("status", "Notebook status"),
                ("retry", "Retry this operation"),
            ]
            if receipt["state"] in {"Accepted", "Ready", "RuntimeFailed"}:
                actions.append(("wait", "Wait for Ready"))
            if receipt["state"] == "Ready":
                actions.append(("save", "Save preset"))
            actions.append(("back", "Session menu"))
            yes = False
            action = await self.details(
                "Operation receipt",
                "Recovery uses this original identity and recorded request.",
                self.receipt_rows(receipt),
                actions,
            )
