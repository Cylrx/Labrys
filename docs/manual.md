---
title: LAB
section: 1
header: User Commands
footer: Labrys
date: September 26, 2026
---

# Name

lab - manage Kubeflow notebooks from a terminal

# Synopsis

```text
lab [--cluster CLUSTER]
lab init
lab cluster add|remove [--cluster-id CLUSTER]
lab notebook COMMAND --cluster CLUSTER [OPTIONS]
lab auth --request PATH
lab --help
lab --version
```

`labrys` is an alias for `lab`. Both commands accept the same arguments.

# Description

Labrys connects to a Kubernetes cluster using credentials stored in 1Password.
It can create a Kubeflow Notebook, open a container shell or VS Code window,
and start, stop or delete the Notebook.

Each invocation owns one authorized connection to one cluster. Keep lab running
while using its shell or editor connection. Exiting lab disconnects local clients
but leaves remote Notebooks running. Use `stop` when you want to release a
Notebook's compute resources.

Run `lab` without a subcommand to use the interactive menu. Run
`lab notebook COMMAND` to perform one operation with explicit arguments.

# Setup

Install Labrys and its manual as described in the source checkout's
[README](../README.md). Setup needs the 1Password desktop app and a read-only
Service Account with access to the vault holding lab's index, cluster credentials
and local-data encryption key.

Run `lab init`, enter the Service Account Token in the hidden prompt, and follow
the instructions to create or select the encryption key and connection index.
Choose an absolute directory for private cluster profiles. Because the Service
Account is read-only, you save the proposed documents in the 1Password app;
lab reads them back to verify the content. Copy copies the full document but
does not save it in 1Password.

Prepare `CLUSTER.yaml` in that directory using your cluster's creation rules.
The checkout contains `profiles/template.yaml` and the repository-local
`lab-profile` Agent Skill. The template uses fictional values and must be adapted
to your environment. Register the prepared profile, then open a session:

```sh
lab cluster add --cluster-id research-example
lab --cluster research-example
```

Shell and editor access require `kubectl` on `PATH`. SSH connections also require
`ssh` and an existing SSH host configuration. Editor access requires `code` on
`PATH`; the adapter currently accepts VS Code 1.137.0 with Dev Containers 0.469.0.
Other versions are rejected by the compatibility check.

# Interactive use

The session menu offers Notebook actions and connection controls. Choose an
action or type a Notebook command without the `lab notebook` prefix, for example
`list --namespace research`. Forms ask for missing values and show the manifest
before creation.

Within the menu, `status` describes the lab connection; `notebook status`
describes a Notebook. `disconnect` or `exit` closes the session. After a
connection loss, `reconnect` restores access using the original authorization
deadline. Use another terminal for a second session or cluster.

Tab and Shift-Tab move between controls, Enter advances or submits, and Escape
returns. Tab accepts a grey field suggestion; typing replaces it. Up and Down
select history or suggestions. F1 or Help explains the focused field.

After a new Notebook becomes Ready, you can save its settings as a preset.
Presets exclude the instance name and are scoped to the cluster and namespace.
Loading a preset fills the form; editing the form does not change the saved
preset. The menu's `save-preset` action can also save a completed creation by
operation ID.

# Commands

## init

Configure this device as described under Setup. When selecting an existing
index, lab checks its encryption key against any local encrypted data.

## cluster add

Register a prepared `.yaml` profile. Omit `--cluster-id` to select an unregistered
profile from the configured directory. Unavailable entries explain their error;
Refresh rereads the directory and index.

Choose an existing 1Password connection note, paste a kubeconfig, or import a
kubeconfig file. For imports, select the context and route, review the connection
and save the proposed note in 1Password. lab verifies the connection, then asks
you to save and verify the updated index. Registration does not create a Notebook.

## cluster remove

Remove a registration from the index. Omit `--cluster-id` to select an entry.
Save the proposed index in 1Password and confirm its read-back. Removing a
registration leaves the profile, connection note, local history and remote
Notebooks intact. Existing lab sessions remain authorized.

Avoid concurrent index edits: a manual save and lab's read-back are separate
operations. Recheck repeats verification; Cancel does not undo a saved change.

## notebook list

List Notebooks and their observed state in one namespace.

## notebook create

Create a Notebook from explicit fields or a saved preset. The standalone command
prints a manifest preview and submits it without a confirmation prompt. The
interactive form asks for confirmation.

The default result is `Accepted`, meaning Kubernetes accepted the object.
Use `--wait` to wait for a Ready Pod. A failed or expired wait leaves the Notebook
on the cluster; inspect its status before retrying.

## notebook status [NAME]

Show a Notebook's configured resources, requested lifecycle and observed Pods.
Use `--operation-id` instead of name and namespace to inspect a recorded creation.
Resource values describe requests, not measured usage.

`Running` means an owned Pod is Ready. A stop request remains `Stopping` until
the owned Pods are gone, then becomes `Stopped`. Other states include `Starting`,
`Error` and `Unknown`. A failed API query is reported as an error.

