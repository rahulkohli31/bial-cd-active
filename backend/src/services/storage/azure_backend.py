"""AzureBlobStorage — Azure Blob Storage via `azure-storage-blob.aio` +
`azure-identity.aio`. The sole backend behind the `ObjectStorage` interface.

No vendor type crosses the port: `BlobProperties`, `ContentSettings`, the page
iterator, and the `upload_blob` `dict[str, Any]` are consumed only here; every
method returns the common `ObjectMeta`/`ListPage`/`bytes`.

Lifecycle: one long-lived `BlobServiceClient` (and, for managed identity, one
credential) per process per config, lazily built into a module-level cache.
`aclose` closes BOTH the client and the credential — an unclosed credential leaks
its own aiohttp session. `close_all_clients()` is the hook `aclose_storage()`
calls on shutdown.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Final, NoReturn, cast
from urllib.parse import urlsplit

import structlog
from azure.core.async_paging import AsyncPageIterator
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob import (
    BlobProperties,
    BlobSasPermissions,
    ContentSettings,
    UserDelegationKey,
    generate_blob_sas,
)
from azure.storage.blob.aio import BlobServiceClient

from src.services.storage.base import ListPage, ObjectMeta, ObjectStorage
from src.services.storage.config import AzureStorageConfig
from src.services.storage.constants import (
    DEFAULT_PAGE_SIZE,
    MAX_PUT_BYTES,
    MAX_SIGNED_URL_TTL,
)
from src.services.storage.errors import (
    StorageAuthError,
    StorageError,
    StorageNotFoundError,
    StorageSignError,
    StorageUploadError,
)
from src.services.storage.keys import normalize_metadata

_log = structlog.get_logger()

# Signed URLs / delegation keys start ~15m in the past to tolerate clock skew.
_CLOCK_SKEW = timedelta(minutes=15)

# The only two 404s Azure NAMES on a blob operation (`x-ms-error-code`). Anything else that
# arrives as a ResourceNotFoundError — a code-less 404 minted by a proxy/WAF between us and the
# account, or a code we don't speak — is a question that FAILED, not an answer about the object.
_BLOB_NOT_FOUND: Final = "BlobNotFound"
_CONTAINER_NOT_FOUND: Final = "ContainerNotFound"


def _now() -> datetime:
    # Indirection so the delegation-key re-mint logic is time-controllable in
    # tests. Repo convention is datetime.now(UTC), not timezone.utc.
    return datetime.now(UTC)


# --- pure helpers (unit-tested directly) -------------------------------------


def _clean_etag(raw: str | None) -> str | None:
    # Strip Azure's quotes; OPAQUE thereafter (a sequence number, never an MD5).
    return (raw or "").strip('"') or None


def _error_code(exc: HttpResponseError) -> str | None:
    # Both ResourceNotFoundError and the broader HttpResponseError carry
    # `error_code`; widened from ResourceNotFoundError so `_raise_azure` can
    # inspect any HttpResponseError (e.g. a 403 → auth).
    code = getattr(exc, "error_code", None)
    return code if isinstance(code, str) else None


def _is_confirmed_absent(exc: ResourceNotFoundError) -> bool:
    """True only when Azure NAMED the object as missing — never merely because a 404 arrived.

    The store answers in THREE states, not two: present, absent, and cannot-tell. A
    `ResourceNotFoundError` is evidence of the third by default: `_error_code` is None for any 404
    Azure did not label (a proxy/WAF blip in front of the account raises the same type), so reading
    the type alone as absence turns "I could not ask" into "the store positively answered: no
    bundle". `_restore_or_provision` trusts that answer and provisions a blank template, which
    finalize then snapshots over the user's saved app — the whole reason absence is claimed only on
    Azure's own word for it.
    """
    return _error_code(exc) == _BLOB_NOT_FOUND


def _conn_field(connection_string: str, field: str) -> str | None:
    prefix = f"{field}="
    for part in connection_string.split(";"):
        if part.startswith(prefix):
            return part[len(prefix) :]
    return None


def _account_name(config: AzureStorageConfig) -> str:
    # Connection strings carry AccountName explicitly (and Azurite's path-style
    # URL has no account label in the host); real-Azure account_url is
    # https://{account}.blob.core.windows.net.
    if config.connection_string is not None:
        name = _conn_field(config.connection_string.get_secret_value(), "AccountName")
        if name:
            return name
    host = urlsplit(config.account_url).hostname or ""
    return host.split(".")[0]


def account_signing_key(config: AzureStorageConfig) -> str | None:
    """The shared-account key that signs a service SAS (account-key auth mode), unwrapped only
    here at the SAS-signing boundary. `None` under managed identity — sign with a delegation key
    instead. Shared by blob-level (`AzureBlobStorage`) and container-level (`AppContainerStore`)
    SAS signing so the secret is unwrapped in exactly one place."""
    if config.account_key is not None:
        return config.account_key.get_secret_value()
    if config.connection_string is not None:
        return _conn_field(config.connection_string.get_secret_value(), "AccountKey")
    return None


def raise_azure(
    exc: HttpResponseError | ServiceRequestError, *, op: str, key: str, provider: str
) -> NoReturn:
    """SANITIZED re-raise: never the raw exception text (which can carry a SAS/account-key
    substring), only the operation; provider/key ride on the fields for logs. A 403 / explicit
    auth failure maps to `StorageAuthError`, everything else to the base `StorageError`. Shared by
    `AzureBlobStorage` and `AppContainerStore` so neither surfaces a credential in a raised
    message."""
    if isinstance(exc, ClientAuthenticationError) or (
        isinstance(exc, HttpResponseError) and exc.status_code == 403
    ):
        raise StorageAuthError(f"Azure {op} denied", provider=provider, key=key) from exc
    raise StorageError(f"Azure {op} failed", provider=provider, key=key) from exc


def _delegation_expiry(udk: UserDelegationKey) -> datetime:
    # signed_expiry is an ISO-8601 string (often Z-suffixed, which fromisoformat
    # handles on 3.11+, yielding a UTC-aware datetime). It is typed Optional;
    # a None here is an Azure anomaly we fail closed on.
    if udk.signed_expiry is None:
        raise StorageSignError("Azure user-delegation key has no expiry", provider="azure")
    return datetime.fromisoformat(udk.signed_expiry)


def _needs_remint(now: datetime, key_expiry: datetime, expires_in: timedelta) -> bool:
    # Re-mint when the cached delegation key can no longer cover the requested
    # (already ≤ MAX_SIGNED_URL_TTL) duration. A small expires_in reuses a key
    # for most of its 7-day life; only a near-expiry key forces a re-mint.
    return (key_expiry - now) < expires_in


def _sas_expiry(now: datetime, expires_in: timedelta, key_expiry: datetime) -> datetime:
    # Never exceed the delegation key's own lifetime, or Azure rejects the SAS.
    return min(now + expires_in, key_expiry)


def _fingerprint(config: AzureStorageConfig) -> str:
    material: tuple[str, ...]
    if config.connection_string is not None:
        material = ("conn", config.connection_string.get_secret_value())
    elif config.account_key is not None:
        material = ("key", config.account_url, config.account_key.get_secret_value())
    else:
        material = ("mi", config.account_url)
    return sha256("\x00".join(material).encode()).hexdigest()


# --- module-level client cache (source of truth for open SDK clients) ---------


class _AzureClient:
    """Cached per-config state: the long-lived service client, the credential to
    close (managed identity only), and the cached user-delegation key."""

    def __init__(
        self, service_client: BlobServiceClient, *, credential: DefaultAzureCredential | None
    ) -> None:
        self.service_client = service_client
        self.credential = credential
        self.delegation_key: UserDelegationKey | None = None
        self.delegation_expiry: datetime | None = None
        # Serializes delegation-key re-minting so only one coroutine mints.
        self.lock = asyncio.Lock()


_client_cache: dict[str, _AzureClient] = {}


async def get_client_state(config: AzureStorageConfig) -> _AzureClient:
    """Module-level get-or-build of the cached per-config client state. Shared by
    `AzureBlobStorage._state` and the account-level `AppContainerStore` — which owns no
    client of its own and resolves the shared client per-op from this cache (KTD-1), so it
    never captures a stale client and never closes the client out from under the backend."""
    fingerprint = _fingerprint(config)
    cached = _client_cache.get(fingerprint)
    if cached is not None:
        return cached
    state = _build_state(config)
    _client_cache[fingerprint] = state
    return state


async def get_delegation_key(
    state: _AzureClient, expires_in: timedelta, now: datetime, *, provider: str
) -> tuple[UserDelegationKey, datetime]:
    """Get-or-mint the cached user-delegation key on a client state, serialized by the state's
    lock so only one coroutine mints; a coroutine that awaited the lock reuses the fresh key.
    Shared by blob-level and container-level SAS signing so both share the one cached key."""
    async with state.lock:
        if (
            state.delegation_key is None
            or state.delegation_expiry is None
            or _needs_remint(now, state.delegation_expiry, expires_in)
        ):
            try:
                # Request Azure's maximum-allowed window — a HARD 7-day cap on (expiry - start).
                # The start is pulled back by the clock skew (to match the SAS `start`), so the
                # expiry is pulled in by the same skew to keep the total span at exactly 7d — a
                # bare `now + MAX_SIGNED_URL_TTL` would be 7d+skew and Azure rejects the mint.
                key = await state.service_client.get_user_delegation_key(
                    now - _CLOCK_SKEW, now + MAX_SIGNED_URL_TTL - _CLOCK_SKEW
                )
            except (HttpResponseError, ServiceRequestError) as exc:
                raise_azure(exc, op="sign", key="", provider=provider)
            state.delegation_key = key
            state.delegation_expiry = _delegation_expiry(key)
        return state.delegation_key, state.delegation_expiry


# Socket bounds on every blob call, matching the shape `settings/foundry.py` already uses
# for the model client (10s connect, a generous per-read idle bound). Without them a wedged
# socket hangs forever: a caller that thinks it has a ceiling does not, because the ceiling
# only bounds code that eventually returns. `read_timeout` is the SDK's per-read IDLE
# bound, not a cap on total transfer time, so a large bundle upload is unaffected — only a
# connection that has genuinely stopped producing bytes trips it.
_CONNECT_TIMEOUT_S: Final = 10.0
_READ_TIMEOUT_S: Final = 120.0


def _build_state(config: AzureStorageConfig) -> _AzureClient:
    # Secrets unwrapped only here, at the SDK boundary (security.md).
    if config.connection_string is not None:
        bsc = BlobServiceClient.from_connection_string(
            config.connection_string.get_secret_value(),
            connection_timeout=_CONNECT_TIMEOUT_S,
            read_timeout=_READ_TIMEOUT_S,
        )
        return _AzureClient(bsc, credential=None)
    if config.account_key is not None:
        bsc = BlobServiceClient(
            config.account_url,
            credential=config.account_key.get_secret_value(),
            connection_timeout=_CONNECT_TIMEOUT_S,
            read_timeout=_READ_TIMEOUT_S,
        )
        return _AzureClient(bsc, credential=None)
    credential = DefaultAzureCredential()
    bsc = BlobServiceClient(
        config.account_url,
        credential=credential,
        connection_timeout=_CONNECT_TIMEOUT_S,
        read_timeout=_READ_TIMEOUT_S,
    )
    return _AzureClient(bsc, credential=credential)


async def _close_state(fingerprint: str) -> None:
    state = _client_cache.pop(fingerprint, None)
    if state is not None:
        await state.service_client.close()
        if state.credential is not None:
            # An unclosed credential leaks its own aiohttp session.
            await state.credential.close()


async def close_all_clients() -> None:
    """Close every cached Azure client + credential. Called by `aclose_storage()`
    on shutdown. Each per-fingerprint close is isolated: a single failure is
    logged (fail-first.md — never a silent swallow) and the loop continues so one
    bad client never leaves the rest open."""
    for fingerprint in list(_client_cache):
        try:
            await _close_state(fingerprint)
        except Exception:
            # No credential value is ever logged — the fingerprint is a sha256
            # hash and the message is static.
            _log.exception("failed to close an azure storage client")


async def reset_client_for_tests() -> None:
    await close_all_clients()


class AzureBlobStorage(ObjectStorage):
    def __init__(self, config: AzureStorageConfig) -> None:
        super().__init__(provider=config.provider)
        self._config = config
        self._container = config.container
        self._account_url = config.account_url.rstrip("/")
        self._account_name = _account_name(config)
        self._fingerprint = _fingerprint(config)

    @classmethod
    def from_config(cls, config: AzureStorageConfig) -> AzureBlobStorage:
        # No client/credential opened here — the lazy cache opens on first op.
        return cls(config)

    async def _state(self) -> _AzureClient:
        return await get_client_state(self._config)

    def _sas_account_key(self) -> str | None:
        return account_signing_key(self._config)

    def _raise_azure(
        self, exc: HttpResponseError | ServiceRequestError, *, op: str, key: str
    ) -> NoReturn:
        raise_azure(exc, op=op, key=key, provider=self.provider)

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectMeta:
        if len(data) > MAX_PUT_BYTES:
            raise StorageUploadError(
                f"object exceeds the {MAX_PUT_BYTES}-byte put ceiling",
                provider=self.provider,
                key=key,
            )
        state = await self._state()
        blob_client = state.service_client.get_blob_client(self._container, key)
        content_settings = (
            ContentSettings(content_type=content_type) if content_type is not None else None
        )
        try:
            result = await blob_client.upload_blob(
                data,
                overwrite=True,
                content_settings=content_settings,
                metadata=normalize_metadata(metadata),
            )
        except ResourceNotFoundError as exc:
            raise StorageError(
                "Azure container not found", provider=self.provider, key=key
            ) from exc
        except (HttpResponseError, ServiceRequestError) as exc:
            self._raise_azure(exc, op="put", key=key)
        # upload_blob returns dict[str, Any] — narrow the best-effort etag (it MAY
        # be absent; callers needing a guaranteed identity head() afterwards).
        raw_etag = result.get("etag")
        return ObjectMeta(
            key=key,
            size=len(data),
            content_type=content_type,
            etag=_clean_etag(raw_etag if isinstance(raw_etag, str) else None),
            last_modified=None,
        )

    async def get(self, key: str) -> bytes:
        state = await self._state()
        blob_client = state.service_client.get_blob_client(self._container, key)
        try:
            downloader = await blob_client.download_blob()  # never pass encoding=
            data = await downloader.readall()
        except ResourceNotFoundError as exc:
            self._raise_not_found(exc, key=key)
        except (HttpResponseError, ServiceRequestError) as exc:
            self._raise_azure(exc, op="get", key=key)
        # With no encoding, readall() returns bytes; a str would corrupt binary, so
        # fail closed rather than encode() it (readall() is typed str | bytes).
        if not isinstance(data, bytes):
            raise StorageError(
                f"Azure get returned {type(data).__name__}, expected bytes",
                provider=self.provider,
                key=key,
            )
        return data

    async def head(self, key: str) -> ObjectMeta | None:
        state = await self._state()
        blob_client = state.service_client.get_blob_client(self._container, key)
        try:
            props = await blob_client.get_blob_properties()
        except ResourceNotFoundError as exc:
            if _error_code(exc) == _CONTAINER_NOT_FOUND:
                raise StorageError(
                    "Azure container not found", provider=self.provider, key=key
                ) from exc
            if not _is_confirmed_absent(exc):
                # An unnamed 404 is not an answer. `None` here means "the store says this object
                # does not exist", and every caller acts on it as such.
                raise StorageError(
                    "Azure head could not determine object state",
                    provider=self.provider,
                    key=key,
                ) from exc
            return None  # missing OBJECT → None
        except (HttpResponseError, ServiceRequestError) as exc:
            self._raise_azure(exc, op="head", key=key)
        # content_settings may be None at runtime even where the stub types it
        # non-optional — narrow before reading content_type.
        cs = props.content_settings
        content_type = cs.content_type if cs is not None else None
        return ObjectMeta(
            key=key,
            size=props.size,
            content_type=content_type,
            etag=_clean_etag(props.etag),
            last_modified=props.last_modified,
            metadata=dict(props.metadata or {}),
        )

    async def delete(self, key: str) -> None:
        state = await self._state()
        blob_client = state.service_client.get_blob_client(self._container, key)
        try:
            await blob_client.delete_blob()
        except ResourceNotFoundError as exc:
            if _error_code(exc) == _CONTAINER_NOT_FOUND:
                raise StorageError(
                    "Azure container not found", provider=self.provider, key=key
                ) from exc
            return  # missing BLOB → idempotent no-op (parity with S3)
        except (HttpResponseError, ServiceRequestError) as exc:
            self._raise_azure(exc, op="delete", key=key)

    async def list(
        self, prefix: str, *, page_size: int = DEFAULT_PAGE_SIZE, token: str | None = None
    ) -> ListPage:
        state = await self._state()
        container_client = state.service_client.get_container_client(self._container)
        item_paged = container_client.list_blobs(
            name_starts_with=prefix, results_per_page=page_size
        )
        # by_page() is typed as a bare AsyncIterator; the runtime object is an
        # AsyncPageIterator carrying .continuation_token (cast at this one boundary).
        page_iter = cast(
            "AsyncPageIterator[BlobProperties]", item_paged.by_page(continuation_token=token)
        )
        keys: list[str] = []
        try:
            async for page in page_iter:
                async for blob in page:
                    if blob.name is not None:  # None-filter → provably tuple[str, ...]
                        keys.append(blob.name)
                break  # one page per call
        except ResourceNotFoundError as exc:
            raise StorageError(
                "Azure container not found", provider=self.provider, key=prefix
            ) from exc
        except (HttpResponseError, ServiceRequestError) as exc:
            self._raise_azure(exc, op="list", key=prefix)
        return ListPage(keys=tuple(keys), next_token=page_iter.continuation_token)

    async def _signed_read_url_impl(self, key: str, *, expires_in: timedelta) -> str:
        state = await self._state()
        blob_url = f"{self._account_url}/{self._container}/{key}"
        now = _now()  # computed once, threaded through delegation-key minting
        if self._config.use_managed_identity:
            udk, key_expiry = await self._delegation_key(state, expires_in, now)
            sas = generate_blob_sas(
                account_name=self._account_name,
                container_name=self._container,
                blob_name=key,
                user_delegation_key=udk,
                permission=BlobSasPermissions(read=True),
                expiry=_sas_expiry(now, expires_in, key_expiry),
                start=now - _CLOCK_SKEW,
            )
        else:
            account_key = self._sas_account_key()
            if account_key is None:
                raise StorageSignError(
                    "no account key available for SAS signing",
                    provider=self.provider,
                    key=key,
                )
            sas = generate_blob_sas(
                account_name=self._account_name,
                container_name=self._container,
                blob_name=key,
                account_key=account_key,
                permission=BlobSasPermissions(read=True),
                expiry=now + expires_in,
                start=now - _CLOCK_SKEW,
            )
        return f"{blob_url}?{sas}"

    async def _delegation_key(
        self, state: _AzureClient, expires_in: timedelta, now: datetime
    ) -> tuple[UserDelegationKey, datetime]:
        return await get_delegation_key(state, expires_in, now, provider=self.provider)

    def _raise_not_found(self, exc: ResourceNotFoundError, *, key: str) -> NoReturn:
        if _error_code(exc) == _CONTAINER_NOT_FOUND:
            raise StorageError(
                "Azure container not found", provider=self.provider, key=key
            ) from exc
        if not _is_confirmed_absent(exc):
            # `StorageNotFoundError` is the ABSENCE answer, and callers branch on it as one
            # (`attachments.py` tells the user their file is gone; the restore path provisions a
            # blank app). An unnamed 404 has not earned it — fail ambiguous instead.
            raise StorageError(
                "Azure get could not determine object state", provider=self.provider, key=key
            ) from exc
        raise StorageNotFoundError("object not found", provider=self.provider, key=key) from exc

    async def aclose(self) -> None:
        await _close_state(self._fingerprint)
