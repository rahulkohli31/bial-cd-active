"""The lake client: what it lists, what it downloads, and what it does with a failure.

THREE THINGS THIS FILE IS ACTUALLY FOR, none of them "does the SDK work".

1. **The listing is unfiltered.** Every trap the lake sets — the month folder, the directory
   placeholders, the zero-byte stubs — is only VISIBLE to `window.py` if the client hands over
   what Azure said, in the order Azure said it. A client that quietly dropped zero-length
   entries would make four of `test_window.py`'s cases unreachable while every test still passed.
2. **The exception clauses are ordered most-specific-first.** `ResourceNotFoundError` IS an
   `HttpResponseError`, so a broader clause above it swallows a confirmed absence. This repository
   has already shipped that exact inversion once (a bounded retry made unreachable by a
   parent-class `except`) and the test that "proved" the fix fabricated an exception the SDK never
   raises. So every exception below is built from the SDK's REAL classes.
3. **A failure carries the coordinates and not the SDK's text.** The account, the container and
   the client id are labels, and they are the only thing that tells a wrong identity apart from a
   missing role assignment. The raw exception message is not ours and rides only as `__cause__`.

The fakes here are async iterators and objects shaped like the SDK's, injected by replacing the
module's client cache — not by monkeypatching the SDK. That keeps the code under test on its real
call path (`get_container_client`, `list_blobs`, `download_blob`, `readall`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import cast

import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity.aio import ManagedIdentityCredential
from azure.storage.blob import BlobProperties
from azure.storage.blob.aio import BlobServiceClient

from src.services.lake import client as lake_client
from src.services.lake.client import LakeClient, LakeEntry, reset_lake_for_tests
from src.services.lake.config import LakeConfig
from src.services.lake.errors import LakeAuthError, LakeError, LakeNotFoundError

_ACCOUNT = "alakeaccount"
_CONTAINER = "acontainer"
_PREFIX = "AOS/tb_flight_fact_report/"
_CLIENT_ID = "52b74947-0621-46e2-a523-a6b466f47c33"
# A plausible-looking SDK message. The point of the constant is that NONE of it may reach a
# raised message — an SDK error body is unbounded, is not ours, and has no audience here.
_SDK_NOISE = "ServerBusy: the account is being throttled, RequestId:9f3c-not-ours"


def _config() -> LakeConfig:
    return LakeConfig(
        url=f"https://{_ACCOUNT}.blob.core.windows.net/{_CONTAINER}/{_PREFIX}",
        identity_client_id=_CLIENT_ID,
        identity_resource_id="/subscriptions/x/resourcegroups/y/providers/z/id/an-identity",
    )


# --- the fakes, shaped like the SDK ------------------------------------------------------------


def _blob(name: str | None, size: int | None) -> BlobProperties:
    """One listing entry, built from the SDK's REAL `BlobProperties`.

    NOT a hand-rolled stand-in, and the difference is not pedantry: `list_blobs` yields
    `BlobProperties` FLAT — `.name` and `.size` — while the JS SDK the golden template's worked
    example uses nests it as `blob.properties.contentLength`. The first draft of this file
    modelled the JS shape, every behavioural test passed, and the client was reading an attribute
    that does not exist on the real object. Using the SDK's own class is what makes the fake
    unable to lie about its shape."""
    properties = BlobProperties(name=name)
    # `size` through `setattr` because the SDK's own type says `int` while the runtime object
    # defaults it to `None` — `BlobProperties(name="x").size is None`, checked. The stub and the
    # object disagree, which is precisely why `client.py` folds a `None` to zero rather than
    # trusting the annotation; writing `None` here is what makes that branch reachable at all.
    setattr(properties, "size", size)  # noqa: B010
    return properties


def _with_code(exc: HttpResponseError, code: str) -> HttpResponseError:
    """Attach an `x-ms-error-code` the way the storage SDK actually does.

    `azure-core` does not declare `error_code` at all; `azure.storage.blob`'s
    `process_storage_error` assigns it onto the exception INSTANCE after construction
    (`error.error_code = error_code`). So the production code reads it with `getattr` and this
    helper writes it with `setattr` — both for the same reason, and neither is a way around a
    type checker: the attribute genuinely is not on the class, in either direction."""
    setattr(exc, "error_code", code)  # noqa: B010
    return exc


class _FakeDownloader:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    async def readall(self) -> object:
        return self._payload


class _FakeBlobClient:
    def __init__(self, payload: object, raises: Exception | None) -> None:
        self._payload = payload
        self._raises = raises

    async def download_blob(self) -> _FakeDownloader:
        if self._raises is not None:
            raise self._raises
        return _FakeDownloader(self._payload)


class _FakeContainerClient:
    def __init__(self, blobs: Iterable[BlobProperties], raises: Exception | None) -> None:
        self._blobs = list(blobs)
        self._raises = raises
        self.prefix_asked: str | None = None

    def list_blobs(self, *, name_starts_with: str) -> AsyncIterator[BlobProperties]:
        self.prefix_asked = name_starts_with
        blobs, raises = self._blobs, self._raises

        async def _iterate() -> AsyncIterator[BlobProperties]:
            if raises is not None:
                raise raises
            for blob in blobs:
                yield blob

        return _iterate()


class _FakeServiceClient:
    def __init__(
        self,
        *,
        blobs: Iterable[BlobProperties] = (),
        list_raises: Exception | None = None,
        payload: object = b"",
        download_raises: Exception | None = None,
    ) -> None:
        self.container = _FakeContainerClient(blobs, list_raises)
        self._payload = payload
        self._download_raises = download_raises
        self.closed = False

    def get_container_client(self, name: str) -> _FakeContainerClient:
        return self.container

    def get_blob_client(self, container: str, name: str) -> _FakeBlobClient:
        return _FakeBlobClient(self._payload, self._download_raises)

    async def close(self) -> None:
        self.closed = True


class _FakeCredential:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
async def _no_cached_client() -> AsyncIterator[None]:
    """Every test starts and ends with an empty client cache.

    Not hygiene for its own sake: the cache is keyed on a fingerprint of the coordinates, so two
    tests using the same config would silently share one fake and the second would assert against
    the first's state."""
    await reset_lake_for_tests()
    yield
    await reset_lake_for_tests()


