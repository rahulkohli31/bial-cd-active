"""Normalized value types the backend converts SDK responses into, plus the
`ObjectStorage` ABC. No vendor type ever crosses this port — the adapter imports
`BlobProperties` / `ContentSettings` only inside its own module and returns these
common types instead.

The ABC + the `@abstractmethod` set are the sanctioned storage-port pattern
(ADR-0009): an ABC, not a Protocol, so nominal subtyping makes an IDE jump land
on the concrete backend and an incomplete backend fails at instantiation.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.services.storage.constants import DEFAULT_PAGE_SIZE, validate_sas_ttl


@dataclass(frozen=True)
class ObjectMeta:
    """Normalized metadata for one stored object. Only `key` and `size` are
    always present; the rest are nullable because Azure returns a different field
    subset from a `put` vs a `head`."""

    key: str
    size: int
    content_type: str | None
    etag: str | None
    """Quotes stripped; OPAQUE. May carry an Azure sequence number. Use only for
    change detection — never parse as MD5/hex."""
    last_modified: datetime | None
    """Nullable: absent from `put()` (a follow-up `head()` fills it when a caller
    needs it)."""


@dataclass(frozen=True)
class ListPage:
    """One page of a prefix listing. `keys` is a tuple so the page is truly
    immutable; `next_token` is a provider-opaque cursor valid only for the same
    store instance (`None` means the listing is exhausted)."""

    keys: tuple[str, ...]
    next_token: str | None


class ObjectStorage(abc.ABC):
    """The core async storage interface — the operations the Azure Blob backend
    implements. A single known implementation (`AzureBlobStorage`), so an ABC (not
    a Protocol): nominal subtyping makes F12 on a concrete factory result land on
    the concrete method, and an incomplete backend fails with a runtime `TypeError`.

    Provider-specific features (tagging, versioning, snapshots, block staging,
    signed UPLOAD URLs, ranged reads) are deliberately NOT here — see the plan's
    Scope Boundaries. Reachable only by downcasting to the concrete backend.
    """

    def __init__(self, *, provider: str) -> None:
        # provider is "azure" — carried so StorageError correlation is filled
        # without the backend threading it through every raise.
        self._provider = provider

    @property
    def provider(self) -> str:
        return self._provider

    @abc.abstractmethod
    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        """Logically-atomic write of `data` (≤ MAX_PUT_BYTES). Returns normalized
        metadata; `etag` is best-effort (MAY be None on Azure) and `last_modified`
        is None — `head()` afterwards for a guaranteed identity."""
        ...

    @abc.abstractmethod
    async def get(self, key: str) -> bytes:
        """Fetch the whole object. Missing object raises StorageNotFoundError."""
        ...

    @abc.abstractmethod
    async def head(self, key: str) -> ObjectMeta | None:
        """Metadata only; folds `exists()`. A missing OBJECT returns None; a
        missing container raises StorageError."""
        ...

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        """Idempotent on a missing object (no-op); a missing container
        raises StorageError."""
        ...

    @abc.abstractmethod
    async def list(
        self, prefix: str, *, page_size: int = DEFAULT_PAGE_SIZE, token: str | None = None
    ) -> ListPage:
        """One page of keys under `prefix`. `next_token` is a provider-opaque
        cursor (None when exhausted)."""
        ...

    async def signed_read_url(self, key: str, *, expires_in: timedelta) -> str:
        """A time-limited read URL (an Azure Blob SAS). CONCRETE so the
        ≤ MAX_SIGNED_URL_TTL ceiling is one invariant the backend can neither skip
        nor silently clamp: it is rejected fail-closed here BEFORE the backend runs.

        SECURITY: the returned `str` is a BEARER credential — it embeds the `sig=`
        SAS token. Callers MUST treat it like a secret and never log or persist it
        in plaintext (the type system can't enforce this on a `str`; the contract
        carries the guarantee).
        """
        validate_sas_ttl(expires_in, provider=self.provider, key=key)
        return await self._signed_read_url_impl(key, expires_in=expires_in)

    @abc.abstractmethod
    async def _signed_read_url_impl(self, key: str, *, expires_in: timedelta) -> str:
        """Backend hook for `signed_read_url`, called only after the TTL ceiling
        has passed. The backend overrides THIS, never the public method."""
        ...

    @abc.abstractmethod
    async def aclose(self) -> None:
        """Close the long-lived client (and, for managed identity, the credential).
        Safe to call more than once."""
        ...
