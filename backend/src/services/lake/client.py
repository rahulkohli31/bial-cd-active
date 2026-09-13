"""The client that reaches one connector's lake: list it, download from it, and translate its
failures — with a credential that names its identity out loud.

WHAT IT DOES AND WHAT IT REFUSES TO DO. `list_files()` returns every entry the container holds
under the configured prefix, as `(name, size)` pairs, IN THE ORDER AZURE PRODUCED THEM, with no
filtering whatsoever. Deciding what a listing MEANS — which entries are files, which are folders,
which are failed loads, and which fall inside a window — is `window.py`'s job, and it is a pure
function precisely so every trap the lake sets is a table-driven unit test. A client that
pre-filtered would hide those traps from the tests that exist to catch them.

`download()` returns bytes, verbatim. Nothing here parses parquet, and nothing in the backend
does: the transfer this feeds is a byte-for-byte copy, so the plain Blob SDK is enough and no
Python dependency is added by this feature.

THE CREDENTIAL IS `ManagedIdentityCredential(client_id=…)`, AND NEVER `DefaultAzureCredential`.
The instinct on reading this file next to `services/storage/azure_backend.py` will be to
"harmonise" it. Do not. `DefaultAzureCredential` reads the ambient `AZURE_CLIENT_ID`, which in a
container app belongs to the PLATFORM's own identity — so it would ask as the wrong principal and
be refused with a 403 that looks exactly like a missing role assignment on the right one. That
module keeps `DefaultAzureCredential` because the platform's own identity reaching the platform's
own storage is a different question; this one names its identity because the answer depends on
which identity asks.

SHAPE MIRRORED FROM `azure_backend.py`: a lazily-built, fingerprint-keyed module cache holding one
long-lived service client and one credential per config, explicit connect/read timeouts, and
`aclose_lake()` / `reset_lake_for_tests()` as the shutdown and test hooks. An unclosed credential
leaks its own aiohttp session, so both are closed.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Final, NamedTuple, NoReturn
from urllib.parse import urlsplit

import structlog
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity.aio import ManagedIdentityCredential
from azure.storage.blob.aio import BlobServiceClient

from src.services.lake.config import LakeConfig
from src.services.lake.errors import LakeAuthError, LakeError, LakeNotFoundError

_log = structlog.get_logger()

# The two absences Azure NAMES on a blob operation (`x-ms-error-code`). Anything else arriving as
# a `ResourceNotFoundError` — a code-less 404 from a proxy between us and the account — is a
# question that failed, not an answer about the object. Same rule as the storage backend's
# `_is_confirmed_absent`, and it matters more here: "the container is empty" and "we could not
# ask" are the two readings this whole feature has to keep apart.
_BLOB_NOT_FOUND: Final = "BlobNotFound"
_CONTAINER_NOT_FOUND: Final = "ContainerNotFound"

# Socket bounds on every call, matching `azure_backend.py`. Without them a wedged socket hangs
# forever, and the transfer that calls this is a detached background task with nothing watching
# it. `read_timeout` is the SDK's per-read IDLE bound, not a cap on total transfer time, so a
# large parquet download is unaffected — only a connection that has stopped producing bytes.
_CONNECT_TIMEOUT_S: Final = 10.0
_READ_TIMEOUT_S: Final = 120.0


class LakeEntry(NamedTuple):
    """One entry in a container listing, exactly as Azure reported it.

    `size` is `content_length` with a `None` folded to zero — which is NOT a loss of information
    here, because a hierarchical-namespace account reports its DIRECTORIES as zero-length entries
    too, and `window.py` already has to tell a folder from a failed load by NAME rather than by
    size. A separate `None` case would be a third state nothing branches on."""

    name: str
    size: int


def _account(config: LakeConfig) -> str:
    """The storage account's name, for a diagnostic message. The first label of the host."""
    return (urlsplit(config.url).hostname or "").split(".")[0]


def _fingerprint(config: LakeConfig) -> str:
    """The cache key: everything that would make a DIFFERENT client. The resource id is
    deliberately absent — it never reaches a credential or a client, only an ARM container spec."""
    material = (config.account_url, config.container, config.identity_client_id)
    return sha256("\x00".join(material).encode()).hexdigest()


def _error_code(exc: HttpResponseError) -> str | None:
    code = getattr(exc, "error_code", None)
    return code if isinstance(code, str) else None


class _LakeClientState:
    """Cached per-config state: the long-lived service client and the credential to close."""

    def __init__(
        self, service_client: BlobServiceClient, *, credential: ManagedIdentityCredential
    ) -> None:
        self.service_client = service_client
        self.credential = credential


