# Configuration

Labrys separates connection credentials from Notebook creation rules. Credentials
live in 1Password. Creation rules live in private YAML profiles on this device.
The local bootstrap records where to find both.

Use `lab init` to configure the device and `lab cluster add` to register a cluster.
The [manual](manual.md) describes those workflows. This document defines the
documents they read and write.

## Local bootstrap

`bootstrap.json` contains three fields:

```json
{
  "schema_version": 2,
  "index_ref": "op://example-vault/lab-index/notesPlain",
  "profiles_dir": "/absolute/private/profiles"
}
```

The paths and references here are fictional. `index_ref` identifies the
1Password root index. `profiles_dir` is the absolute directory selected during
setup. lab reads exactly `<profiles_dir>/<cluster-id>.yaml`, independently of the
working directory and installed release.

## Root index

The root index is YAML stored in a 1Password Secure Note:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`. |
| `data_key_ref` | `op://vault/item/field` reference to the local-data encryption key. |
| `session.max_age_seconds` | Positive integer; default `604800` (seven days). This is the absolute session lifetime, including sleep. |
| `clusters` | List of entries containing `id` and `config_ref`. An empty list is valid. |

[examples/index.yaml](../examples/index.yaml) shows a fictional registration.
Initialization creates `clusters: []`. Registration appends an entry while
preserving the encryption-key reference, session settings and other clusters.
Prefer stable vault and item IDs in 1Password references.

A cluster ID contains 1–63 lowercase letters, digits or hyphens, beginning and
ending with a letter or digit. IDs must be unique; `template` is reserved.
Values are used exactly as supplied.

For example, `research-example` is both the value of `--cluster` and the filename
stem of `research-example.yaml`. Local history, presets and creation receipts are
scoped to that ID. Renaming a registration therefore creates a different scope;
lab does not migrate the old history. The kubeconfig context and server address
are separate connection properties.

## Connection note

A cluster's `config_ref` identifies a Secure Note with two top-level fields:

| Field | Meaning |
| --- | --- |
| `kubernetes.context` | Selected context in the imported kubeconfig. |
| `kubernetes.kubeconfig` | Complete inline kubeconfig containing the server, trust configuration and bearer token. |
| `transport` | `{mode: direct}` or `{mode: ssh, ssh_target: configured-host}`. |

An SSH target names an existing SSH configuration entry. It is not a shell
command. [examples/connection.yaml](../examples/connection.yaml) shows a fictional,
nonfunctional note. The note contains connection data; the index supplies the
cluster ID, and the private profile supplies creation rules.

Registration can reuse an existing note, accept a masked kubeconfig paste or
import an explicitly selected file. You save new notes in the 1Password app;
the Service Account only reads them.

Connections support static bearer tokens and HTTPS origins. Inline
`certificate-authority-data` supplies trust when present; otherwise the system
trust store is used. TLS verification remains enabled and `tls-server-name` is
preserved. Executable authentication, auth providers, token/key/CA files, proxy
routing and TLS verification bypasses are rejected. Import does not follow
referenced files.

## Private creation profile

A profile contains only `owner_label_key` and a nonempty `namespace_rules` map.
`owner_label_key` selects the attribution label and cannot use lab's reserved
operation label, `lab.operations/id`. The owner value is a creation input.

Each namespace rule declares:

- Required CPU and GPU node selectors, GPU resource names and type mappings.
- Equal resource requests and limits, with an optional enforced CPU/memory ratio
  per GPU. A supplied quantity must match an enforced ratio.
- Allowed host directory roots, mount restrictions and supplemental `emptyDir`
  mounts.
- Default container startup and exact-image command overrides.
- Registry matching, existing namespaced pull Secret names and the behavior for
  unmatched registries.

Use [profiles/template.yaml](../profiles/template.yaml) for the structure and
`NamespaceRule` in [config.py](../src/lab/config.py) for field definitions. The
template's values are fictional; it is never a runtime fallback. The
[lab-profile Skill](../.agents/skills/lab-profile/SKILL.md) helps prepare a profile
from confirmed infrastructure requirements and validates it with lab's parser.

Keep credentials, addresses and duplicated cluster IDs out of profiles. A pull
Secret name is a reference, so it is allowed; the Secret's content is not.
Container `command` and `args` fields describe startup in the future Pod and are
never executed as local hooks.

Profiles are required for registration, creation and creation retries that
revalidate requirements. They are not required to list or access existing
Notebooks. A rule is selected by exact cluster ID and namespace, without
inheritance or fallback to another environment. Profile validation does not
grant Kubernetes permissions or guarantee that admission will accept a request.

Keep actual profiles private. The checkout ignores files under `profiles/`
except the public template, and package builds include only that template.
An external directory selected during setup also works. Editing a profile does
not require reinstalling lab. On another device, restore the files under the
same registered IDs in that device's configured directory.

## Parsing and validation

YAML documents are limited to 1 MiB and 32 nesting levels. Duplicate keys,
anchors, aliases, explicit tags and unknown fields are rejected. Errors omit
input values. Configuration models are immutable, and secret fields have
redacted representations.

Tools preparing profiles should reuse the runtime parser:

```python
from pathlib import Path

from lab.policy import load_profile, parse_profile

parse_profile(Path("profiles/template.yaml").read_text())
load_profile(Path("/absolute/private/profiles"), "research-example")
```

`lab.config.parse_cluster_note(text)` validates a connection note.
`lab.config.parse_cluster(text, cluster_id)` also binds it to its index identity.
Avoid printing parsed private configuration or secrets during validation.
