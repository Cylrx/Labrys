"""Observed Notebook lifecycle and its shared human-readable presentation."""

from decimal import Decimal, InvalidOperation


def notebook_state(stop_requested: bool, pods: list[dict]) -> str:
    """Summarize owned Pods without treating a stop request as completed shutdown."""
    if stop_requested:
        return "Stopping" if pods else "Stopped"
    if not pods:
        return "Starting"
    states = {pod["state"] for pod in pods}
    if "Ready" in states:
        return "Running"
    if "Unknown" in states:
        return "Unknown"
    if states - {"Ready", "Pending", "Running", "Terminating"}:
        return "Error"
    return "Starting"


def state_label(state: str) -> str:
    return state + ("…" if state in {"Starting", "Stopping"} else "")


def status_description(state: str) -> str:
    return {
        "Stopped": "No running instance. Start the Notebook to connect.",
        "Stopping": "Waiting for Pods to disappear. Resource release is not yet confirmed.",
        "Starting": "Waiting for a Ready instance before connecting.",
        "Running": "Ready to connect.",
        "Error": "The instance is not ready. Inspect the Pod state below.",
        "Unknown": "The instance state is unknown. Refresh before connecting.",
    }[state]


def status_actions(state: str) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Return available navigation and explicit reasons for disabled operations."""
    actions = [
        ("status", "Refresh"),
        ("shell", "Shell"),
        ("open", "Open in VS Code"),
        ("editor-restart", "Restart VS Code Server…"),
        ("start", "Start"),
        ("stop", "Stop"),
        ("delete", "Delete"),
        ("back", "Back"),
    ]
    disabled = {}
    if state != "Running":
        disabled.update(
            dict.fromkeys(("shell", "open", "editor-restart"), "Requires a Ready instance")
        )
    if state == "Stopped":
        actions.sort(key=lambda action: action[0] != "start")
    else:
        disabled["start"] = (
            "Wait until stopped" if state == "Stopping" else "Running is already requested"
        )
    if state in {"Stopped", "Stopping"}:
        disabled["stop"] = "Already stopped" if state == "Stopped" else "Stop already requested"
    return actions, disabled


def pod_rows(pods: list[dict]) -> list[tuple[str, str]]:
    return [("Pod", f"{pod['name']} · {pod['state']}") for pod in pods] or [
        ("Pod", "None — no running instance")
    ]


def resource_summary(requests: dict) -> str:
    """Format configured requests without implying current resource usage."""
    parts = []
    for key, value in requests.items():
        raw = str(value)
        if key == "cpu":
            try:
                cores = Decimal(raw[:-1]) / 1000 if raw.endswith("m") else Decimal(raw)
                raw = format(cores.normalize(), "f") if cores.is_finite() else raw
            except InvalidOperation:
                pass
            parts.append(f"CPU {raw}")
        elif key == "memory":
            for unit, factor in (("Ti", 1024**4), ("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024)):
                if raw.isdigit() and int(raw) and int(raw) % factor == 0:
                    raw = f"{int(raw) // factor} {unit}B"
                    break
                if raw.endswith(unit):
                    raw = f"{raw[: -len(unit)]} {unit}B"
                    break
            parts.append(f"Memory {raw}")
        elif key == "nvidia.com/gpu":
            parts.append(f"GPU {raw}")
        else:
            parts.append(f"{key}: {raw}")
    return " · ".join(parts) or "Not specified"
