"""Human output preserves data literally and keeps secrets out of Rich recordings."""

from io import StringIO

from rich.console import Console

from lab import presentation


def test_secret_value_is_exact_and_bypasses_rich_recording(monkeypatch, capsys):
    output = Console(file=StringIO(), width=20, record=True, theme=presentation.THEME)
    monkeypatch.setattr(presentation, "console", lambda **kwargs: output)
    value = "synthetic-key-value-that-is-longer-than-twenty-columns="
    presentation.secret_value(value)
    assert capsys.readouterr().out == value + "\n"
    assert value not in output.export_text()


def test_item_title_is_literal_and_has_a_visible_purpose(monkeypatch):
    stream = StringIO()
    output = Console(file=stream, width=80, color_system=None, theme=presentation.THEME)
    monkeypatch.setattr(presentation, "console", lambda **kwargs: output)
    presentation.item_guide(
        purpose="Local data encryption key",
        description="Encrypts local history and presets.",
        title="My [red]literal[/red] title",
        category="Password",
        field="password",
    )
    text = stream.getvalue()
    assert "Encrypts local history and presets" in text
    assert "My [red]literal[/red] title" in text


def test_runtime_console_never_records_output():
    assert presentation.console().record is False


def test_human_status_uses_observed_state_and_explicit_empty_pod(monkeypatch):
    stream = StringIO()
    monkeypatch.setattr(
        presentation,
        "console",
        lambda **kwargs: Console(file=stream, width=100, theme=presentation.THEME),
    )
    presentation.result(
        "status",
        "ok",
        None,
        {"uid": "synthetic-uid", "stopped": True, "state": "Stopped", "pods": []},
    )
    text = stream.getvalue()
    assert "No running instance" in text or "no running instance" in text
    assert "Stopped" in text and "True" not in text
    stream.seek(0)
    stream.truncate()
    presentation.result(
        "status",
        "ok",
        None,
        {
            "uid": "synthetic-uid",
            "stopped": True,
            "state": "Stopping",
            "pods": [{"name": "example-0", "state": "Terminating"}],
        },
    )
    assert "Stopping" in stream.getvalue() and "Terminating" in stream.getvalue()
    assert "Stopped" not in stream.getvalue()


def test_notebook_list_uses_observed_status_column(monkeypatch):
    stream = StringIO()
    monkeypatch.setattr(
        presentation,
        "console",
        lambda **kwargs: Console(file=stream, width=100, theme=presentation.THEME),
    )
    presentation.result(
        "list",
        "ok",
        None,
        [
            {"name": "first", "uid": "one", "stopped": True, "state": "Stopping"},
            {"name": "second", "uid": "two", "stopped": True, "state": "Stopped"},
        ],
    )
    text = stream.getvalue()
    assert "Status" in text and "Stopping" in text and "Stopped" in text
    assert "requested" not in text.lower()
