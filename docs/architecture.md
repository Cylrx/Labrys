# Architecture

Lab separates authorization from connections. `access.Authorization` holds a
1Password client that can resolve the registered clusters its credentials permit.
Each `Session` owns a connection to one cluster. Remote Notebooks have their own
lifetime in Kubernetes, independent of these local objects.

Closing local authorization disconnects its clients without requesting Notebook
shutdown or deletion. Processes attached to an exec terminal can still be affected
by the terminal disconnecting.

## Local access

A session owns both the network route and the credentials used through it.
`transport` provides the route, and `credential` implements kubectl's
ExecCredential helper. Generated kubeconfigs point to that helper; a temporary
client capability lets it request a Kubernetes token from the owning lab process
through a Unix socket. The 1Password Service Account Token stays in lab.

Closing a session invalidates its capabilities and closes its streams. The
expiry clock includes laptop sleep, so suspending the process cannot extend its
authorization. Reconnecting retains the original deadline. Clients must be
trusted because lab cannot revoke a token that a client has already copied.

For `--request-auth`, `auth` creates an expiring request and accepts one token
from a separate terminal. The request file describes the operation; the token
travels through the private socket. This allows a person to authorize a command
without putting a token in its arguments or environment.

## Reusable CLI authorization

`lab session start` publishes a Unix socket in a private temporary directory and
stays in the foreground. The directory and socket are user-owned; both peers
check the Unix user identity. Session IDs are locators, not bearer credentials.
No token is stored in the directory. The local user account and code are trusted.

Each socket handler owns one request. The authorization reads the index once at
approval and each cluster's connection once on first use, then reuses them until it
ends; profiles are read from disk on each request. A kubectl request creates an ordinary `Session` and client
grant, then keeps the socket open until its client exits. The caller runs kubectl
locally with inherited standard streams, so terminals and pipes need no custom
streaming protocol. Separate clients get separate cluster connections, client
grants and streams.

In SSH mode those connections share one forwarder per cluster, owned by the
authorization and bounded by its deadline. The SSH key is therefore used when the
forwarder starts, not once per kubectl command, so an SSH agent that later locks
or asks for confirmation does not interrupt an authorization that is still valid.
If the forwarder exits, the next request restarts it, which uses the key again.
Sharing never crosses authorizations: each `lab session start` owns its own
forwarders and closes them at shutdown.

A forwarder listens on a Unix socket in a private directory rather than a TCP
port, so no other local user can use the route however long it lives. A restart
binds the same path, so sessions that share the forwarder reach its replacement
unchanged. Sessions see a route only as a function that opens a stream to the
Kubernetes endpoint: a direct route opens TCP to the server, an SSH route opens the
socket, and forwarding and health checks are the same for both. The supervisor
runs in its own session, so a signal to the owner's process group (a closed
terminal, a runner timeout) ends only the owner, whose exit the supervisor observes
before closing SSH. kubectl itself connects only over TCP, so each session's relay
still listens on a loopback port while its client runs.

The owner preserves one absolute deadline, the requested duration capped by the
index maximum, across every connection. It also ends when its parent process
exits, which binds authorization to the agent or terminal that requested it.
The parent is recorded before approval is awaited, so a requester that exits
while waiting is refused rather than replaced by whichever process adopts lab. An
optional cluster list hides every other indexed cluster from listing, inspection
and connection. Shutdown stops
new requests, drains connection cleanup, then acknowledges closure and removes
the socket. A cleanup failure ends authorization with an error. These access paths
do not load Notebook history or its encryption key.

## Configuration and creation

`config` validates connection documents, while `policy` loads creation rules by
cluster ID and namespace. Keeping them separate lets users access existing
Notebooks even when a creation profile is missing. Discovery supplies form
suggestions; profiles constrain the manifest; Kubernetes decides whether to
admit the request. These are separate decisions.

`secrets` only reads 1Password. Setup and registration therefore prepare documents
for the user to save in the desktop app, then read them back to verify the result.
The [configuration reference](configuration.md) describes their formats.

`notebooks` records a creation's identity before submitting the manifest through
`kubernetes`. A lost response can mean that Kubernetes accepted the request.
The encrypted receipt lets a later command find that same object or resubmit
only after confirming its absence. A retry preserves the original identity and
checks the current profile. `history` stores these receipts alongside presets
and input history; `storage` handles encryption, locking and atomic replacement.

## Terminal interface

The CLI and interactive interface call the same Notebook services, so choosing a
menu action does not introduce a second implementation of that operation.
`display` keeps one terminal renderer across pages to avoid clearing and
rebuilding the screen between prompts. It releases the terminal when a container
shell takes over. `document` wraps text for display while retaining the original
content for copying, so visual wrapping cannot corrupt a kubeconfig.

`resources` reads node inventory and visible pod requests through the existing
Kubernetes client. It produces a snapshot with explicit namespace coverage and
unknown accounting. `resource_view` renders that snapshot in the session's
existing terminal application, with bounded pages and a native search buffer.
The view requires neither a creation profile nor cluster-specific resource ratios.
The `resources` CLI command uses the same reader through the existing authorization
socket. Its connection is owned by that request and closes on completion,
cancellation or authorization expiry. Only the resource report crosses the socket;
the client receives no Kubernetes credential grant. Text and JSON output consume
the same report, with explicit units, namespace coverage and upper-bound values.

## Editor recovery

Restarting VS Code Server is disabled until an exact image and editor build have
been qualified. Qualification must verify the server layout, interpreter, mounts,
isolation and an independent PID 1 in a disposable environment. Real editor
shutdown, reattachment and effects on running work must also be checked.

The helper holds a Linux pidfd, a kernel handle to one process instance, across
confirmation. That lets it signal the selected instance even if its numeric PID
is later reused. It makes at most one SIGTERM attempt and never escalates to
SIGKILL. Once authorization to signal has been transmitted, connection loss can
leave the outcome unknown; retrying automatically could affect a replacement.
The Linux fixture in `tests/disposable/` exercises the process mechanism with a
synthetic server. It does not establish compatibility with an actual editor build.

## Development

From the checkout:

```sh
uv sync --locked
uv run --locked lab --help
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src/lab
```

The tests use synthetic credentials and simulated APIs. Changes to cluster or
editor integrations also need checks in the intended environment.
Fixtures must use entirely fictional identifiers, device labels and resource
values. Do not copy live output or screenshots into fixtures, even with renamed
prefixes; naming patterns and hardware configurations can identify a deployment.

`docs/manual.md` is the source for the terminal manual. After editing it, regenerate
`man/lab.1` with [Pandoc](https://pandoc.org/installing.html):

```sh
pandoc --standalone --from markdown --to man docs/manual.md -o man/lab.1
```

Include the generated file with the source so users can install the manual
without Pandoc. The wheel's `shared-data` includes both pages under
`share/man/man1` in the Python environment. The
[README](../README.md#documentation) explains how to add that directory to
`MANPATH` when installing with uv.
