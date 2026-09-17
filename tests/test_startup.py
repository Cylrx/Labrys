"""Initialization and session ownership with synthetic secret readers and screens."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fixtures import EXAMPLES

from lab import cli, interface
from lab.errors import LabError
from lab.storage import Paths, write_bootstrap


class Screens:
    def __init__(self):
        self.actions = []
        self.titles = []

    async def details(self, title, *args, **kwargs):
        self.titles.append(title)
        return self.actions.pop(0)

    async def work(self, operation, *args, **kwargs):
        return await operation


@pytest.fixture
def startup(monkeypatch, tmp_path):
    screens = Screens()
    paths = Paths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    source = SimpleNamespace(
        read=AsyncMock(return_value=(EXAMPLES / "index.yaml").read_text()),
        deadline=None,
    )
    authenticate = AsyncMock(return_value=source)
    read_token = AsyncMock(return_value="synthetic-service-token")
    initialize = AsyncMock(
        return_value={
            "schema_version": 2,
            "index_ref": "op://vault/root/notesPlain",
            "profiles_dir": str(tmp_path / "profiles"),
        }
    )
    connect = AsyncMock(return_value=None)
    monkeypatch.setattr(cli, "terminal_required", lambda: None)
    monkeypatch.setattr(cli, "Screens", lambda **kwargs: screens)
    monkeypatch.setattr(cli, "Paths", lambda: paths)
    monkeypatch.setattr(cli.Secrets, "authenticate", authenticate)
    monkeypatch.setattr(cli, "read_token", read_token)
    monkeypatch.setattr(cli, "initialize", initialize)
    monkeypatch.setattr(cli, "run_authorized", connect)
    return SimpleNamespace(**locals())


async def test_unconfigured_start_can_exit_without_requesting_credentials(startup):
    startup.screens.actions = ["exit"]
    assert await cli.run(cli.parser().parse_args([])) is None
    assert startup.screens.titles == ["Set up this device"]
    startup.read_token.assert_not_awaited()
    startup.authenticate.assert_not_awaited()


async def test_unconfigured_standalone_fails_before_requesting_credentials(startup):
    args = cli.parser().parse_args(
        ["--cluster", "west", "notebook", "list", "--namespace", "research"]
    )
    with pytest.raises(LabError, match="lab init"):
        await cli.run(args)
    startup.read_token.assert_not_awaited()
    assert startup.screens.titles == []


@pytest.mark.parametrize("arguments", [[], ["init"]])
async def test_setup_finishes_without_opening_a_cluster_session(startup, arguments):
    startup.screens.actions = (["setup"] if not arguments else []) + ["done"]
    await cli.run(cli.parser().parse_args(arguments))
    startup.read_token.assert_awaited_once()
    startup.authenticate.assert_awaited_once_with("synthetic-service-token")
    startup.initialize.assert_awaited_once_with(startup.source, startup.paths)
    startup.source.read.assert_not_awaited()
    startup.connect.assert_not_awaited()


async def test_empty_index_guides_add_and_exits_without_cluster_menu(startup):
    import yaml

    index = yaml.safe_load((EXAMPLES / "index.yaml").read_text())
    index["clusters"] = []
    startup.source.read.return_value = yaml.safe_dump(index)
    write_bootstrap(startup.paths, "op://vault/root/notesPlain", startup.tmp_path / "profiles")
    startup.screens.actions = ["exit"]
    await cli.run(cli.parser().parse_args([]))
    assert startup.screens.titles == ["No registered clusters"]
    startup.connect.assert_not_awaited()


async def test_cluster_add_has_an_independent_authorized_flow(startup, monkeypatch):
    from fixtures import CLUSTER_ID

    write_bootstrap(startup.paths, "op://vault/root/notesPlain", startup.tmp_path / "profiles")
    add = AsyncMock()
    monkeypatch.setattr(cli, "add_cluster", add)
    args = cli.parser().parse_args(["cluster", "add", "--cluster-id", CLUSTER_ID])
    await cli.run(args)
    add.assert_awaited_once()
    assert add.call_args.args[2] == CLUSTER_ID
    assert add.call_args.kwargs["authorized_at"] <= cli.clock.now()
    startup.connect.assert_not_awaited()


@pytest.mark.parametrize("flag", ["--json", "--yes", "--token-stdin", "--request-auth"])
def test_cluster_registration_requires_its_interactive_confirmation(flag):
    for tokens in (["cluster", "add", flag], [flag, "cluster", "add"]):
        with pytest.raises(LabError):
            args = cli.parser().parse_args(tokens)
            cli.validate_arguments(args)


async def test_menu_delegation_retains_identity_during_cancellation(monkeypatch):
    target = {"namespace": "research", "name": "example"}
    data = {"operation_id": "synthetic-operation", "uid": "synthetic-uid"}
    native = SimpleNamespace(
        run=AsyncMock(side_effect=LabError("Interrupted", 130, target=target, data=data)),
        outcome={"target": target, "data": data},
        result={"status": "error", "data": data},
    )
    monkeypatch.setattr(interface, "Interactive", lambda *args: native)
    application = cli.Application(None, None)
    with pytest.raises(LabError) as error:
        await application.menu(editor_only=True)
    native.run.assert_awaited_once_with(editor_only=True)
    assert error.value.data == application.outcome["data"] == data
    assert application.result == native.result


@pytest.mark.parametrize("selected", ["research-example", "research-other", "unregistered"])
async def test_readable_name_selects_the_matching_connection_and_session(
    monkeypatch, tmp_path, selected
):
    import yaml
    from fixtures import cluster_data

    index_data = {
        "schema_version": 1,
        "data_key_ref": "op://vault/key/password",
        "clusters": [
            {"id": name, "config_ref": f"op://vault/{name}/notesPlain"}
            for name in ("research-example", "research-other")
        ],
    }
    index = cli.parse_index(yaml.safe_dump(index_data))
    secrets = SimpleNamespace(read=AsyncMock(side_effect=[yaml.safe_dump(cluster_data()), "key"]))
    session = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), check=lambda: None)
    selected_sessions = []

    def create_session(*args, **kwargs):
        selected_sessions.append(kwargs["cluster_id"])
        return session

    api = SimpleNamespace(session=session, collection=AsyncMock(return_value=[]))
    monkeypatch.setattr(cli, "data_key", lambda text: b"synthetic-key")
    monkeypatch.setattr(cli, "Repository", lambda *args: SimpleNamespace(verify=lambda: None))
    monkeypatch.setattr(
        cli.Toolchain, "load", lambda: SimpleNamespace(ssh=None, python=None, credential=None)
    )
    monkeypatch.setattr(cli, "Session", create_session)
    monkeypatch.setattr(cli, "Kubernetes", lambda session: api)
    args = cli.parser().parse_args(
        ["--cluster", selected, "--json", "notebook", "list", "--namespace", "research"]
    )
    if selected == "unregistered":
        with pytest.raises(LabError, match="not in the selected root index"):
            await cli.run_authorized(
                args, index, cli.clock.now(), secrets, None, tmp_path / "absent-profiles"
            )
        secrets.read.assert_not_awaited()
        assert not selected_sessions
        return
    result = await cli.run_authorized(
        args, index, cli.clock.now(), secrets, None, tmp_path / "absent-profiles"
    )
    assert result["target"]["cluster"] == selected
    assert selected_sessions == [selected]
    assert [call.args[0] for call in secrets.read.await_args_list] == [
        f"op://vault/{selected}/notesPlain",
        "op://vault/key/password",
    ]
    session.connect.assert_awaited_once()
    session.close.assert_awaited_once()


async def test_cluster_remove_has_its_own_read_only_authorized_flow(startup, monkeypatch):
    from fixtures import CLUSTER_ID

    write_bootstrap(startup.paths, "op://vault/root/notesPlain", startup.tmp_path / "profiles")
    remove = AsyncMock()
    monkeypatch.setattr(cli, "remove_cluster", remove)
    add = AsyncMock()
    monkeypatch.setattr(cli, "add_cluster", add)
    args = cli.parser().parse_args(["cluster", "remove", "--cluster-id", CLUSTER_ID])
    await cli.run(args)
    remove.assert_awaited_once()
    assert remove.call_args.args[2] == CLUSTER_ID
    add.assert_not_awaited()
    startup.connect.assert_not_awaited()