_client_cache: dict[str, _LakeClientState] = {}


def _build_state(config: LakeConfig) -> _LakeClientState:
    # THE ONE LINE THIS MODULE EXISTS FOR. `client_id` is explicit and not optional: see the
    # module docblock, and `LakeAuthError` for what the failure looks like without it.
    credential = ManagedIdentityCredential(client_id=config.identity_client_id)
    service_client = BlobServiceClient(
        config.account_url,
        credential=credential,
        connection_timeout=_CONNECT_TIMEOUT_S,
        read_timeout=_READ_TIMEOUT_S,
    )
    return _LakeClientState(service_client, credential=credential)


def _state_for(config: LakeConfig) -> _LakeClientState:
    fingerprint = _fingerprint(config)
    cached = _client_cache.get(fingerprint)
    if cached is not None:
        return cached
    state = _build_state(config)
    _client_cache[fingerprint] = state
    return state


async def _close_state(fingerprint: str) -> None:
    state = _client_cache.pop(fingerprint, None)
    if state is not None:
        await state.service_client.close()
        # An unclosed credential leaks its own aiohttp session.
        await state.credential.close()


async def aclose_lake() -> None:
    """Close every cached lake client and credential, and drop the accessor's singleton. Wired
    into the FastAPI lifespan shutdown.

    Each per-fingerprint close is isolated: a single failure is logged — never silently
    swallowed — and the loop continues, so one bad client never leaves the rest open."""
    global _lake_singleton
    for fingerprint in list(_client_cache):
        try:
            await _close_state(fingerprint)
        except Exception:
            # The fingerprint is a sha256 and the message is static; no coordinate is logged
            # here because a shutdown path has no operator watching it.
            _log.exception("failed to close a connector lake client")
    _lake_singleton = None


async def reset_lake_for_tests() -> None:
    """Drop BOTH layers so a suite building clients from different configs never inherits the
    previous test's fake."""
    await aclose_lake()


