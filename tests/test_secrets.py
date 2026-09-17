"""Structured SDK diagnostics preserve secret values and limit transport retries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from onepassword.errors import RateLimitExceededException
from onepassword.types import ResolveAllResponse

from lab import secrets
from lab.errors import LabError

REFERENCE = "op://vault/item/notesPlain"
SECRET = "synthetic-private-value"


def response(*, value=SECRET, error=None):
    result = (
        {"error": error}
        if error
        else {"content": {"secret": value, "itemId": "item", "vaultId": "vault"}}
    )
    return ResolveAllResponse.model_validate({"individualResponses": {REFERENCE: result}})


def source(results):
    read = AsyncMock(side_effect=results)
    client = SimpleNamespace(secrets=SimpleNamespace(resolve_all=read))
    return secrets.Secrets(client), read


async def test_resolution_uses_public_structured_response_and_returns_value_only():
    api, read = source([response()])
    assert await api.read(REFERENCE) == SECRET
    read.assert_awaited_once_with([REFERENCE])


@pytest.mark.parametrize(
    "kind,category",
    [
        ("fieldNotFound", "field_not_found"),
        ("vaultNotFound", "vault_unavailable"),
        ("itemNotFound", "item_unavailable"),
        ("tooManyVaults", "ambiguous_vault"),
        ("tooManyItems", "ambiguous_item"),
        ("tooManyMatchingFields", "ambiguous_field"),
        ("noMatchingSections", "section_not_found"),
        ("other", "resolution_error"),
    ],
)
async def test_reference_errors_are_specific_and_never_automatically_retried(kind, category):
    api, read = source([response(error={"type": kind})])
    with pytest.raises(LabError, match=category) as error:
        await api.read(REFERENCE)
    assert REFERENCE not in str(error.value)
    assert SECRET not in str(error.value)
    assert read.await_count == 1


async def test_sdk_error_messages_are_never_exposed_or_pattern_matched():
    for result in [Exception(SECRET), response(error={"type": "parsing", "message": SECRET})]:
        api, read = source([result])
        with pytest.raises(LabError) as error:
            await api.read(REFERENCE)
        assert SECRET not in str(error.value)
        assert read.await_count == 1


async def test_known_transport_interruption_retries_same_read_with_backoff(monkeypatch):
    pause = AsyncMock()
    monkeypatch.setattr(secrets.asyncio, "sleep", pause)
    api, read = source([ConnectionResetError(SECRET), TimeoutError(SECRET), response()])
    assert await api.read(REFERENCE) == SECRET
    assert read.await_count == 3
    assert [call.args[0] for call in pause.await_args_list] == [0.25, 0.5]


async def test_retry_budget_is_finite(monkeypatch):
    monkeypatch.setattr(secrets.asyncio, "sleep", AsyncMock())
    api, read = source([ConnectionError(SECRET)] * 3)
    with pytest.raises(LabError, match="connection_interrupted") as error:
        await api.read(REFERENCE)
    assert SECRET not in str(error.value)
    assert read.await_count == 3


async def test_rate_limit_is_reported_without_fast_retry():
    api, read = source([RateLimitExceededException(SECRET)])
    with pytest.raises(LabError, match="rate_limited") as error:
        await api.read(REFERENCE)
    assert SECRET not in str(error.value)
    assert read.await_count == 1


async def test_cancellation_is_not_retried():
    api, read = source([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await api.read(REFERENCE)
    assert read.await_count == 1


async def test_deadline_expiry_during_backoff_prevents_next_attempt(monkeypatch):
    now = 0
    monkeypatch.setattr(secrets.clock, "now", lambda: now)

    async def expire(_):
        nonlocal now
        now = 11

    monkeypatch.setattr(secrets.asyncio, "sleep", expire)
    api, read = source([ConnectionError(SECRET), response()])
    api.deadline = 10
    with pytest.raises(LabError, match="expired"):
        await api.read(REFERENCE)
    assert read.await_count == 1


async def test_expired_session_cannot_return_late_secret(monkeypatch):
    now = 0
    monkeypatch.setattr(secrets.clock, "now", lambda: now)

    async def late(_):
        nonlocal now
        now = 11
        return response()

    api, read = source([])
    read.side_effect = late
    api.deadline = 10
    with pytest.raises(LabError, match="expired"):
        await api.read(REFERENCE)


@pytest.mark.parametrize(
    "entry",
    [
        None,
        {},
        {"content": {"secret": SECRET, "itemId": "i", "vaultId": "v"}, "error": {"type": "other"}},
    ],
)
async def test_incomplete_or_ambiguous_sdk_result_never_returns_a_secret(entry):
    result = ResolveAllResponse.model_validate(
        {"individualResponses": {REFERENCE: entry} if entry is not None else {}}
    )
    api, _ = source([result])
    with pytest.raises(LabError, match="invalid_response") as error:
        await api.read(REFERENCE)
    assert SECRET not in str(error.value)


async def test_document_limit_still_applies():
    api, _ = source([response(value="x" * (1024 * 1024 + 1))])
    with pytest.raises(LabError, match="1 MiB"):
        await api.read(REFERENCE)


async def test_authentication_reports_rate_limit_without_echoing_token(monkeypatch):
    monkeypatch.setattr(
        secrets.Client, "authenticate", AsyncMock(side_effect=RateLimitExceededException(SECRET))
    )
    with pytest.raises(LabError, match="rate_limited") as error:
        await secrets.Secrets.authenticate(SECRET)
    assert SECRET not in str(error.value)


async def test_overall_read_budget_cancels_a_hanging_request(monkeypatch):
    original_timeout = asyncio.timeout
    monkeypatch.setattr(secrets.asyncio, "timeout", lambda seconds: original_timeout(0.01))
    cancelled = asyncio.Event()

    async def hang(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    api, read = source([])
    read.side_effect = hang
    with pytest.raises(LabError, match="timeout"):
        await api.read(REFERENCE)
    assert cancelled.is_set()
    assert read.await_count == 1
