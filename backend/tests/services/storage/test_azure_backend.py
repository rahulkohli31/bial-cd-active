"""Azure backend unit tests — network-free. Pure helpers (account-name parsing,
delegation re-mint logic, SAS expiry capping) are tested directly; behavioral
tests inject a mock BlobServiceClient into the module client cache so put/head/
delete and the service-vs-user-delegation SAS branch are observable without
Azure. Round-trips against Azurite live in the opt-in integration suite
(`-m integration`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from azure.core.exceptions import ResourceNotFoundError
from pydantic import SecretStr

from src.services.storage import azure_backend
from src.services.storage.azure_backend import (
    AzureBlobStorage,
    _account_name,
    _clean_etag,
    _conn_field,
    _fingerprint,
    _needs_remint,
    _sas_expiry,
)
from src.services.storage.config import AzureStorageConfig
from src.services.storage.errors import (
    StorageError,
    StorageNotFoundError,
    StorageSignError,
    StorageUploadError,
)

_AZURITE_CONN = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=c2VjcmV0a2V5;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1"
)


def _azure(**overrides: object) -> AzureStorageConfig:
    base: dict[str, object] = {
        "account_url": "https://acct.blob.core.windows.net",
        "container": "c",
        "account_key": SecretStr("a2V5"),
    }
    return AzureStorageConfig.model_validate({**base, **overrides})


def _not_found(code: str) -> ResourceNotFoundError:
    exc = ResourceNotFoundError("not found")
    # error_code is set dynamically by azure-core from the response; set it via
    # setattr so the (incomplete) stub doesn't flag the assignment.
    setattr(exc, "error_code", code)
    return exc


def _uncoded_not_found() -> ResourceNotFoundError:
    """A 404 Azure never named — what a proxy/WAF blip between us and the account raises. The SDK
    populates `error_code` from the `x-ms-error-code` RESPONSE HEADER, so a 404 that did not come
    from Azure's own blob layer carries none, while arriving as the very same exception type.

    Every other fake here returns/raises absence only for a genuinely-missing key, which is why the
    suite could not express a FALSE absent before this: the fakes agreed with the code's assumption
    that ResourceNotFoundError means the object is gone."""
    return ResourceNotFoundError("not found")


def _install_mock(config: AzureStorageConfig, service_client: Any) -> None:
    fp = _fingerprint(config)
    azure_backend._client_cache[fp] = azure_backend._AzureClient(service_client, credential=None)


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    # The module client cache is process-global; mock entries hold no real
    # resources, so a plain clear suffices between tests.
    azure_backend._client_cache.clear()
    yield
    azure_backend._client_cache.clear()


# --- pure helpers ------------------------------------------------------------


def test_account_name_from_url() -> None:
    assert _account_name(_azure()) == "acct"


def test_account_name_from_connection_string() -> None:
    # Azurite path-style URL has no account label in the host; the name comes
    # from the connection string's AccountName.
    cfg = _azure(account_key=None, connection_string=SecretStr(_AZURITE_CONN))
    assert _account_name(cfg) == "devstoreaccount1"


def test_conn_field_extracts_account_key() -> None:
    assert _conn_field(_AZURITE_CONN, "AccountKey") == "c2VjcmV0a2V5"
    assert _conn_field(_AZURITE_CONN, "Missing") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [('"0x8DABC"', "0x8DABC"), ("", None), (None, None)],
)
def test_clean_etag(raw: str | None, expected: str | None) -> None:
    assert _clean_etag(raw) == expected


def test_needs_remint() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    # Key expires in 30m, request wants 1h → can't cover → re-mint.
    assert _needs_remint(now, now + timedelta(minutes=30), timedelta(hours=1)) is True
    # Key expires in 6d, request wants 1h → covered → no re-mint.
    assert _needs_remint(now, now + timedelta(days=6), timedelta(hours=1)) is False


def test_sas_expiry_caps_at_key_expiry() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    key_expiry = now + timedelta(hours=2)
    # Requested 5h but key only lives 2h → capped at the key's expiry.
    assert _sas_expiry(now, timedelta(hours=5), key_expiry) == key_expiry
    # Requested 1h, key lives 2h → full requested duration.
    assert _sas_expiry(now, timedelta(hours=1), key_expiry) == now + timedelta(hours=1)


def test_fingerprint_differs_by_auth_mode() -> None:
    key_cfg = _azure()
    conn_cfg = _azure(account_key=None, connection_string=SecretStr(_AZURITE_CONN))
    mi_cfg = _azure(account_key=None, use_managed_identity=True)
    fps = {_fingerprint(key_cfg), _fingerprint(conn_cfg), _fingerprint(mi_cfg)}
    assert len(fps) == 3


# --- pre-flight guards -------------------------------------------------------


async def test_put_rejects_oversized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(azure_backend, "MAX_PUT_BYTES", 4)
    _install_mock(_azure(), MagicMock())
    backend = AzureBlobStorage.from_config(_azure())
    with pytest.raises(StorageUploadError):
        await backend.put("k.txt", b"too large")


async def test_signed_url_rejects_over_ceiling() -> None:
    backend = AzureBlobStorage.from_config(_azure())
    with pytest.raises(StorageSignError):
        await backend.signed_read_url("k.txt", expires_in=timedelta(days=8))


# --- stubbed-client behavior -------------------------------------------------


async def test_put_calls_upload_blob_overwrite_true() -> None:
    config = _azure()
    mock_blob = MagicMock()
    mock_blob.upload_blob = AsyncMock(return_value={"etag": '"abc123"'})
    mock_bsc: Any = MagicMock()
    mock_bsc.get_blob_client.return_value = mock_blob
    _install_mock(config, mock_bsc)

    backend = AzureBlobStorage.from_config(config)
    meta = await backend.put("screenshots/a.png", b"data", content_type="image/png")

    mock_bsc.get_blob_client.assert_called_once_with("c", "screenshots/a.png")
    call = mock_blob.upload_blob.await_args
    assert call is not None
    assert call.kwargs["overwrite"] is True
    assert call.args[0] == b"data"
    assert meta.etag == "abc123"
    assert meta.size == 4
    assert meta.content_type == "image/png"


async def test_head_missing_blob_returns_none() -> None:
    config = _azure()
    mock_blob = MagicMock()
    mock_blob.get_blob_properties = AsyncMock(side_effect=_not_found("BlobNotFound"))
    mock_bsc: Any = MagicMock()
    mock_bsc.get_blob_client.return_value = mock_blob
    _install_mock(config, mock_bsc)

    backend = AzureBlobStorage.from_config(config)
    assert await backend.head("missing.png") is None


async def test_head_missing_container_raises() -> None:
    config = _azure()
    mock_blob = MagicMock()
    mock_blob.get_blob_properties = AsyncMock(side_effect=_not_found("ContainerNotFound"))
    mock_bsc: Any = MagicMock()
    mock_bsc.get_blob_client.return_value = mock_blob
    _install_mock(config, mock_bsc)

    backend = AzureBlobStorage.from_config(config)
    with pytest.raises(StorageError):
        await backend.head("any.png")


# --- the third state: cannot-tell ≠ absent -----------------------------------
#
# `head()` → None and `get()` → StorageNotFoundError are the ABSENCE answer, and the restore path
# acts on them as one: a confirmed-absent bundle provisions a blank template, which finalize then
# snapshots straight over the user's saved app. So absence must be Azure's word, not a bare 404's.


async def test_head_uncoded_not_found_raises_rather_than_claiming_absent() -> None:
    config = _azure()
    mock_blob = MagicMock()
    mock_blob.get_blob_properties = AsyncMock(side_effect=_uncoded_not_found())
    mock_bsc: Any = MagicMock()
    mock_bsc.get_blob_client.return_value = mock_blob
    _install_mock(config, mock_bsc)

    backend = AzureBlobStorage.from_config(config)
    with pytest.raises(StorageError) as excinfo:
        await backend.head("saved-work.bundle")
    # NOT StorageNotFoundError: `_snapshot_exists_or_bust` reads that as "confirmed absent" too.
    assert type(excinfo.value) is StorageError


async def test_get_uncoded_not_found_is_ambiguous_not_missing() -> None:
    config = _azure()
    mock_blob = MagicMock()
    mock_blob.download_blob = AsyncMock(side_effect=_uncoded_not_found())
    mock_bsc: Any = MagicMock()
    mock_bsc.get_blob_client.return_value = mock_blob
    _install_mock(config, mock_bsc)

    backend = AzureBlobStorage.from_config(config)
    with pytest.raises(StorageError) as excinfo:
        await backend.get("saved-work.bundle")
    # StorageNotFoundError is the one error `_restore_or_provision` lets reach provision_new.
    assert not isinstance(excinfo.value, StorageNotFoundError)


async def test_get_named_blob_not_found_still_reports_missing() -> None:
    # The other half of the contract: a NAMED absence must keep answering absent, or the
    # genuinely-new-app path would abort every first build.
    config = _azure()
    mock_blob = MagicMock()
    mock_blob.download_blob = AsyncMock(side_effect=_not_found("BlobNotFound"))
    mock_bsc: Any = MagicMock()
    mock_bsc.get_blob_client.return_value = mock_blob
    _install_mock(config, mock_bsc)

    backend = AzureBlobStorage.from_config(config)
    with pytest.raises(StorageNotFoundError):
        await backend.get("never-built.bundle")


async def test_delete_missing_blob_is_noop() -> None:
    config = _azure()
    mock_blob = MagicMock()
    mock_blob.delete_blob = AsyncMock(side_effect=_not_found("BlobNotFound"))
    mock_bsc: Any = MagicMock()
    mock_bsc.get_blob_client.return_value = mock_blob
    _install_mock(config, mock_bsc)

    backend = AzureBlobStorage.from_config(config)
    await backend.delete("missing.png")  # no raise


async def test_delete_missing_container_raises() -> None:
    config = _azure()
    mock_blob = MagicMock()
    mock_blob.delete_blob = AsyncMock(side_effect=_not_found("ContainerNotFound"))
    mock_bsc: Any = MagicMock()
    mock_bsc.get_blob_client.return_value = mock_blob
    _install_mock(config, mock_bsc)

    backend = AzureBlobStorage.from_config(config)
    with pytest.raises(StorageError):
        await backend.delete("any.png")


# --- SAS branch selection ----------------------------------------------------


async def test_account_key_takes_service_sas_path(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _azure()  # account-key mode
    mock_bsc: Any = MagicMock()
    _install_mock(config, mock_bsc)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        azure_backend,
        "generate_blob_sas",
        lambda *a, **k: (captured.update(k), "sig=SVC")[1],
    )

    backend = AzureBlobStorage.from_config(config)
    url = await backend.signed_read_url("a.png", expires_in=timedelta(hours=1))

    assert captured.get("account_key") is not None
    assert captured.get("user_delegation_key") is None
    mock_bsc.get_user_delegation_key.assert_not_called()
    assert url == "https://acct.blob.core.windows.net/c/a.png?sig=SVC"


async def test_managed_identity_takes_user_delegation_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _azure(account_key=None, use_managed_identity=True)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(azure_backend, "_now", lambda: base)
    udk = MagicMock()
    udk.signed_expiry = (base + timedelta(days=7)).isoformat()
    mock_bsc: Any = MagicMock()
    mock_bsc.get_user_delegation_key = AsyncMock(return_value=udk)
    _install_mock(config, mock_bsc)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        azure_backend,
        "generate_blob_sas",
        lambda *a, **k: (captured.update(k), "sig=MI")[1],
    )

    backend = AzureBlobStorage.from_config(config)
    await backend.signed_read_url("a.png", expires_in=timedelta(hours=1))

    mock_bsc.get_user_delegation_key.assert_awaited_once()
    assert captured.get("user_delegation_key") is udk
    assert captured.get("account_key") is None


async def test_managed_identity_caches_and_remints_delegation_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _azure(account_key=None, use_managed_identity=True)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    clock = {"now": base}
    monkeypatch.setattr(azure_backend, "_now", lambda: clock["now"])
    udk1 = MagicMock()
    udk1.signed_expiry = (base + timedelta(days=7)).isoformat()
    udk2 = MagicMock()
    udk2.signed_expiry = (base + timedelta(days=14)).isoformat()
    mock_bsc: Any = MagicMock()
    mock_bsc.get_user_delegation_key = AsyncMock(side_effect=[udk1, udk2])
    _install_mock(config, mock_bsc)
    monkeypatch.setattr(azure_backend, "generate_blob_sas", lambda *a, **k: "sig=X")

    backend = AzureBlobStorage.from_config(config)
    await backend.signed_read_url("a.png", expires_in=timedelta(hours=1))
    assert mock_bsc.get_user_delegation_key.await_count == 1
    # Same window — cached key reused, no re-mint.
    await backend.signed_read_url("b.png", expires_in=timedelta(hours=1))
    assert mock_bsc.get_user_delegation_key.await_count == 1
    # Advance to within 30m of the key's expiry; a 1h request can't be covered.
    clock["now"] = base + timedelta(days=7) - timedelta(minutes=30)
    await backend.signed_read_url("c.png", expires_in=timedelta(hours=1))
    assert mock_bsc.get_user_delegation_key.await_count == 2
