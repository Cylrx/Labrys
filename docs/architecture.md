# Architecture

A remote Notebook and a local lab session have independent lifetimes. Kubernetes
keeps the Notebook until someone stops or deletes it. A `Session` authorizes
local access to one cluster until disconnection or expiry. This is why exiting
lab disconnects its shell and editor clients without stopping their Notebook.

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