def _bind(service: _FakeServiceClient) -> _FakeCredential:
    """Install `service` as the cached client for `_config()`, exactly as `_build_state` would."""
    credential = _FakeCredential()
    # The two casts are the whole of the injection: the fakes are structurally what the client
    # calls (`get_container_client`, `get_blob_client`, `close`) but are not the SDK's nominal
    # types. Casting here rather than typing the cache loosely keeps `client.py` itself strict.
    state = lake_client._LakeClientState(
        cast("BlobServiceClient", service),
        credential=cast("ManagedIdentityCredential", credential),
    )
    lake_client._client_cache[lake_client._fingerprint(_config())] = state
    return credential


# --- listing ------------------------------------------------------------------------------------


async def test_the_listing_is_handed_over_unfiltered_and_in_order() -> None:
    """★ EVERY ENTRY, INCLUDING THE ONES THAT ARE OBVIOUSLY NOT FILES.

    The four rows below are the shapes `window.py` has to tell apart: a real file, a directory
    placeholder (zero length, folder-shaped name), a failed load that left a zero-byte stub, and
    a file whose name does not match the pattern at all. If the client dropped any of them, the
    resolver's tests would be asserting against a listing the lake never produces."""
    service = _FakeServiceClient(
        blobs=[
            _blob(f"{_PREFIX}2026/SEPTEMBER/tb_flight_fact_report_20260831.parquet", 4_012),
            _blob(f"{_PREFIX}2026/SEPTEMBER", 0),
            _blob(f"{_PREFIX}2026/SEPTEMBER/tb_flight_fact_report_20260901.parquet", 0),
            _blob(f"{_PREFIX}2026/SEPTEMBER/_SUCCESS", 0),
        ]
    )
    _bind(service)

    entries = await LakeClient(_config()).list_files()

    assert entries == (
        LakeEntry(f"{_PREFIX}2026/SEPTEMBER/tb_flight_fact_report_20260831.parquet", 4_012),
        LakeEntry(f"{_PREFIX}2026/SEPTEMBER", 0),
        LakeEntry(f"{_PREFIX}2026/SEPTEMBER/tb_flight_fact_report_20260901.parquet", 0),
        LakeEntry(f"{_PREFIX}2026/SEPTEMBER/_SUCCESS", 0),
    )
    assert service.container.prefix_asked == _PREFIX


async def test_a_missing_content_length_reads_as_zero_rather_than_none() -> None:
    """`content_length` is typed optional. A `None` here would be a third state nothing branches
    on — and the resolver already has to tell a folder from a broken file by NAME, so folding it
    to zero loses nothing it could have used."""
    _bind(_FakeServiceClient(blobs=[_blob(f"{_PREFIX}a.parquet", None)]))

    assert await LakeClient(_config()).list_files() == (LakeEntry(f"{_PREFIX}a.parquet", 0),)


