"""English command interface; each invocation owns exactly one authorized session."""

import argparse
import asyncio
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from prompt_toolkit.output import create_output
from pydantic import TypeAdapter, ValidationError

from lab import __version__, auth, clock, editor_recovery, presentation
from lab.config import cluster_name, dns_name, namespace_name, parse_cluster, parse_index
from lab.display import display_session
from lab.editor import open_editor, open_shell
from lab.errors import LabError
from lab.guidance import SETUP
from lab.history import Repository
from lab.inputs import choose, controlling_terminal, terminal_required
from lab.kubernetes import Kubernetes
from lab.manifest import NotebookInput, compile_notebook, normalized_inputs
from lab.notebooks import Notebooks
from lab.policy import Profile
from lab.registration import add_cluster, remove_cluster
from lab.secrets import Secrets
from lab.session import Session, Tools
from lab.setup import data_key, initialize
from lab.storage import Paths, read_bootstrap
from lab.tools import Toolchain, protect_process
from lab.ui import Field, Screens

CREATION_FIELDS = {
    "name": "Name of the new Notebook",
    "namespace": "Explicit Kubernetes namespace",
    "image": "Container image reference",
    "gpu_type": "GPU type from the private profile's resource and selector rules",
    "gpus": "Integer GPU count; use 0 for a CPU Notebook",
    "cpu": "CPU cores or millicores, for example 2 or 2000m",
    "memory": "Memory quantity, for example 4Gi or 4096Mi",
    "node": "Optional node hostname selector",
    "storage_source": "Existing absolute directory on the Kubernetes node",
    "mount_path": "Absolute container path for the data mount",
    "workdir": "Absolute container working directory; lab does not create it",
    "owner": "Attribution label value, not a login account",
}
ACTIONS = {
    "list": "List Notebooks and their observed state",
    "create": "Preview and submit a new Notebook",
    "status": "Inspect a Notebook or recorded creation",
    "shell": "Open a shell in a Ready container",
    "open": "Open VS Code and keep its connection authorized",
    "start": "Request that a stopped Notebook start",
    "stop": "Stop a Notebook while retaining its configuration",
    "delete": "Delete the Notebook object",
    "retry": "Reconcile and retry a recorded creation",
    "editor-restart": "Stop the original VS Code Server; requires a qualified image/build",
}


class Parser(argparse.ArgumentParser):
    def format_help(self):
        return super().format_help() + "\nFor more information, run 'man lab'.\n"

    def error(self, message):
        raise LabError("Invalid command or option. Run lab --help for the command syntax.", 2)

    def parse_args(self, args=None, namespace=None):
        result = super().parse_args(args, namespace)
        for key, default in {
            "cluster": None,
            "token_stdin": False,
            "request_auth": False,
            "json": False,
            "yes": False,
        }.items():
            if not hasattr(result, key):
                setattr(result, key, default)
        return result