## notebook shell NAME

Open a shell in a Ready container. `--pod` and `--container` resolve ambiguous
targets. A controlling terminal is required. The remote shell is owned by this
lab invocation; start a terminal multiplexer yourself if you need one.

## notebook open NAME

Request a VS Code window attached to a Ready container. The lab process stays in
the foreground to keep the editor connection authorized. Exiting lab leaves the
window open but disconnected. A new authorized session opens a new window.

## notebook start|stop|delete NAME

Start or stop the Notebook through the Kubeflow controller, or delete the Notebook
object. These commands confirm the selected object before changing it. `--yes`
skips that confirmation for this invocation.

Start and stop return when the change is accepted, before the controller finishes.
Use `status` to observe progress. Stopping releases the running Pod while retaining
the Notebook configuration. Deleting also removes that configuration. lab does
not delete the files in the host directory mounted as Notebook data; temporary
container and `emptyDir` data do not survive Pod removal.

## notebook retry

Reconcile a recorded creation using `--operation-id`. If the original object
exists, verify its identity. Submit again only after a confirmed absence and a
check against the current profile. A changed profile or conflicting object blocks
replay. The command asks for confirmation unless `--yes` is supplied.

## notebook editor-restart NAME

This operation is currently unavailable: no image/build profiles are qualified,
so it returns `Unsupported` before remote execution. `--yes` cannot bypass this
check. Ordinary `open` is available with the editor versions listed under Setup.

## auth --request PATH

Supply a token to one waiting `--request-auth` invocation from another terminal.
Run the exact command printed by the requester. Review the displayed operation,
then enter the token in the hidden prompt. This command accepts only `--request`.
It does not create a reusable authorization service.

# Options

Common Notebook options can appear before or after `notebook` or its command.
Standalone Notebook commands require `--cluster` and `--namespace`. Commands
targeting an existing Notebook also require its positional name. An operation ID
supplies name and namespace for `status` and `retry`; omit both in that case.
Inside a session, forms can ask for missing values.

| Option | Meaning |
| ----------------------- | ---------------------------------------------------- |
| `--cluster CLUSTER` | Registered cluster ID. Also selects the cluster for an interactive session. |
| `--namespace NAMESPACE` | Explicit Kubernetes namespace; there is no implicit `default`. |
| `--token-stdin` | Read one UTF-8 token line, at most 16 KiB, from stdin instead of the hidden prompt. |
| `--request-auth` | Wait up to five minutes for a private token handoff from `lab auth`. Mutually exclusive with `--token-stdin`. |
| `--json` | Write one JSON result to stdout. Available for standalone Notebook commands except `shell` and `open`. |
| `--yes`, `-y` | Skip confirmation for start, stop, delete, retry or supported editor restart. Does not bypass validation or authorize later menu actions. |
| `--cluster-id CLUSTER` | Profile or registration to select for `cluster add` or `cluster remove`. |
| `--operation-id UUID` | Creation receipt to inspect with `status` or reconcile with `retry`. Required for `retry`. |
| `--pod POD` | Select a Ready owned Pod for shell, open or editor restart. |
| `--container CONTAINER` | Select a container for shell, open or editor restart. |
| `--helper-python PATH` | Absolute remote interpreter path for editor restart; default `/usr/bin/python3`. |
| `--request PATH` | Explicit rendezvous file printed by the waiting command; required for `auth`. |
| `--help`, `-h` | Show help for the selected command and exit. |
| `--version` | Print the installed version and exit. |

`init` and `cluster` are interactive setup commands. They do not accept the
Notebook authorization, JSON or confirmation options.

## Creation options

Without a preset, supply name, namespace, owner, image, GPU count and all three
paths. Supply CPU and memory unless the profile derives them from the GPU count.
A positive GPU count also requires a GPU type. With a preset, explicit fields
override its saved settings; name and namespace remain required.

| Option | Meaning |
| ----------------------- | ---------------------------------------------------- |
| `--name NAME` | Name of the new Notebook. |
| `--preset PRESET` | Saved preset name or ID in this cluster and namespace. |
| `--owner OWNER` | Attribution label value, not a login account or permission. |
| `--image IMAGE` | Container image reference, such as `registry.example.invalid/research/base:v1`. |
| `--gpus COUNT` | Integer GPU count; use `0` explicitly for CPU-only creation. |
| `--gpu-type TYPE` | GPU type resolved by the private profile's resource and selector rules. |
| `--cpu QUANTITY` | CPU cores or millicores, such as `2` or `2000m`. |
| `--memory QUANTITY` | Memory quantity, such as `4Gi`, `4096Mi` or exact bytes. |
| `--node NODE` | Optional node hostname selector. |
| `--storage-source PATH` | Existing absolute directory on the Kubernetes node, allowed by the profile. |
| `--mount-path PATH` | Absolute path where that directory appears in the container. |
| `--workdir PATH` | Absolute container working directory. lab does not create it. |
| `--wait` | Wait for readiness after API acceptance. |
| `--timeout SECONDS` | Readiness wait limit; default `300`, greater than zero and at most `86400`. The session deadline still applies. |

