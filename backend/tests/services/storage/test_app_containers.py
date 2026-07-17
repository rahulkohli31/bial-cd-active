"""AppContainerStore — per-app Blob container management (C9 §6).

Network-free unit tests inject a mock BlobServiceClient into the module client cache so
ensure/mint/delete behavior and the account-key-vs-user-delegation SAS branch are observable
without Azure; the account-scoped-SAS round-trip, cross-container isolation (the named acceptance
criterion), and idempotency are proven against a live Azurite backend in the opt-in
`-m integration` lane. No secret value ever appears in a raised message (asserted).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from pydantic import SecretStr

import src.services.storage.accessor as accessor
from src.services.storage import (
    APP_CONTAINER_SAS_TTL,
    AppContainerStore,
    app_containers,
    azure_backend,
    get_app_container_store,
)
from src.services.storage.app_containers import DEPLOY_POLICY_ID
from src.services.storage.config import AzureStorageConfig
from src.services.storage.constants import DEPLOY_SAS_TTL, MAX_SIGNED_URL_TTL
from src.services.storage.errors import StorageError, StorageSignError

_APP = uuid.UUID("019f1c00-0000-7000-8000-0000000000bb")


def _azure(**overrides: object) -> AzureStorageConfig:
    base: dict[str, object] = {
        "account_url": "https://acct.blob.core.windows.net",
        "container": "c",
        "account_key": SecretStr("a2V5"),
    }
    return AzureStorageConfig.model_validate({**base, **overrides})


def _http_error(message: str, *, code: str | None = None) -> HttpResponseError:
    exc = HttpResponseError(message=message)
    if code is not None:
        # error_code is set dynamically by azure-core from the response; setattr so the
        # (incomplete) stub doesn't flag the assignment (mirrors test_azure_backend).
        setattr(exc, "error_code", code)
    return exc


def _exists_error(*, code: str) -> ResourceExistsError:
    # Real Azure delivers BOTH a genuine ContainerAlreadyExists AND the post-delete name-lock
    # (ContainerBeingDeleted) as a ResourceExistsError carrying that error_code — see the SDK's
    # _shared/response_handlers.py. The retry path must be driven with this real type, not a bare
    # HttpResponseError the SDK never actually raises for these 409s.
    exc = ResourceExistsError(message=code)
    setattr(exc, "error_code", code)
    return exc


def _install_mock(config: AzureStorageConfig, service_client: Any) -> None:
    fp = azure_backend._fingerprint(config)
    azure_backend._client_cache[fp] = azure_backend._AzureClient(service_client, credential=None)


@pytest.fixture(autouse=True)
def _reset_storage_singletons() -> Any:
    # Sync isolation: clear the process-global client cache + the accessor singletons directly
    # (mock clients hold no real resources, so we must NOT await a real aclose over them).
    azure_backend._client_cache.clear()
    accessor._backend_singleton = None
    accessor._app_container_store_singleton = None
    yield
    azure_backend._client_cache.clear()
    accessor._backend_singleton = None
    accessor._app_container_store_singleton = None


# --- container_name / URL / TTL (pure) ---------------------------------------


def test_app_container_sas_ttl_is_the_seven_day_max() -> None:
    assert APP_CONTAINER_SAS_TTL == MAX_SIGNED_URL_TTL == timedelta(days=7)


def test_container_url_defaults_to_account_url() -> None:
    store = AppContainerStore(_azure())
    assert store.container_url(_APP) == f"https://acct.blob.core.windows.net/app-{_APP}"


def test_container_url_uses_sandbox_facing_base_and_strips_trailing_slash() -> None:
    # KTD-2: the injected URL must be a host the sandbox reaches — callers pass the sandbox-facing
    # Blob base (e.g. the docker-network Azurite address), and the trailing slash is normalized.
    store = AppContainerStore(_azure())
    url = store.container_url(_APP, base_url="http://azurite:10000/devstoreaccount1/")
    assert url == f"http://azurite:10000/devstoreaccount1/app-{_APP}"


# --- ensure_container --------------------------------------------------------


async def test_ensure_container_creates_by_app_name() -> None:
    config = _azure()
    mock_bsc: Any = MagicMock()
    mock_bsc.create_container = AsyncMock()
    _install_mock(config, mock_bsc)

    await AppContainerStore(config).ensure_container(_APP)
    mock_bsc.create_container.assert_awaited_once_with(f"app-{_APP}")


async def test_ensure_container_swallows_already_exists() -> None:
    config = _azure()
    mock_bsc: Any = MagicMock()
    mock_bsc.create_container = AsyncMock(side_effect=ResourceExistsError("exists"))
    _install_mock(config, mock_bsc)

    await AppContainerStore(config).ensure_container(_APP)  # no raise — idempotent
    assert mock_bsc.create_container.await_count == 1  # exists is NOT retried


async def test_ensure_container_retries_container_being_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real-Azure post-delete name-lock (409 ContainerBeingDeleted): tolerate with a bounded retry.
    # The SDK delivers this 409 as a ResourceExistsError carrying error_code, NOT a bare
    # HttpResponseError — drive the retry path with the type real Azure actually raises.
    config = _azure()
    being_deleted = _exists_error(code="ContainerBeingDeleted")
    mock_bsc: Any = MagicMock()
    mock_bsc.create_container = AsyncMock(side_effect=[being_deleted, being_deleted, None])
    _install_mock(config, mock_bsc)
    monkeypatch.setattr(app_containers, "_RECREATE_BACKOFF_SECONDS", 0.0)

    await AppContainerStore(config).ensure_container(_APP)
    assert mock_bsc.create_container.await_count == 3  # two 409s tolerated, third succeeds


async def test_ensure_container_raises_after_persistent_being_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _azure()
    mock_bsc: Any = MagicMock()
    mock_bsc.create_container = AsyncMock(side_effect=_exists_error(code="ContainerBeingDeleted"))
    _install_mock(config, mock_bsc)
    monkeypatch.setattr(app_containers, "_RECREATE_BACKOFF_SECONDS", 0.0)

    with pytest.raises(StorageError):
        await AppContainerStore(config).ensure_container(_APP)
    assert mock_bsc.create_container.await_count == app_containers._RECREATE_MAX_ATTEMPTS


async def test_ensure_container_wraps_and_sanitizes_other_error() -> None:
    # A non-BeingDeleted Azure error is wrapped to StorageError with NO credential substring and
    # NO retry — the error-sanitization contract (mirrors azure_backend._raise_azure).
    config = _azure()
    secret = "SUPERSECRETSIGVALUE"
    mock_bsc: Any = MagicMock()
    mock_bsc.create_container = AsyncMock(side_effect=_http_error(f"boom sig={secret}"))
    _install_mock(config, mock_bsc)

    with pytest.raises(StorageError) as ei:
        await AppContainerStore(config).ensure_container(_APP)
    assert secret not in str(ei.value)
    assert mock_bsc.create_container.await_count == 1


async def test_ensure_container_wraps_service_request_error() -> None:
    # A transport-level ServiceRequestError (request sent, response lost) is NOT a
    # ContainerBeingDeleted, so it falls straight through to raise_azure with NO retry.
    config = _azure()
    mock_bsc: Any = MagicMock()
    mock_bsc.create_container = AsyncMock(side_effect=ServiceRequestError("transport dropped"))
    _install_mock(config, mock_bsc)

    with pytest.raises(StorageError):
        await AppContainerStore(config).ensure_container(_APP)
    assert mock_bsc.create_container.await_count == 1  # transport error is not retried


# --- mint_container_sas ------------------------------------------------------


async def test_mint_container_sas_account_key_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _azure()  # account-key mode — the local/verified path
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        app_containers,
        "generate_container_sas",
        lambda *a, **k: (captured.update(k), "sv=X&sig=SVC")[1],
    )

    sas = await AppContainerStore(config).mint_container_sas(_APP)

    assert sas == "sv=X&sig=SVC"
    assert captured["account_key"] == "a2V5"
    assert captured.get("user_delegation_key") is None
    assert captured["container_name"] == f"app-{_APP}"
    # rwld — read + write + list + delete must all be granted (the destructive surface).
    perm = captured["permission"]
    assert perm.read and perm.write and perm.list and perm.delete


async def test_mint_container_sas_managed_identity_branch(
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
        app_containers,
        "generate_container_sas",
        lambda *a, **k: (captured.update(k), "sv=X&sig=MI")[1],
    )

    await AppContainerStore(config).mint_container_sas(_APP)

    mock_bsc.get_user_delegation_key.assert_awaited_once()
    assert captured["user_delegation_key"] is udk
    assert captured.get("account_key") is None


async def test_mint_container_sas_rejects_over_ceiling() -> None:
    with pytest.raises(StorageSignError):
        await AppContainerStore(_azure()).mint_container_sas(_APP, ttl=timedelta(days=8))


async def test_mint_container_sas_rejects_nonpositive_ttl() -> None:
    with pytest.raises(StorageSignError):
        await AppContainerStore(_azure()).mint_container_sas(_APP, ttl=timedelta(0))


# --- mint_deploy_container_sas (U2/R2) ---------------------------------------


def _deploy_mock(config: AzureStorageConfig) -> tuple[Any, Any, list[str]]:
    """A BlobServiceClient double for the deploy-mint path + the container client it hands out,
    plus a shared call log so a test can assert the ORDER of provision-then-sign."""
    calls: list[str] = []
    container_client: Any = MagicMock()
    container_client.set_container_access_policy = AsyncMock(
        side_effect=lambda *a, **k: calls.append("set_policy")
    )
    mock_bsc: Any = MagicMock()
    mock_bsc.create_container = AsyncMock(side_effect=lambda *a, **k: calls.append("create"))
    mock_bsc.get_container_client = MagicMock(return_value=container_client)
    _install_mock(config, mock_bsc)
    return mock_bsc, container_client, calls


def test_deploy_sas_ttl_is_long_lived_but_guarded() -> None:
    # THE GUARD (a comment is not one): a future edit that widens the deploy credential's life
    # past 400 days fails here. The deploy TTL is intentionally far past the session ceiling —
    # that divergence is the whole point of U2 — but it is not unbounded.
    assert DEPLOY_SAS_TTL == timedelta(days=365)
    assert DEPLOY_SAS_TTL <= timedelta(days=400)
    assert DEPLOY_SAS_TTL > MAX_SIGNED_URL_TTL


async def test_mint_deploy_container_sas_signs_against_the_stored_access_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _azure()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(azure_backend, "_now", lambda: base)
    _, container_client, _ = _deploy_mock(config)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        app_containers,
        "generate_container_sas",
        lambda *a, **k: (captured.update(k), "sv=X&si=deploy&sig=DEPLOY")[1],
    )

    credential = await AppContainerStore(config).mint_deploy_container_sas(_APP)

    assert credential.sas == "sv=X&si=deploy&sig=DEPLOY"
    assert credential.expires_at == base + timedelta(days=365)
    # Container-scoped and account-key-signed — never an account SAS, never a delegation key
    # (which Azure would cap at 7 days).
    assert captured["container_name"] == f"app-{_APP}"
    assert captured["account_key"] == "a2V5"
    assert captured.get("user_delegation_key") is None
    # The revocability contract: the SAS delegates permission+expiry to the policy and inlines
    # NEITHER — an inlined expiry would make the runbook's revocation lever a lie.
    assert captured["policy_id"] == DEPLOY_POLICY_ID
    assert captured.get("permission") is None
    assert captured.get("expiry") is None
    # ...and the policy is what actually carries them: rwld, dying with the credential.
    (identifiers,), _ = container_client.set_container_access_policy.await_args
    policy = identifiers[DEPLOY_POLICY_ID]
    assert policy.expiry == base + timedelta(days=365)
    assert policy.permission.read and policy.permission.write
    assert policy.permission.list and policy.permission.delete


async def test_mint_deploy_container_sas_provisions_before_signing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `generate_container_sas` is pure local crypto: it would happily sign for a container that
    # was never created, yielding a credential that 404s at runtime. Provision first, always.
    config = _azure()
    _, _, calls = _deploy_mock(config)

    def _sign(*a: Any, **k: Any) -> str:
        calls.append("sign")
        return "sv=X&sig=D"

    monkeypatch.setattr(app_containers, "generate_container_sas", _sign)

    await AppContainerStore(config).mint_deploy_container_sas(_APP)

    assert calls == ["create", "set_policy", "sign"]


async def test_mint_deploy_container_sas_tolerates_existing_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The NORMAL case — the app was provisioned at build time, so the ensure is a no-op. The
    # already-exists 409 must not abort the mint (`ensure_container`'s idempotency, reused).
    config = _azure()
    _, _, calls = _deploy_mock(config)
    mock_bsc: Any = azure_backend._client_cache[azure_backend._fingerprint(config)].service_client
    mock_bsc.create_container = AsyncMock(side_effect=ResourceExistsError("exists"))
    monkeypatch.setattr(app_containers, "generate_container_sas", lambda *a, **k: "sv=X&sig=D")

    credential = await AppContainerStore(config).mint_deploy_container_sas(_APP)
    assert credential.sas == "sv=X&sig=D"
    assert calls == ["set_policy"]  # signed anyway; the policy still landed


async def test_mint_deploy_container_sas_refuses_managed_identity() -> None:
    # A user-delegation SAS is hard-capped at 7 days by Azure, so a managed-identity config
    # simply cannot mint this credential — fail typed and BEFORE provisioning anything.
    config = _azure(account_key=None, use_managed_identity=True)
    mock_bsc, _, calls = _deploy_mock(config)

    with pytest.raises(StorageSignError) as ei:
        await AppContainerStore(config).mint_deploy_container_sas(_APP)
    assert "account key" in str(ei.value)
    assert calls == []
    mock_bsc.create_container.assert_not_awaited()


async def test_mint_deploy_container_sas_wraps_policy_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failed policy write must NOT fall through to signing: without the policy, the `si=` SAS
    # is dead on arrival (403) — and the error is sanitized, never echoing a credential.
    config = _azure()
    _, container_client, _ = _deploy_mock(config)
    secret = "SUPERSECRETSIGVALUE"
    container_client.set_container_access_policy = AsyncMock(
        side_effect=_http_error(f"boom sig={secret}")
    )
    monkeypatch.setattr(
        app_containers,
        "generate_container_sas",
        lambda *a, **k: pytest.fail("must not sign after a failed policy write"),
    )

    with pytest.raises(StorageError) as ei:
        await AppContainerStore(config).mint_deploy_container_sas(_APP)
    assert secret not in str(ei.value)


async def test_session_mint_still_rejects_the_deploy_ttl() -> None:
    # REGRESSION: `validate_sas_ttl` remains authoritative for SESSION SAS. U2 opened a 365-day
    # door for the deploy credential ONLY — the session path must never be able to walk through it.
    with pytest.raises(StorageSignError):
        await AppContainerStore(_azure()).mint_container_sas(_APP, ttl=DEPLOY_SAS_TTL)


# --- delete_container --------------------------------------------------------


async def test_delete_container_deletes_by_app_name() -> None:
    config = _azure()
    mock_bsc: Any = MagicMock()
    mock_bsc.delete_container = AsyncMock()
    _install_mock(config, mock_bsc)

    await AppContainerStore(config).delete_container(_APP)
    mock_bsc.delete_container.assert_awaited_once_with(f"app-{_APP}")


async def test_delete_container_missing_is_noop() -> None:
    config = _azure()
    mock_bsc: Any = MagicMock()
    mock_bsc.delete_container = AsyncMock(side_effect=ResourceNotFoundError("gone"))
    _install_mock(config, mock_bsc)

    await AppContainerStore(config).delete_container(_APP)  # no raise — idempotent


async def test_delete_container_wraps_http_error() -> None:
    config = _azure()
    mock_bsc: Any = MagicMock()
    mock_bsc.delete_container = AsyncMock(side_effect=_http_error("boom"))
    _install_mock(config, mock_bsc)

    with pytest.raises(StorageError):
        await AppContainerStore(config).delete_container(_APP)


async def test_delete_container_wraps_service_request_error() -> None:
    # The transport-error escape hatch on delete: wrapped into StorageError via raise_azure.
    config = _azure()
    mock_bsc: Any = MagicMock()
    mock_bsc.delete_container = AsyncMock(side_effect=ServiceRequestError("transport dropped"))
    _install_mock(config, mock_bsc)

    with pytest.raises(StorageError):
        await AppContainerStore(config).delete_container(_APP)


# --- get_app_container_store (accessor divergence + reset hook) ---------------


async def test_get_app_container_store_none_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config import settings

    monkeypatch.setattr(settings, "object_store", None)
    # Diverges from get_storage() (which raises) — returns None so callers branch on "feature off".
    assert get_app_container_store() is None


async def test_get_app_container_store_returns_store_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config import settings

    monkeypatch.setattr(settings, "object_store", _azure())
    assert isinstance(get_app_container_store(), AppContainerStore)


async def test_get_app_container_store_reset_hook_drops_stale_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config import settings
    from src.services.storage import reset_storage_for_tests

    monkeypatch.setattr(settings, "object_store", _azure())
    first = get_app_container_store()
    await reset_storage_for_tests()  # the reset hook must drop the singleton
    second = get_app_container_store()
    assert first is not second  # a config-swapping suite never reuses a stale store


# --- Azurite integration (opt-in: `-m integration`) --------------------------


@pytest.mark.integration
async def test_ensure_and_delete_container_are_idempotent(
    app_container_store: AppContainerStore,
) -> None:
    app_id = uuid.uuid4()
    try:
        await app_container_store.ensure_container(app_id)
        await app_container_store.ensure_container(app_id)  # second create → no-op
    finally:
        await app_container_store.delete_container(app_id)
        await app_container_store.delete_container(app_id)  # second delete → no-op


@pytest.mark.integration
async def test_container_sas_round_trips_put_list_get_delete(
    app_container_store: AppContainerStore,
) -> None:
    app_id = uuid.uuid4()
    await app_container_store.ensure_container(app_id)
    try:
        sas = await app_container_store.mint_container_sas(app_id)
        assert "sig=" in sas
        base = app_container_store.container_url(
            app_id
        )  # Azurite host (127.0.0.1) — test-reachable
        blob_url = f"{base}/hello.txt?{sas}"
        async with httpx.AsyncClient() as client:
            put = await client.put(
                blob_url, content=b"hello blob", headers={"x-ms-blob-type": "BlockBlob"}
            )
            assert put.status_code in (200, 201)

            got = await client.get(blob_url)
            assert got.status_code == 200
            assert got.content == b"hello blob"

            listing = await client.get(f"{base}?restype=container&comp=list&{sas}")
            assert listing.status_code == 200
            assert "hello.txt" in listing.text  # the SAS grants list on the container

            deleted = await client.delete(blob_url)
            assert deleted.status_code in (200, 202)  # the SAS grants delete
            assert (await client.get(blob_url)).status_code == 404
    finally:
        await app_container_store.delete_container(app_id)


@pytest.mark.integration
async def test_deploy_sas_round_trips_and_is_revocable_by_policy(
    app_container_store: AppContainerStore,
) -> None:
    # The deploy credential against a REAL Blob service: it provisions the container itself,
    # round-trips rwld, and — the claim the runbook makes — DIES when the app's stored access
    # policy is deleted, while the SAS string itself is untouched. That last assertion is the
    # whole reason the mint references a policy instead of inlining an expiry.
    app_id = uuid.uuid4()
    try:
        credential = await app_container_store.mint_deploy_container_sas(app_id)
        assert f"si={DEPLOY_POLICY_ID}" in credential.sas  # the revocation handle rides along
        assert credential.expires_at > datetime.now(UTC) + timedelta(days=364)
        base = app_container_store.container_url(app_id)
        blob_url = f"{base}/deployed.txt?{credential.sas}"
        async with httpx.AsyncClient() as client:
            put = await client.put(
                blob_url, content=b"live app data", headers={"x-ms-blob-type": "BlockBlob"}
            )
            assert put.status_code in (200, 201)
            got = await client.get(blob_url)
            assert got.status_code == 200
            assert got.content == b"live app data"

            # The incident-response lever: drop the policy → the issued SAS is refused.
            state = await azure_backend.get_client_state(app_container_store._config)
            await state.service_client.get_container_client(
                f"app-{app_id}"
            ).set_container_access_policy({})
            assert (await client.get(blob_url)).status_code == 403
    finally:
        await app_container_store.delete_container(app_id)


@pytest.mark.integration
async def test_cross_container_sas_isolation(app_container_store: AppContainerStore) -> None:
    # The named acceptance criterion: app A's SAS must be refused (403) on read, write, AND delete
    # against app B's container — proving container-scoping is the isolation boundary (C9 §6.4).
    app_a, app_b = uuid.uuid4(), uuid.uuid4()
    await app_container_store.ensure_container(app_a)
    await app_container_store.ensure_container(app_b)
    try:
        sas_a = await app_container_store.mint_container_sas(app_a)
        base_b = app_container_store.container_url(app_b)
        async with httpx.AsyncClient() as client:
            put = await client.put(
                f"{base_b}/x.txt?{sas_a}", content=b"x", headers={"x-ms-blob-type": "BlockBlob"}
            )
            assert put.status_code == 403
            assert (await client.get(f"{base_b}/x.txt?{sas_a}")).status_code == 403
            assert (await client.delete(f"{base_b}/x.txt?{sas_a}")).status_code == 403
    finally:
        await app_container_store.delete_container(app_a)
        await app_container_store.delete_container(app_b)
