"""Read-only 1Password access with bounded requests and value-free diagnostics."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from onepassword import Client
from onepassword.errors import DesktopSessionExpiredException, RateLimitExceededException
from onepassword.types import ResolveReferenceErrorTypes

from lab import __version__, clock
from lab.errors import LabError


@dataclass(frozen=True)
class Choice:
    id: str
    title: str


REFERENCE_ERRORS = {
    ResolveReferenceErrorTypes.PARSING: ("invalid_reference", "The reference could not be parsed."),
    ResolveReferenceErrorTypes.FIELD_NOT_FOUND: (
        "field_not_found",
        "The selected field was not found.",
    ),
    ResolveReferenceErrorTypes.VAULT_NOT_FOUND: (
        "vault_unavailable",
        "The vault was not found or is inaccessible.",
    ),
    ResolveReferenceErrorTypes.ITEM_NOT_FOUND: (
        "item_unavailable",
        "The item was not found or is inaccessible.",
    ),
    ResolveReferenceErrorTypes.TOO_MANY_VAULTS: (
        "ambiguous_vault",
        "Select a vault by its unique ID.",
    ),
    ResolveReferenceErrorTypes.TOO_MANY_ITEMS: (
        "ambiguous_item",
        "Select an item by its unique ID.",
    ),
    ResolveReferenceErrorTypes.TOO_MANY_MATCHING_FIELDS: (
        "ambiguous_field",
        "More than one field matched the reference.",
    ),
    ResolveReferenceErrorTypes.NO_MATCHING_SECTIONS: (
        "section_not_found",
        "The selected section was not found.",
    ),
}


def _failure(operation: str, category: str, message: str) -> LabError:
    return LabError(f"1Password {operation} failed [{category}]. {message}", 3)


def _sdk_failure(operation: str, error: Exception) -> LabError:
    if isinstance(error, RateLimitExceededException):
        return _failure(
            operation, "rate_limited", "The service reported a rate limit. Wait before retrying."
        )
    if isinstance(error, TimeoutError):
        return _failure(
            operation, "timeout", "The request timed out. Retry when the connection is available."
        )
    if isinstance(error, ConnectionError):
        return _failure(
            operation, "connection_interrupted", "The connection was interrupted. Retry the read."
        )
    if isinstance(error, DesktopSessionExpiredException):
        return _failure(operation, "authorization_expired", "Start a new authorization session.")
    return _failure(
        operation,
        "sdk_error",
        "The SDK did not provide a safe error category. "
        "Retry the operation; no credential values were logged.",
    )


class Secrets:
    """Resolve selected fields without desktop authentication or ambient tokens."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self.deadline: float | None = None

    @classmethod
    async def authenticate(cls, token: str) -> "Secrets":
        if not token or len(token.encode()) > 16384:
            raise LabError("A nonempty Service Account Token of at most 16 KiB is required.", 3)
        try:
            async with asyncio.timeout(30):
                client = await Client.authenticate(
                    auth=token, integration_name="Labrys", integration_version=__version__
                )
            return cls(client)
        except Exception as error:
            raise _sdk_failure("authorization", error) from None

    def _check_deadline(self) -> None:
        if self.deadline is not None and clock.now() >= self.deadline:
            raise LabError("The authorization session has expired.", 3)

    async def _read[T](self, operation: Callable[[], Awaitable[T]], purpose: str) -> T:
        """Retry typed transport failures within one 30-second read budget."""
        self._check_deadline()
        try:
            async with asyncio.timeout(30):
                for attempt in range(3):
                    self._check_deadline()
                    try:
                        result = await operation()
                    except (TimeoutError, ConnectionError) as error:
                        self._check_deadline()
                        if attempt == 2:
                            raise _sdk_failure(purpose, error) from None
                        await asyncio.sleep(0.25 * (2**attempt))
                        continue
                    except Exception as error:
                        raise _sdk_failure(purpose, error) from None
                    self._check_deadline()
                    return result
        except TimeoutError:
            raise _sdk_failure(purpose, TimeoutError()) from None
        raise AssertionError("Read attempts exhausted without a result or failure")

    async def read(self, reference: str) -> str:
        from lab.config import secret_reference

        self._check_deadline()
        try:
            secret_reference(reference)
        except ValueError:
            raise LabError("Invalid secret reference.", 4) from None
        # Batch resolution exposes structured per-reference errors; resolve() discards them.
        response = await self._read(
            lambda: self._client.secrets.resolve_all([reference]), "field read"
        )
        result = response.individual_responses.get(reference)
        if result is None or (result.content is None) == (result.error is None):
            raise _failure(
                "field read",
                "invalid_response",
                "The SDK returned an incomplete or ambiguous result.",
            )
        if result.error is not None:
            category, message = REFERENCE_ERRORS.get(
                result.error.type,
                (
                    "resolution_error",
                    "The field could not be resolved. "
                    "The SDK provided no more specific safe category.",
                ),
            )
            raise _failure("field read", category, message)
        value = result.content.secret
        if len(value.encode()) > 1024 * 1024:
            raise LabError("The selected field exceeds the 1 MiB document limit.", 4)
        return value

    async def vaults(self) -> list[Choice]:
        vaults = await self._read(self._client.vaults.list, "vault listing")
        return [Choice(vault.id, vault.title) for vault in vaults]

    async def items(self, vault_id: str) -> list[Choice]:
        items = await self._read(lambda: self._client.items.list(vault_id), "item listing")
        return [Choice(item.id, item.title) for item in items]
