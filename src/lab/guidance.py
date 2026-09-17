"""Field explanations shared by the live interface and simulated workflows."""

NOTEBOOK = {
    "name": "The name of this Notebook instance inside its namespace. "
    "Use lowercase letters, numbers and hyphens. Example: scene-dev.",
    "namespace": "A workspace in Kubernetes that groups Notebooks and permissions. "
    "It is not a Linux directory or login account. Example: research; use a namespace "
    "your team has assigned to you.",
    "owner": "A label showing who this Notebook belongs to, so teammates can identify it. "
    "It does not create an account, log you in or grant permissions. "
    "Use the attribution name agreed by your team. Example: alex.chen.",
    "image": "The packaged Linux environment to start: its installed tools, Python and libraries. "
    "Enter an image reference, not a website URL or an archive on shared storage. "
    "Example: registry.example.org/team/python:v1.",
    "gpu_type": "The GPU model to request. Suggestions come from cluster discovery, "
    "but you can enter another model name. Use the cluster's exact label value. "
    "Example: NVIDIA-H200. This field is inactive when GPU count is zero.",
    "gpus": "How many GPUs this Notebook reserves while running, even when no training is active. "
    "Enter 0 for a CPU-only Notebook, or a whole number such as 1 or 2.",
    "cpu": "The CPU capacity reserved for this Notebook. Example: 2 means two cores; "
    "500m means half a core. Leave it blank only when your team's GPU ratio supplies it.",
    "memory": "System RAM reserved for this Notebook, not GPU memory or disk space. "
    "Examples: 4Gi or 4096Mi. Leave it blank only when your team's GPU ratio supplies it.",
    "node": "An optional request for a particular server in the cluster. Leave it blank "
    "to let the scheduler choose within the team's placement rules. "
    "Use a discovered server name; a conflicting choice is rejected.",
    "storage_source": "An existing directory on the cluster's storage that you want to access. "
    "This is not a path on your laptop. Example: a project folder beneath your team's "
    "shared-storage root. Creating a Notebook does not create this source directory.",
    "mount_path": "Where that storage directory appears inside the Notebook's Linux filesystem. "
    "For example, mount a shared project folder at /work. It shows the same files; "
    "it does not copy them. Team rules may restrict the destination.",
    "workdir": "The directory where the container's main program starts. It must exist; "
    "this setting does not create or mount a folder. Example: /work, if your storage "
    "is mounted there. It can be the mount path or a directory inside the image.",
}

SETUP = {
    "token": "The access token for your restricted 1Password Service Account. "
    "Paste the token issued when you created that Service Account. This is not your "
    "1Password account password, an SSH key or a Kubernetes token.",
    "reference": "The address of one field in a 1Password item that your Service Account can read. "
    "Example: op://vault-id/item-id/notesPlain. For a saved encryption key, the field is password.",
    "path": "The kubeconfig file on this computer to import. Example: ~/.kube/config. "
    "lab reads the selected file and leaves it unchanged.",
    "target": "The existing host alias you normally put after ssh. "
    "For example, if you use ssh my-vm, enter my-vm here, not the whole command.",
    "title": "A recognizable display name for the item you will save in 1Password. "
    "You can rename it later. Example: lab connection. It is not a password or secret reference.",
    "cluster_name": "The unique readable name used in lab commands, the connection index and "
    "the profile filename. Example: research-h200 uses research-h200.yaml. "
    "Use 1–63 lowercase letters, digits or hyphens, with a letter or digit at each end. "
    "The name template is reserved. Changing this name creates a different local identity.",
    "preset": "A name for this reusable Notebook configuration. "
    "Example: small-gpu-dev. The Notebook instance name is excluded, so you can reuse "
    "the preset to create another instance.",
}
