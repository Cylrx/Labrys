---
name: lab-profile
description: Prepare or update a private lab Notebook creation profile for a cluster, using confirmed infrastructure requirements and lab's runtime validator. Use before lab cluster add or when creation requirements change.
---

# Lab profile preparation

Deliver a private `<profiles_dir>/<cluster-id>.yaml` file that lab can load, plus
the exact registration command for a new cluster. This Skill is repository-local;
it does not install itself globally or change application source.

Read `profiles/template.yaml`, `docs/configuration.rst`, `src/lab/policy.py` and
`NamespaceRule` in `src/lab/config.py` from the repository root. The public template
is fictional. Reuse its structure, never treat its values as team requirements.

## Establish the facts

Obtain the configured absolute profiles directory from the user's bootstrap or
explicit instruction. Ask the user to choose a meaningful readable cluster name
if they have not supplied one. The name is the unique ID within the index and
must be 1–63 lowercase letters, digits or hyphens, beginning and ending with a
letter or digit; `template` is reserved. Use the exact name for the filename,
registration command and session `--cluster` value. Do not normalize it. For an
existing cluster, use the registered name supplied by the user; do not infer
identity from an API address or kubeconfig context. A different name creates a
different history, preset and receipt scope, without automatic history migration.
Do not overwrite an existing file unless the user requested that update.

Use supplied team documentation, confirmed working manifests and narrowly scoped,
authorized read-only inspection. Registration permission does not authorize broad
cluster or filesystem discovery. Do not inspect other users' resources or Secret
contents. Check available authorized evidence before asking questions. If facts
are missing, conflicting or ambiguous, ask focused questions and explain any
remaining assumptions; do not fill gaps with guesses and declare readiness. Do
not ask again for facts already established.

Confirm namespace, owner label key, required selectors, GPU mappings and resource
ratios, storage roots and restrictions, supplemental mounts, startup behavior,
registry rules and existing pull Secret names. Distinguish requirements from
suggestions; preserve enforced constraints as constraints. Do not infer team
policy from inventory alone or invent a CPU/memory ratio, mount root or registry
permission to make validation pass.

## Write and validate

The only top-level fields are `owner_label_key` and nonempty `namespace_rules`.
Keep IDs, addresses, owner values, controller selection and all credentials out
of the profile. Tokens, kubeconfigs, keys and Secret contents must not enter
chat, logs, public examples or the generated file. Pull Secret names are allowed.
Container `command` and `args` are declarations; never execute them locally.

Use lab's parser from the checkout's existing Python environment. For example,
with the environment's Python interpreter, adapting the directory and chosen name:

```python
from pathlib import Path
from lab.policy import load_profile

load_profile(Path("/absolute/private/profiles"), "research-example")
```

For the public template, call `parse_profile(Path("profiles/template.yaml").read_text())`
from `lab.policy`. Do not duplicate the schema in a custom validator. Do not print
the loaded model or private configuration. Parser success checks configuration,
not live admission, resource availability or creation authorization.

Report the chosen name, absolute file path, source of confirmed requirements,
validation result and any unresolved facts. When ready for new registration, provide:

```console
lab cluster add --cluster-id <chosen-name>
```

Registration is a separate interactive flow. The user copies connection notes and
index updates into the 1Password GUI; the Service Account remains read-only. Do
not write 1Password, run a live registration or create a Notebook merely to verify
this profile. Adding a supported environment needs no source edit or reinstall.