class LakeClient:
    """One configured lake, read-only.

    Holds no client of its own — it resolves the shared, fingerprint-cached client per operation,
    the same way `AppContainerStore` borrows the storage backend's. So constructing one is free,
    and closing the module's clients does not leave a captured stale handle behind."""

    def __init__(self, config: LakeConfig) -> None:
        self._config = config
        self._account = _account(config)

    def _raise(
        self,
        exc: HttpResponseError | ServiceRequestError | ServiceResponseError,
        *,
        op: str,
        target: str,
    ) -> NoReturn:
        """SANITISED re-raise that KEEPS the coordinates and drops the SDK's own text.

        `op` and `target` are ours; the raw exception rides only as `__cause__`. A 403 or an
        explicit credential failure becomes `LakeAuthError`, whose message names the identity —
        see that class for why that one field is worth the departure from the storage backend's
        fully-static rule."""
        coordinates = {
            "account": self._account,
            "container": self._config.container,
            "client_id": self._config.identity_client_id,
        }
        if isinstance(exc, ClientAuthenticationError) or (
            isinstance(exc, HttpResponseError) and exc.status_code == 403
        ):
            # LOGGED AS WELL AS RAISED. The caller of this path is a detached background task
            # whose failures are swallowed on purpose, so a raise alone would reach nobody. A
            # preventive comment in a worked example does not help the operator staring at a
            # failure that has two indistinguishable causes; this line does.
            _log.warning(
                "connector_lake_access_denied",
                op=op,
                detail=(
                    "the lake refused this identity: either the identity is not the one the "
                    "role was granted to, or it holds no Storage Blob Data Reader on this "
                    "container. The client id below is the one that asked."
                ),
                **coordinates,
            )
            raise LakeAuthError(
                f"the lake refused {op} as identity {self._config.identity_client_id} "
                f"on {self._account}/{self._config.container}",
                **coordinates,
            ) from exc
        raise LakeError(
            f"lake {op} failed on {self._account}/{self._config.container}/{target}",
            **coordinates,
        ) from exc

    def _raise_absent(self, exc: ResourceNotFoundError, *, op: str, target: str) -> NoReturn:
        """A 404 Azure NAMED becomes `LakeNotFoundError`; an unnamed one stays ambiguous.

        The order of the two checks is not cosmetic: a missing CONTAINER means the coordinates
        are wrong, which is a different sentence from a missing file, and it is the one an
        operator most needs to read."""
        code = _error_code(exc)
        if code == _CONTAINER_NOT_FOUND:
            raise LakeNotFoundError(
                f"the lake has no container {self._config.container} on {self._account}",
                account=self._account,
                container=self._config.container,
                client_id=self._config.identity_client_id,
            ) from exc
        if code == _BLOB_NOT_FOUND:
            raise LakeNotFoundError(
                f"the lake has no file {target}",
                account=self._account,
                container=self._config.container,
                client_id=self._config.identity_client_id,
            ) from exc
        self._raise(exc, op=op, target=target)

    async def list_files(self) -> tuple[LakeEntry, ...]:
        """Every entry under the configured prefix, as `(name, size)`, unfiltered and in the
        order Azure produced them.

        NO FILTERING, AND NO PAGING KNOB. The container holds a few dozen files; the SDK's async
        iterator pages transparently, and a caller that wanted a page boundary would be deciding
        what a listing means, which is `window.py`'s job. The whole listing is what the window
        resolver needs — it selects by DATE, so it cannot know in advance how far back to read.
        """
        state = _state_for(self._config)
        container_client = state.service_client.get_container_client(self._config.container)
        entries: list[LakeEntry] = []
        try:
            async for blob in container_client.list_blobs(name_starts_with=self._config.prefix):
                # `list_blobs` yields `BlobProperties` DIRECTLY — `.name` and `.size`, flat.
                # It is not the JS SDK's `blob.properties.contentLength`, which is the shape the
                # worked example in the golden template reads and the shape a reader porting from
                # it will assume. A fake built the wrong way round passes every behavioural test
                # in this package and fails against the real SDK; the type gate is what caught it.
                if blob.name is None:  # None-filter -> provably tuple[LakeEntry, ...]
                    continue
                entries.append(LakeEntry(blob.name, blob.size or 0))
        # ORDER: MOST SPECIFIC FIRST. `ResourceNotFoundError` IS an `HttpResponseError`, so a
        # broader clause above this one would swallow the confirmed-absent container and report
        # it as a generic failure. That inversion is a shipped defect this repository has
        # already paid for once, in a bounded retry made unreachable by a parent-class `except`.
        #
        # `ServiceResponseError` IS THE THIRD ONE, and it is easy to leave out because it is not
        # an `HttpResponseError` — it is a SIBLING of `ServiceRequestError` under `AzureError`.
        # The aiohttp transport raises it for a read timeout, which is precisely the failure this
        # module's own `_READ_TIMEOUT_S` manufactures. Without it a slow lake escapes the
        # sanitised re-raise AND `transfer_window_or_log`'s `(LakeError, RedisError)`, landing as
        # a raw stack with none of the coordinates. `aca_publish.py` and `sandbox/client.py` both
        # already catch all three.
        except ResourceNotFoundError as exc:
            self._raise_absent(exc, op="list", target=self._config.prefix)
        except (HttpResponseError, ServiceRequestError, ServiceResponseError) as exc:  # fmt: skip
            self._raise(exc, op="list", target=self._config.prefix)
        return tuple(entries)

    async def download(self, name: str) -> bytes:
        """The file's bytes, verbatim. `name` is a full blob name as `list_files` reported it.

        NEVER `encoding=`: with one, the SDK decodes and a parquet file comes back mangled. The
        `isinstance` below is not defensive noise — `readall()` is typed `str | bytes`, and a
        `str` here would be silent corruption written straight into Redis."""
        state = _state_for(self._config)
        blob_client = state.service_client.get_blob_client(self._config.container, name)
        try:
            downloader = await blob_client.download_blob()
            data = await downloader.readall()
        except ResourceNotFoundError as exc:
            self._raise_absent(exc, op="download", target=name)
        except (HttpResponseError, ServiceRequestError, ServiceResponseError) as exc:  # fmt: skip
            self._raise(exc, op="download", target=name)
        if not isinstance(data, bytes):
            raise LakeError(
                f"the lake returned {type(data).__name__} for {name}, expected bytes",
                account=self._account,
                container=self._config.container,
                client_id=self._config.identity_client_id,
            )
        return data


_lake_singleton: LakeClient | None = None


def get_lake() -> LakeClient | None:
    """The configured lake (app-level singleton), or **`None` when no lake is configured**.

    `None` rather than a raise, and this is the shape every caller in this feature branches on:
    a developer machine has no lake, a deployment that has not been given one is a supported
    posture, and every path here is background work nothing waits for. Compare
    `get_app_container_store()`, which answers the same way for the same reason —
    `get_storage()`'s raise exists because a request path needs a status to return."""
    global _lake_singleton
    if _lake_singleton is None:
        from src.config import settings  # lazy: avoid an import cycle via src.config

        if settings.connector_lake is None:
            return None
        _lake_singleton = LakeClient(settings.connector_lake)
    return _lake_singleton
