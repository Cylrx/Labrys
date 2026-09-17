"""Bounded API reads and Notebook discovery against a synthetic HTTP server."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from pydantic import SecretStr

from lab.errors import LabError
from lab.kubernetes import Kubernetes, ResponseTooLarge, owned_pods


@pytest.fixture
async def api_server():
    runners = []

    async def start(handler):
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        runners.append(runner)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return Kubernetes(
            SimpleNamespace(
                endpoint=f"http://127.0.0.1:{port}",
                token=SecretStr("synthetic-test-token"),
                ssl_context=False,
                tls_name=None,
                check=lambda: None,
                check_health=AsyncMock(),
            )
        )

    yield start
    for runner in runners:
        await runner.cleanup()


def owned(name, uid, parent):
    return {"metadata": {"name": name, "uid": uid, "ownerReferences": [{"uid": parent}]}}


async def test_status_filters_on_server_and_verifies_owner_uids(api_server):
    requests = []

    async def response(request):
        assert request.method == "GET"
        requests.append(request.path)
        if request.path == "/apis/apps/v1/namespaces/research/statefulsets":
            assert request.query["fieldSelector"] == "metadata.name=sample"
            items = [owned("sample", "parent", "notebook")]
        else:
            assert request.path == "/api/v1/namespaces/research/pods"
            assert request.query["labelSelector"] == "notebook-name=sample"
            items = [
                owned("sample-0", "pod", "parent"),
                owned("stale", "old-pod", "old-parent"),
                owned("unrelated", "foreign", "foreign-parent"),
            ]
        return web.json_response({"items": items})

    api = await api_server(response)
    notebook = {"metadata": {"namespace": "research", "name": "sample", "uid": "notebook"}}
    assert [pod["metadata"]["name"] for pod in await owned_pods(api, notebook)] == ["sample-0"]
    assert len(requests) == 2


async def test_large_page_retries_same_cursor_without_duplicate_results(api_server):
    calls = []
    large = "x" * (3 * 1024 * 1024)

    async def response(request):
        assert request.method == "GET"
        assert request.query["labelSelector"] == "notebook-name=sample"
        limit = int(request.query["limit"])
        cursor = request.query.get("continue", "")
        calls.append((cursor, limit))
        if not cursor:
            return web.json_response({"items": [{"id": 0}], "metadata": {"continue": "next"}})
        offset = 1 if cursor == "next" else int(cursor)
        end = min(offset + limit, 4)
        return web.json_response(
            {
                "items": [{"id": index, "data": large} for index in range(offset, end)],
                "metadata": {"continue": str(end) if end < 4 else ""},
            }
        )

    api = await api_server(response)
    result = await api.collection("/pods", limit=4, labelSelector="notebook-name=sample")
    assert [item["id"] for item in result] == [0, 1, 2, 3]
    assert calls == [("", 4), ("next", 4), ("next", 2), ("3", 2)]
    api.session.check_health.assert_not_awaited()


async def test_single_oversized_object_fails_without_unbounded_retries(api_server):
    calls = []

    async def response(request):
        calls.append(request.method)
        return web.json_response({"items": [{"data": "x" * (8 * 1024 * 1024)}]})

    api = await api_server(response)
    with pytest.raises(ResponseTooLarge, match="8 MiB"):
        await api.collection("/pods", limit=1)
    assert calls == ["GET"]


async def test_repeated_pagination_cursor_is_rejected(api_server):
    async def response(request):
        return web.json_response({"items": [], "metadata": {"continue": "same"}})

    api = await api_server(response)
    with pytest.raises(LabError, match="repeated a pagination cursor"):
        await api.collection("/pods")