async def test_an_entry_with_no_name_is_dropped() -> None:
    """`name` is typed optional too, and an entry without one cannot be downloaded or dated.
    Dropped rather than carried as an empty string, which would look like a real prefix."""
    _bind(_FakeServiceClient(blobs=[_blob(None, 10), _blob(f"{_PREFIX}a.parquet", 10)]))

    assert await LakeClient(_config()).list_files() == (LakeEntry(f"{_PREFIX}a.parquet", 10),)


async def test_an_empty_container_lists_nothing_and_is_not_an_error() -> None:
    """A lake that has not loaded yet is not a fault. This is the one case that MUST NOT raise,
    because the resolver's answer for it — an empty window — is a legitimate product state."""
    _bind(_FakeServiceClient(blobs=[]))

    assert await LakeClient(_config()).list_files() == ()


# --- downloading --------------------------------------------------------------------------------


async def test_a_download_returns_the_exact_bytes() -> None:
    """★ BYTE-FOR-BYTE. The whole transfer this feeds is a verbatim copy, so a payload spanning
    the full 8-bit range — including a null byte and a UTF-8-invalid sequence — has to survive."""
    payload = bytes(range(256)) + b"\x00PAR1\xff\xfe"
    _bind(_FakeServiceClient(payload=payload))

    assert await LakeClient(_config()).download(f"{_PREFIX}a.parquet") == payload


async def test_a_download_that_comes_back_as_text_is_refused_rather_than_encoded() -> None:
    """`readall()` is typed `str | bytes`. A `str` here means something decoded the stream, and
    encoding it back would be silent corruption written straight into Redis — so it fails
    closed."""
    _bind(_FakeServiceClient(payload="PAR1 but decoded"))

    with pytest.raises(LakeError) as raised:
        await LakeClient(_config()).download(f"{_PREFIX}a.parquet")

    assert "expected bytes" in str(raised.value)


# --- failures -----------------------------------------------------------------------------------


async def test_a_denial_names_the_identity_that_asked() -> None:
    """★ THE ONE DIAGNOSTIC THE SITUATION AFFORDS. A wrong identity and a missing role assignment
    both come back 403. The client id in the message is what settles which, in one reading."""
    _bind(_FakeServiceClient(list_raises=ClientAuthenticationError(message=_SDK_NOISE)))

    with pytest.raises(LakeAuthError) as raised:
        await LakeClient(_config()).list_files()

    assert _CLIENT_ID in str(raised.value)
    assert _ACCOUNT in str(raised.value) and _CONTAINER in str(raised.value)
    assert raised.value.client_id == _CLIENT_ID


async def test_a_403_is_a_denial_even_without_an_authentication_exception() -> None:
    """Azure answers a missing data-plane role with a plain 403 `HttpResponseError`, not with
    `ClientAuthenticationError` — that one is for a credential that could not be obtained at all.
    Both are the same sentence to an operator, so both map to the same type."""
    denied = HttpResponseError(message=_SDK_NOISE)
    denied.status_code = 403
    _bind(_FakeServiceClient(list_raises=denied))

    with pytest.raises(LakeAuthError):
        await LakeClient(_config()).list_files()


async def test_no_sdk_text_reaches_the_raised_message() -> None:
    """Asserted as an ABSENCE of the SDK's own words, paired with a positive assertion that the
    message says something — an assert-absence test passes just as happily on a message that is
    empty because the code crashed before composing one."""
    _bind(_FakeServiceClient(list_raises=HttpResponseError(message=_SDK_NOISE)))

    with pytest.raises(LakeError) as raised:
        await LakeClient(_config()).list_files()

    assert "ServerBusy" not in str(raised.value)
    assert "RequestId" not in str(raised.value)
    assert "lake list failed" in str(raised.value)
    # The raw exception is not lost — it rides as the cause, where a log can reach it.
    assert isinstance(raised.value.__cause__, HttpResponseError)


async def test_a_named_absent_container_is_told_apart_from_a_generic_failure() -> None:
    """★ THE EXCEPTION-ORDERING TEST, built from the SDK's REAL hierarchy.

    `ResourceNotFoundError` subclasses `HttpResponseError`. Move the broad clause above the
    specific one in `client.py` and this goes red while every other test here stays green —
    which is the shape of the defect this repository has already paid for once."""
    assert issubclass(ResourceNotFoundError, HttpResponseError), (
        "if this ever stops being true the ordering below stops being load-bearing"
    )
    absent = _with_code(ResourceNotFoundError(message=_SDK_NOISE), "ContainerNotFound")
    _bind(_FakeServiceClient(list_raises=absent))

    with pytest.raises(LakeNotFoundError) as raised:
        await LakeClient(_config()).list_files()

    assert _CONTAINER in str(raised.value)


