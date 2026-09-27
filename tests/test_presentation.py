"""Human output reflects observed state and keeps the runtime console non-recording."""

from io import StringIO

import pytest
from fixtures import OPERATION_ID, inputs, profile, profile_data
from rich.console import Console
from rich.control import Control
from test_resources import node

from lab import presentation
from lab.manifest import compile_notebook
from lab.resources import snapshot


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


@pytest.mark.parametrize(
    "render",
    [
        lambda value: presentation.heading(value, value, value),
        presentation.note,
        presentation.success,
        presentation.error,
        lambda value: presentation.details([(value, value)], value),
        lambda value: presentation.result(
            value, value, {value: value}, [{"name": value, "state": value, "uid": value}]
        ),
        lambda value: presentation.result("inspect", "ok", None, {value: value}),
    ],
    ids=["heading", "note", "success", "error", "details", "list", "result"],
)
def test_human_output_cannot_emit_terminal_instructions(render, capsys, monkeypatch):
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    render("example\x1b]0;untrusted-title\x1b\\\x9b2J\x9d0;other-title\x9c")
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert not any(char in output for char in "\x1b\x9b\x9d\x9c")
    assert "\\x1b" in output and "\\x9b" in output


def test_profile_preview_escapes_controls_without_changing_startup_arguments(capsys, monkeypatch):
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    argument = "example\x1b]0;untrusted-title\x1b\\"
    data = profile_data()
    data["namespace_rules"]["research"]["runtime"]["exact_images"][inputs().image]["args"] = [
        argument
    ]
    manifest = compile_notebook(profile(data), inputs(), OPERATION_ID)
    presentation.preview(manifest)
    captured = capsys.readouterr()
    assert captured.out == "" and "Notebook preview" in captured.err
    assert "\x1b" not in captured.err and "\\x1b" in captured.err
    assert manifest["spec"]["template"]["spec"]["containers"][0]["args"] == [argument]


def test_resource_table_escapes_controls_in_api_metadata(capsys, monkeypatch):
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    data = snapshot([node("node-a\x1b[2J")], [], None).report()
    presentation.resources("research-example", data)
    output = capsys.readouterr().out
    assert "\x1b" not in output and "node-a\\x1b[2J" in output


def test_human_output_escapes_c0_del_and_c1_controls(capsys, monkeypatch):
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    controls = "".join(map(chr, (*range(32), *range(127, 160)))).replace("\n", "").replace("\t", "")
    presentation.note(controls)
    output = capsys.readouterr().out
    assert not any(char in output for char in controls)
    assert "\\x07" in output and "\\x7f" in output and "\\x9f" in output


def test_console_preserves_unicode_layout_styles_and_rich_controls(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    screen = presentation.console()
    stream = StringIO()
    screen.file = stream
    monkeypatch.setattr(presentation, "console", lambda **kwargs: screen)
    presentation.heading("正常 ≤8", "Ready\nSecond line")
    screen.print(Control.move_to_column(0), end="")
    output = stream.getvalue()
    assert "正常 ≤8" in output and "\n" in output
    assert "Ready" in output and "Second line" in output
    assert "\x1b[1;36m" in output and "\x1b[1G" in output