# Examples

These examples use fictional cluster names, images and paths. Replace them with
values allowed by your private profile.

List Notebooks and inspect one:

```sh
lab notebook list --cluster research-example --namespace research
lab notebook status example-notebook --cluster research-example \
  --namespace research
```

Create a CPU Notebook and wait up to five minutes for readiness:

```sh
lab notebook create --cluster research-example \
  --namespace research \
  --name example-notebook --owner example.researcher \
  --image registry.example.invalid/research/base:v1 \
  --gpus 0 --cpu 2 --memory 4Gi \
  --storage-source /shared/projects/example \
  --mount-path /work --workdir /work \
  --wait --timeout 300
```

Inspect or retry a creation after a lost response, using the operation ID reported
by the original command:

```sh
lab notebook status --cluster research-example \
  --operation-id OPERATION_UUID
lab notebook retry --cluster research-example \
  --operation-id OPERATION_UUID
```

Request JSON output and provide the token privately from a separate terminal:

```sh
lab notebook list --cluster research-example --namespace research \
  --json --request-auth
```

Run the printed authorization command in your own terminal. Tokens should never
appear in command arguments, shell history or chat. For `--token-stdin`, supply
the input through a trusted process or pipe without embedding the token in the
shell command.

# Output and exit status

Human-readable output uses tables and status messages. With `--json`, stdout
contains one object with `schema_version: 1`. Previews and diagnostics use stderr.
Successful results contain `operation`, `status`, `target` and `data`. Errors
contain `status: "error"`, `error`, `code`, `target` and `data`.

Creation results include an `operation_id` and, when known, the server object's
`uid`. Keep them after an interrupted request: failure to receive or save a
response does not mean the Notebook was not created.

| Code | Meaning |
| ----- | ---------------------------------------------------------------------- |
| `0` | The requested command completed. `Accepted` does not imply readiness. |
| `2` | Invalid arguments or missing required input. |
| `3` | Authorization, 1Password access or session expiry error. |
| `4` | Configuration, policy or session precondition failed. |
| `5` | Request interrupted or operation outcome unknown. Inspect before retrying. |
| `6` | Kubernetes rejected the request, returned invalid data, or editor recovery refused the target. |
| `7` | Readiness, container access or process completion failed or timed out. |
| `8` | Local storage, cleanup or internal error. |
| `9` | Required tool, platform or editor capability is unavailable or unsupported. |
| `130` | Cancelled or interrupted. A remote operation may already have taken effect. |

# Files and authorization

On macOS, lab stores `bootstrap.json`, `presets.enc` and `history.enc` under
`~/Library/Application Support/lab/`. On Linux the defaults are
`~/.config/lab/bootstrap.json`, `~/.local/share/lab/presets.enc` and
`~/.local/state/lab/history.enc`; the corresponding XDG directory variables apply.
Creation receipts are part of encrypted history. Private profiles live in the
absolute directory recorded by setup.

The bootstrap contains references and the profiles directory. Credentials remain
in 1Password; the Service Account Token is held by the authorized process and is
not saved. Each session has an absolute lifetime, including time spent asleep,
configured by `session.max_age_seconds` in the index. There is no idle timeout.

Shell and editor clients receive a temporary credential capability tied to the
live lab process. They are trusted local clients. Disconnecting blocks lab's
routes and grants; it cannot revoke a cluster token that another client has
already copied. The local user account, installed code, terminal and editor
extensions must be trusted.

# Troubleshooting

**No clusters are registered:** prepare a private profile and run `lab cluster add`.
Repeating `lab init` is unnecessary when the index is already configured.

**Profile missing or invalid:** check the configured directory, the exact cluster
ID and the `.yaml` extension. Creation and creation retry need a valid profile;
listing and accessing existing Notebooks do not.

**1Password read-back differs:** save the complete proposed document in the
correct item, then use Recheck. Inspect the saved index before retrying a
cancelled verification.

**Copy is unavailable:** install `wl-copy`, `xclip` or `xsel` on Linux. macOS uses
its built-in `pbcopy`. You can also select and copy the document manually.

**Notebook is not Ready:** inspect status and the reported Pod state. A readiness
timeout does not delete or stop the Notebook. If creation's outcome is uncertain,
use its operation ID to reconcile before creating anything else.

**Editor disconnected:** keep the owning lab process running. Use Reconnect after
a connection loss while authorization is still valid. After exiting lab or
expiry, authorize a new session and open a new editor window.

**Local encrypted data cannot be read:** preserve the files and check the selected
encryption key and file permissions. lab does not reset corrupt or wrong-key data.

**Manual not found:** follow the manual installation instructions in the
checkout's README, then check `man -w lab`.

# See also

`lab --help`, `lab notebook COMMAND --help`, `kubectl(1)`, `ssh(1)`.

The source checkout includes `docs/configuration.md` for configuration formats
and `docs/architecture.md` for the implementation and development workflow.