async def test_an_unnamed_404_stays_ambiguous_rather_than_claiming_absence() -> None:
    """A 404 with no `x-ms-error-code` is a question that FAILED — a proxy or a WAF between us and
    the account — not an answer about the container. Claiming absence here would tell an operator
    the lake is empty when it is unreachable."""
    unnamed = ResourceNotFoundError(message=_SDK_NOISE)
    _bind(_FakeServiceClient(list_raises=unnamed))

    with pytest.raises(LakeError) as raised:
        await LakeClient(_config()).list_files()

    assert not isinstance(raised.value, LakeNotFoundError)


async def test_a_named_absent_file_on_download_is_a_not_found() -> None:
    absent = _with_code(ResourceNotFoundError(message=_SDK_NOISE), "BlobNotFound")
    _bind(_FakeServiceClient(download_raises=absent))

    with pytest.raises(LakeNotFoundError) as raised:
        await LakeClient(_config()).download(f"{_PREFIX}gone.parquet")

    assert "gone.parquet" in str(raised.value)


async def test_a_transport_failure_is_the_apps_own_error_type() -> None:
    """`ServiceRequestError` never reaches the server at all, so it carries no status code and no
    error code — without its own clause it escapes every handler here and surfaces as a bare
    exception on a background path that swallows nothing else."""
    _bind(_FakeServiceClient(list_raises=ServiceRequestError(message=_SDK_NOISE)))

    with pytest.raises(LakeError):
        await LakeClient(_config()).list_files()


async def test_a_read_timeout_is_the_apps_own_error_type_too() -> None:
    """★ `ServiceResponseError` IS NOT AN `HttpResponseError` — it is a sibling of
    `ServiceRequestError` under `AzureError`, and it is what the aiohttp transport raises when the
    server accepted the request and then stopped answering. This module sets `_READ_TIMEOUT_S`
    itself, so it manufactures exactly that failure; a clause that named only the other two would
    let a slow lake escape both the sanitised re-raise here AND `transfer_window_or_log`'s
    `(LakeError, RedisError)`, landing as a raw stack with none of the coordinates on it.

    Asserted on both verbs because the two `except` chains are written out separately."""
    _bind(_FakeServiceClient(list_raises=ServiceResponseError(message=_SDK_NOISE)))
    with pytest.raises(LakeError) as listing:
        await LakeClient(_config()).list_files()
    assert _SDK_NOISE not in str(listing.value)

    await reset_lake_for_tests()
    _bind(_FakeServiceClient(download_raises=ServiceResponseError(message=_SDK_NOISE)))
    with pytest.raises(LakeError) as download:
        await LakeClient(_config()).download("slow.parquet")
    assert "slow.parquet" in str(download.value)


# --- the cache ----------------------------------------------------------------------------------


async def test_resetting_drops_the_cached_client_and_closes_both_halves() -> None:
    """★ Otherwise a second test inherits the first's fake — and in production a shutdown leaks
    the credential's own aiohttp session alongside the service client's pool."""
    service = _FakeServiceClient(blobs=[])
    credential = _bind(service)

    await LakeClient(_config()).list_files()
    await reset_lake_for_tests()

    assert service.closed is True
    assert credential.closed is True
    assert lake_client._client_cache == {}


async def test_the_same_config_reuses_one_client() -> None:
    """The cache is keyed on a fingerprint of the coordinates, so two `LakeClient` instances over
    the same config share one long-lived SDK client rather than opening a pool each."""
    service = _FakeServiceClient(blobs=[_blob(f"{_PREFIX}a.parquet", 1)])
    _bind(service)

    await LakeClient(_config()).list_files()
    await LakeClient(_config()).list_files()

    assert len(lake_client._client_cache) == 1


# --- the accessor -------------------------------------------------------------------------------


async def test_an_unconfigured_lake_answers_none_rather_than_raising() -> None:
    """★ `None`, NOT AN EXCEPTION, and this is the shape every caller in the feature branches on.

    A developer machine has no lake and a deployment that has not been given one is a supported
    posture — so "no lake" is an ANSWER, not a failure. `get_storage()` raises instead because a
    request path needs a status to return; every path here is background work nothing waits for,
    and `get_app_container_store()` answers `None` for exactly the same reason.

    Deliberately binds no lake fixture: with one bound this branch is unreachable BY
    CONSTRUCTION, which is how a documented off-posture stays broken on every deployment that
    has the dependency switched off."""
    from src.config import settings

    assert settings.connector_lake is None, ".env.test must not configure a lake"
    assert lake_client.get_lake() is None
