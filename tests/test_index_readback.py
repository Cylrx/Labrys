"""Manual index saves remain pending until the entire document reads back correctly."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from lab.errors import LabError
from lab.registration import save_index

ROOT = "op://fictional-vault/index/notesPlain"
VERIFY = "Index verification incomplete"


@pytest.fixture
def document():
    return {
        "schema_version": 1,
        "data_key_ref": "op://fictional-vault/key/password",
        "session": {"max_age_seconds": 900},
        "clusters": [
            {"id": "alpha", "config_ref": "op://fictional-vault/alpha/notesPlain"},
            {"id": "beta", "config_ref": "op://fictional-vault/beta/notesPlain"},
        ],
    }


def flow(reads, actions):
    async def work(awaitable, title):
        return await awaitable

    return SimpleNamespace(
        secrets=SimpleNamespace(read=AsyncMock(side_effect=reads)),
        screens=SimpleNamespace(details=AsyncMock(side_effect=actions), work=work),
    )


def verification_pages(state):
    return [call for call in state.screens.details.await_args_list if call.args[0] == VERIFY]


async def test_stale_then_current_recheck_only_reads_again(document):
    stale = deepcopy(document)
    stale["clusters"].pop()
    state = flow([yaml.safe_dump(stale), yaml.safe_dump(document)], ["saved", "recheck"])
    await save_index(state.secrets, ROOT, document, state.screens)
    assert [call.args[0] for call in state.secrets.read.await_args_list] == [ROOT, ROOT]
    assert state.screens.details.await_count == 2
    assert verification_pages(state)[0].args[2] == [("Missing cluster IDs", "beta")]
    assert verification_pages(state)[0].args[3] == [
        ("recheck", "Recheck"),
        ("review", "Review expected index"),
        ("cancel", "Cancel"),
    ]


@pytest.mark.parametrize("field", ["missing", "unexpected", "config", "key", "lifetime", "order"])
async def test_each_difference_stays_pending_and_cancel_never_claims_success(document, field):
    changed = deepcopy(document)
    if field == "missing":
        changed["clusters"].pop()
        diagnostic = ("Missing cluster IDs", "beta")
    elif field == "unexpected":
        changed["clusters"].append(
            {"id": "gamma", "config_ref": "op://fictional-vault/gamma/notesPlain"}
        )
        diagnostic = ("Unexpected cluster IDs", "gamma")
    elif field == "config":
        changed["clusters"][0]["config_ref"] = "op://fictional-vault/changed/notesPlain"
        diagnostic = ("Changed config_ref for cluster IDs", "alpha")
    elif field == "key":
        changed["data_key_ref"] = "op://fictional-vault/changed/password"
        diagnostic = ("data_key_ref", "Changed")
    elif field == "lifetime":
        changed["session"]["max_age_seconds"] = 600
        diagnostic = ("session.max_age_seconds", "Changed")
    else:
        changed["clusters"].reverse()
        diagnostic = ("Cluster order", "Changed")
    state = flow([yaml.safe_dump(changed)] * 2, ["saved", "recheck", "cancel"])
    with pytest.raises(LabError, match="not verified") as error:
        await save_index(state.secrets, ROOT, document, state.screens)
    assert error.value.code == 130
    assert state.secrets.read.await_count == 2
    assert len(verification_pages(state)) == 2
    for page in verification_pages(state):
        assert page.args[2] == [diagnostic]
        assert "op://" not in repr(page)
        assert "fictional-vault" not in repr(page)


async def test_review_preserves_original_complete_copy_payload(document):
    changed = deepcopy(document)
    changed["data_key_ref"] = "op://fictional-vault/changed/password"
    state = flow(
        [yaml.safe_dump(changed), yaml.safe_dump(document)],
        ["saved", "review", "saved"],
    )
    await save_index(state.secrets, ROOT, document, state.screens)
    first, _, review = state.screens.details.await_args_list
    assert first == review
    assert yaml.safe_load(review.kwargs["copy_text"]) == document
    assert state.secrets.read.await_count == 2


@pytest.mark.parametrize(
    ("failure", "diagnostic"),
    [
        (LabError("1Password temporarily unavailable", 5), "temporarily unavailable"),
        ("[invalid: YAML", "Invalid configuration document"),
        ("data_key_ref: op://fictional-vault/secret/password", "Invalid configuration at"),
    ],
)
async def test_read_or_parse_failure_can_be_rechecked(document, failure, diagnostic):
    state = flow([failure, yaml.safe_dump(document)], ["saved", "recheck"])
    await save_index(state.secrets, ROOT, document, state.screens)
    page = verification_pages(state)[0]
    assert page.args[2][0][0] == "Read-back failed"
    assert diagnostic in page.args[2][0][1]
    assert "op://" not in repr(page)
    assert state.secrets.read.await_count == 2


@pytest.mark.parametrize("failure", [LabError("Cancelled.", 130), asyncio.CancelledError()])
async def test_read_cancellation_propagates_without_retry_page(document, failure):
    state = flow([failure], ["saved"])
    with pytest.raises(type(failure)) as error:
        await save_index(state.secrets, ROOT, document, state.screens)
    assert error.value is failure
    assert not verification_pages(state)
    state.secrets.read.assert_awaited_once_with(ROOT)


async def test_escape_at_verification_propagates(document):
    changed = deepcopy(document)
    changed["clusters"] = []
    failure = LabError("Cancelled.", 130)
    state = flow([yaml.safe_dump(changed)], ["saved", failure])
    with pytest.raises(LabError) as error:
        await save_index(state.secrets, ROOT, document, state.screens)
    assert error.value is failure
    state.secrets.read.assert_awaited_once_with(ROOT)