def parser() -> Parser:
    common = Parser(add_help=False)
    common.add_argument(
        "--cluster",
        default=argparse.SUPPRESS,
        help="Registered cluster ID; required for standalone Notebook commands",
    )
    authentication = common.add_mutually_exclusive_group()
    authentication.add_argument(
        "--token-stdin",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Read one Service Account Token line from stdin (maximum 16 KiB)",
    )
    authentication.add_argument(
        "--request-auth",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Wait for one hidden token handoff from lab auth",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write a JSON result; unavailable for menus, shell and open",
    )
    common.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Skip confirmation for this action; validation still applies",
    )
    root = Parser(
        prog="lab",
        description="Manage Kubeflow notebooks. Run without a subcommand for the interactive menu.",
        epilog="Exiting lab disconnects local clients and leaves Notebooks running.",
        parents=[common],
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command")
    sender = commands.add_parser("auth", help="Authorize an explicit waiting lab process")
    sender.add_argument(
        "--request", required=True, help="Request file printed by the waiting lab command"
    )
    commands.add_parser("init", help="Set up the root reference with the 1Password GUI")
    cluster = commands.add_parser("cluster", help="Register cluster connections")
    cluster_actions = cluster.add_subparsers(dest="action", required=True)
    add = cluster_actions.add_parser("add", help="Bind a prepared private profile")
    add.add_argument("--cluster-id", help="Name of a prepared profile, for example research-h200")
    remove = cluster_actions.add_parser("remove", help="Remove a connection from the index")
    remove.add_argument(
        "--cluster-id", help="Registered cluster name; omit to select from the list"
    )
    notebook = commands.add_parser("notebook", help="Manage Kubernetes Notebooks", parents=[common])
    actions = notebook.add_subparsers(dest="action", required=True)
    for action, description in ACTIONS.items():
        sub = actions.add_parser(
            action, help=description, description=description, parents=[common]
        )
        sub.add_argument("--namespace", help=CREATION_FIELDS["namespace"])
        if action not in {"list", "create", "retry"}:
            sub.add_argument("name", nargs="?", help="Notebook name")
        if action in {"status", "retry"}:
            sub.add_argument(
                "--operation-id", help="Creation receipt UUID; replaces name and --namespace"
            )
        if action == "create":
            sub.add_argument(
                "--preset", help="Saved preset name or ID in this cluster and namespace"
            )
            for name, help_text in CREATION_FIELDS.items():
                if name != "namespace":
                    sub.add_argument(
                        "--" + name.replace("_", "-"),
                        type=int if name == "gpus" else str,
                        help=help_text,
                    )
            sub.add_argument(
                "--wait", action="store_true", help="Wait for readiness after API acceptance"
            )
            sub.add_argument(
                "--timeout",
                type=float,
                default=300,
                help="Readiness wait in seconds with --wait (default: 300; range: >0 to 86400)",
            )
            sub.epilog = (
                "Submits without confirmation. A readiness timeout leaves the Notebook on "
                "the cluster."
            )
        if action in {"shell", "open", "editor-restart"}:
            sub.add_argument("--pod", help="Select a Ready Pod owned by this Notebook")
            sub.add_argument("--container", help="Select one container in the Pod")
        if action == "editor-restart":
            sub.add_argument(
                "--helper-python",
                default="/usr/bin/python3",
                help="Absolute remote Python path (default: /usr/bin/python3)",
            )
    return root


def validate_arguments(args, *, interactive=False) -> None:
    if args.command == "auth":
        if args.json or args.token_stdin or args.request_auth or args.cluster or args.yes:
            raise LabError("lab auth accepts only an explicit --request path.", 2)
        return
    if args.token_stdin and args.request_auth:
        raise LabError("--request-auth and --token-stdin are mutually exclusive.", 2)
    if interactive and args.request_auth:
        raise LabError("--request-auth is standalone only.", 2)
    if args.command in {"init", "cluster"} and (args.cluster or args.yes or interactive):
        raise LabError("Configuration commands run independently without --cluster or --yes.", 2)
    if args.command in {None, "init", "cluster"}:
        if args.json or args.token_stdin or args.request_auth:
            raise LabError(
                "Interactive setup/session requires a terminal, without --json or --token-stdin.", 2
            )
        return
    if not interactive and not args.cluster:
        raise LabError("Supply --cluster explicitly for a standalone command.", 2)
    if interactive and args.json:
        raise LabError("Use a standalone command for --json output.", 2)
    if args.action in {"shell", "open"} and args.json:
        raise LabError("Interactive shell/editor operations do not support --json.", 2)
    operation_id = getattr(args, "operation_id", None)
    if operation_id:
        if args.namespace or getattr(args, "name", None):
            raise LabError("--operation-id supplies its own name and namespace.", 2)
    elif args.action == "retry":
        raise LabError("retry requires --operation-id.", 2)
    elif not interactive:
        if not args.namespace:
            raise LabError("Supply --namespace explicitly.", 2)
        if args.action != "list" and not getattr(args, "name", None):
            raise LabError("Supply the Notebook name explicitly.", 2)
    if args.action == "create" and (
        not math.isfinite(args.timeout) or args.timeout <= 0 or args.timeout > 86400
    ):
        raise LabError("--timeout must be greater than zero and at most 86400 seconds.", 2)

    try:
        for key in ("name", "namespace", "pod", "container"):
            value = getattr(args, key, None)
            if value is not None:
                (namespace_name if key == "namespace" else dns_name)(value)
        if args.cluster is not None:
            cluster_name(args.cluster)
    except ValueError:
        raise LabError("Invalid cluster, namespace, or Notebook/container selector.", 2) from None
    helper = getattr(args, "helper_python", None)
    if helper is not None and (
        not PurePosixPath(helper).is_absolute()
        or ".." in PurePosixPath(helper).parts
        or any(ord(c) < 33 or ord(c) > 126 for c in helper)
    ):
        raise LabError("--helper-python must be an explicit absolute interpreter path.", 2)
    if args.action == "create" and not interactive:
        fields = {key: getattr(args, key, None) for key in CREATION_FIELDS}
        values = {key: value for key, value in fields.items() if value is not None}
        try:
            if args.preset:
                for key, value in values.items():
                    TypeAdapter(
                        NotebookInput.model_fields[key].rebuild_annotation()
                    ).validate_python(value)
            else:
                if args.gpus is None:
                    raise ValueError
                NotebookInput.model_validate(values)
        except (ValueError, ValidationError):
            raise LabError(
                "Invalid or missing creation fields; inspect lab notebook create --help.", 2
            ) from None


def emit(operation: str, status: str, target=None, data=None, *, machine=False) -> dict:
    result = {
        "schema_version": 1,
        "operation": operation,
        "status": status,
        "target": target,
        "data": data,
    }
    if not machine:
        presentation.result(operation, status, target, data)
    return result


async def read_token(from_stdin: bool) -> str:
    if not from_stdin:
        terminal_required()
        values = await Screens(
            context="lab / authorization", output=create_output(sys.stderr)
        ).form(
            "Authorize lab",
            [Field("token", "Service Account Token", secret=True, help=SETUP["token"])],
            description=(
                "Use the restricted 1Password Service Account Token. It stays in this process only."
            ),
            submit_label="Authorize",
            cancel_label="Exit",
        )
        return values["token"]
    raw = sys.stdin.buffer.readline(16386)
    if len(raw) > 16385:
        raise LabError("Service Account Token input exceeds 16 KiB.", 3)
    try:
        value = raw.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8")
    except UnicodeError:
        raise LabError("Token input must be UTF-8.", 3) from None
    if not value or len(value.encode()) > 16384:
        raise LabError("Service Account Token input is empty or too long.", 3)
    return value


async def permission(message: str, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise LabError("This action requires confirmation. Supply --yes for a scripted request.", 2)
    if not await Screens().confirm("Confirm action", message, []):
        raise LabError("Cancelled.", 130)


@dataclass
class Application:
    service: Notebooks
    tools: Toolchain
    outcome: dict = field(default_factory=dict)
    result: dict | None = None

    def creation(self, args, profile: Profile) -> NotebookInput:
        fields = {name: getattr(args, name, None) for name in CREATION_FIELDS}
        if args.preset:
            presets = self.service.history.presets(
                str(self.service.cluster.cluster_id), args.namespace
            )
            preset = next(
                (item for item in presets if args.preset in {item["id"], item["name"]}), None
            )
            if preset is None:
                raise LabError("Preset not found in this cluster and namespace.", 4)
            fields = {
                **preset["editable_fields"],
                **{key: value for key, value in fields.items() if value is not None},
            }
        values = {key: value for key, value in fields.items() if value is not None and value != ""}
        if "gpus" not in values:
            raise LabError("Enter a GPU count explicitly, including zero for a CPU Notebook.", 2)
        try:
            inputs = NotebookInput.model_validate(values)
        except ValidationError as error:
            locations = sorted({".".join(map(str, item["loc"])) for item in error.errors()})
            raise LabError(
                "Invalid or missing creation fields: " + ", ".join(locations), 2
            ) from None
        return normalized_inputs(profile, inputs)

    async def dispatch(self, args) -> None:
        self.outcome = {}
        try:
            await self._dispatch(args)
        except LabError as error:
            error.target = error.target or self.outcome.get("target")
            error.data = error.data or self.outcome.get("data")
            outcome = (error.data or {}).get("outcome")
            if args.action == "editor-restart" and outcome in {
                "OriginalProcessExited",
                "AlreadyExited",
                "NoServer",
            }:
                self.outcome["data"] = error.data
                self.result = emit(
                    args.action, outcome, error.target, error.data, machine=args.json
                )
                return
            raise
        except asyncio.CancelledError:
            raise LabError(
                "Interrupted. Inspect the operation before retrying.",
                130,
                target=self.outcome.get("target"),
                data=self.outcome.get("data"),
            ) from None

    async def _dispatch(self, args) -> None:
        validate_arguments(args)
        service = self.service
        session = service.api.session
        action = args.action
        operation_id = getattr(args, "operation_id", None)
        if operation_id:
            receipt = service.receipt(operation_id)
            namespace, name = receipt["namespace"], receipt["name"]
        else:
            namespace = args.namespace
            args.namespace = namespace
            name = getattr(args, "name", None)
        target = {"cluster": self.service.cluster.cluster_id, "namespace": namespace, "name": name}
        self.outcome["target"] = target
        if action == "create":
            profile = service.profile()
            inputs = self.creation(args, profile)
            target.update(namespace=inputs.namespace, name=inputs.name)
            preview = compile_notebook(profile, inputs, "00000000-0000-4000-8000-000000000001")
            presentation.preview(preview)
            receipt = service.prepare(inputs, profile)
            self.outcome["data"] = {"operation_id": receipt["operation_id"], "state": "Prepared"}
            notebook = await service.submit(receipt)
            data = {"operation_id": receipt["operation_id"], "uid": notebook["metadata"]["uid"]}
            self.outcome["data"] = {**data, "state": "Accepted"}
            if args.wait:
                if not args.json:
                    presentation.success("Notebook accepted")
                    presentation.note(f"Operation {receipt['operation_id']}")
                    with presentation.console(stderr=True).status(
                        "Waiting for the Notebook to become Ready…", spinner="dots"
                    ):
                        await service.wait(receipt, notebook, args.timeout)
                else:
                    await service.wait(receipt, notebook, args.timeout)
                self.result = emit(action, "Ready", target, data, machine=args.json)
            else:
                self.result = emit(action, "Accepted", target, data, machine=args.json)
            return
        if action == "list":
            notebooks = await service.list_notebooks(namespace)
            snapshots = await service.statuses(namespace, notebooks)
            entries = [
                {"name": item["metadata"]["name"], **snapshots[item["metadata"]["uid"]]}
                for item in notebooks
            ]
            self.result = emit(action, "ok", target, entries, machine=args.json)
            return
        if action == "retry":
            presentation.preview(receipt["manifest"])
            await permission(
                f"Reconcile and, only if absent, retry {namespace}/{name} ({operation_id})?",
                args.yes,
            )
            assert operation_id is not None
            notebook = await service.retry(operation_id)
            self.result = emit(
                action, "Accepted", target, {"uid": notebook["metadata"]["uid"]}, machine=args.json
            )
        elif action == "status":
            if operation_id:
                await service.reconcile(operation_id)
            self.result = emit(
                action, "ok", target, await service.status(namespace, name), machine=args.json
            )
        else:
            notebook = await service.get(namespace, name)
            if action in {"stop", "delete", "start"}:
                await permission(
                    f"{action.title()} {namespace}/{name} (UID {notebook['metadata']['uid']})?",
                    args.yes,
                )
                result = await service.change(notebook, action)
                self.result = emit(
                    action,
                    "Accepted",
                    target,
                    {"uid": notebook["metadata"]["uid"], "result": result.get("status", "Updated")},
                    machine=args.json,
                )
            elif action == "editor-restart":

                async def confirm(selected, frame):
                    await permission(
                        f"{selected.namespace}/{selected.notebook} · {selected.container} · "
                        f"VS Code {editor_recovery.SUPPORTED_EDITOR}\n\n" + editor_recovery.WARNING,
                        args.yes,
                    )
                    return True

                recovery = await editor_recovery.restart(
                    service,
                    notebook,
                    self.tools,
                    confirm,
                    args.pod,
                    args.container,
                    args.helper_python,
                )
                self.outcome["data"] = recovery.data()
                if editor_recovery.CODES[recovery.status]:
                    raise LabError(
                        recovery.data()["message"],
                        editor_recovery.CODES[recovery.status],
                        target=target,
                        data=recovery.data(),
                    )
                self.result = emit(
                    action, recovery.status, target, recovery.data(), machine=args.json
                )
                return
            elif action == "shell":
                code = await open_shell(service, notebook, self.tools, args.pod, args.container)
                if code:
                    raise LabError(f"Notebook shell exited with code {code}.", 7)
            elif action == "open":
                await open_editor(service, notebook, self.tools, args.pod, args.container)
                presentation.success("VS Code window requested")
                presentation.note("Keep this lab session open to retain its connection.")
                await self.menu(editor_only=True)
        session.check()

    async def menu(self, *, editor_only=False) -> None:
        from lab.interface import Interactive

        interface = Interactive(self.service, self.tools)
        try:
            await interface.run(editor_only=editor_only)
        finally:
            self.outcome = interface.outcome
            self.result = interface.result


def completed_recovery(value) -> bool:
    return isinstance(value, editor_recovery.Result) or (
        isinstance(value, dict)
        and value.get("operation") == "editor-restart"
        and value.get("status") in editor_recovery.CODES
    )


async def until_deadline(deadline: float, operation):
    """Bound every post-index interaction by the authorization deadline."""
    task = asyncio.create_task(operation)
    try:
        if clock.now() >= deadline:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise LabError("The authorization session has expired.", 3)
        while not task.done():
            await asyncio.wait({task}, timeout=0.2)
            if not task.done() and clock.now() >= deadline:
                task.cancel()
                results = await asyncio.gather(task, return_exceptions=True)
                result = results[0]
                if completed_recovery(result):
                    return result
                if (
                    isinstance(result, LabError)
                    and (result.data or {}).get("outcome") in editor_recovery.CODES
                ):
                    raise result
                raise LabError(
                    "The session expired. Remote Notebooks remain running.",
                    3,
                    target=getattr(result, "target", None),
                    data=getattr(result, "data", None),
                )
        return task.result()
    except asyncio.CancelledError:
        task.cancel()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if isinstance(result, LabError):
            raise result from None
        if completed_recovery(result):
            return result
        raise
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def run(args) -> dict | None:
    validate_arguments(args)
    native = args.command in {None, "init", "cluster"}
    if native:
        terminal_required()
    async with display_session(enabled=native):
        return await run_command(args)


async def run_command(args) -> dict | None:
    validate_arguments(args)
    if args.command == "auth":
        raise LabError("Run lab auth directly in a separate terminal.", 2)
    paths = Paths()
    native = args.command in {None, "init", "cluster"}
    screens = Screens(context="lab / setup")
    bootstrap = read_bootstrap(paths)
    if bootstrap is None and args.command != "init":
        if args.command is not None:
            raise LabError("Run 'lab init' to configure this device first.", 4)
        action = await screens.details(
            "Set up this device",
            "No connection index is configured yet.",
            [],
            [("setup", "Start setup"), ("exit", "Exit")],
        )
        if action == "exit":
            return None
        args.command = "init"
    if args.request_auth and args.action in {"shell", "open"}:
        try:
            with open("/dev/tty", "r+b", buffering=0):
                pass
        except OSError:
            raise LabError(
                "A controlling terminal is required for Notebook shell/editor access.", 2
            ) from None
    token = (
        await auth.request_token(auth.description(args))
        if args.request_auth
        else await read_token(args.token_stdin)
    )
    with controlling_terminal(
        (args.token_stdin or args.request_auth) and getattr(args, "action", None) == "open"
    ):
        if native:
            secrets = await screens.work(
                Secrets.authenticate(token),
                "Authorizing lab",
                "Connecting with the restricted Service Account.",
            )
        else:
            secrets = await Secrets.authenticate(token)
        token = ""
        authorized_at = clock.now()
        if args.command == "init":
            await initialize(secrets, paths)
            await screens.details(
                "Setup complete",
                "Prepare a private cluster profile, then run lab cluster add to register it.",
                [],
                [("done", "Done")],
            )
            return None
        assert bootstrap is not None
        if native:
            raw_index = await screens.work(
                secrets.read(bootstrap["index_ref"]), "Reading connection index"
            )
        else:
            raw_index = await secrets.read(bootstrap["index_ref"])
        index = parse_index(raw_index)
        deadline = authorized_at + index.session.max_age_seconds
        secrets.deadline = deadline
        if args.command == "cluster":
            await until_deadline(
                deadline,
                add_cluster(secrets, bootstrap, args.cluster_id, authorized_at=authorized_at)
                if args.action == "add"
                else remove_cluster(secrets, bootstrap, args.cluster_id),
            )
            return None
        if not index.clusters:
            message = "No clusters are registered. Prepare a profile, then run lab cluster add."
            if args.command is None:
                await screens.details("No registered clusters", message, [], [("exit", "Exit")])
                return None
            raise LabError(message, 4)
        return await until_deadline(
            deadline,
            run_authorized(
                args, index, authorized_at, secrets, paths, Path(bootstrap["profiles_dir"])
            ),
        )


async def run_authorized(
    args, index, authorized_at, secrets: Secrets, paths: Paths, profiles_dir: Path
) -> dict | None:
    cluster_id = args.cluster
    if not cluster_id:
        if args.command is not None:
            raise LabError("Supply --cluster explicitly for a standalone command.", 2)
        cluster_id = await choose("Cluster", [(item.id, item.id) for item in index.clusters])
    entry = next((item for item in index.clusters if item.id == cluster_id), None)
    if entry is None:
        raise LabError("Cluster name is not in the selected root index.", 4)
    screens = Screens(context=f"lab / {cluster_id}")
    if args.command is None:
        cluster_text = await screens.work(
            secrets.read(entry.config_ref), "Reading cluster configuration"
        )
        key_text = await screens.work(secrets.read(index.data_key_ref), "Reading local data key")
    else:
        cluster_text = await secrets.read(entry.config_ref)
        key_text = await secrets.read(index.data_key_ref)
    cluster = parse_cluster(cluster_text, entry.id)
    history = Repository(paths, data_key(key_text))
    key_text = ""
    history.verify()
    tools = Toolchain.load()
    session = Session(
        cluster.connection,
        cluster.transport,
        index.session.max_age_seconds,
        Tools(tools.ssh, tools.python, tools.credential),
        cluster_id=entry.id,
        authorized_at=authorized_at,
    )
    application = Application(Notebooks(Kubernetes(session), cluster, history, profiles_dir), tools)
    try:
        if args.command is None:
            await screens.work(session.connect(), "Connecting to cluster", cluster_id)
            await application.menu()
        else:
            await session.connect()
            await application.dispatch(args)
    except LabError as error:
        error.target = error.target or application.outcome.get("target")
        error.data = error.data or application.outcome.get("data")
        raise
    finally:
        try:
            await session.close()
        except LabError as error:
            error.target = error.target or application.outcome.get("target")
            error.data = error.data or application.outcome.get("data")
            raise
    return application.result


def main() -> None:
    machine = "--json" in sys.argv
    try:
        protect_process()
        args = parser().parse_args()
        validate_arguments(args)
        if args.command == "auth":
            auth.send_token(args.request)
            return
        result = asyncio.run(run(args))
        if machine and result is not None:
            print(json.dumps(result, ensure_ascii=True))
    except LabError as error:
        if machine:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "error",
                        "error": str(error),
                        "code": error.code,
                        "target": error.target,
                        "data": error.data,
                    }
                )
            )
        else:
            presentation.error(str(error))
            if error.data:
                presentation.details(
                    [
                        (key.replace("_", " ").title(), str(value))
                        for key, value in error.data.items()
                    ],
                    stderr=True,
                )
        raise SystemExit(error.code) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        message = "lab encountered an internal error. No automatic retry was attempted."
        if machine:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "error",
                        "code": 8,
                        "error": message,
                        "target": None,
                        "data": None,
                    }
                )
            )
        else:
            presentation.error(message)
        raise SystemExit(8) from None


if __name__ == "__main__":
    main()
