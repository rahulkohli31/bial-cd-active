"""AppContainerStore — account-level per-app Blob container management (C9 §6).

The `ObjectStorage` ABC is deliberately **single-container, blob-level**. Per-app storage needs
the *account-level* operations one tier up — create/delete a whole container, mint a
**container-scoped** SAS — so this is a separate store, not an `ObjectStorage` subclass. It reuses
the SAME account config (`settings.object_store`, D2) and the SAME cached `BlobServiceClient`
(KTD-1): it owns **no** client of its own, resolves the shared client **per-op** from
`azure_backend`'s module cache (never captured at `__init__`, so it can't go stale), and does
**not** `aclose` that shared client (the backend owns the client lifecycle).

Isolation model (the ADR-0019 analog for Blob, done now): shared account → one `app-{app_id}`
container per app → a container-scoped SAS (rwld) per app. A leaked SAS reaches only its one
container — never the account, a sibling container, or the platform store (`snapshots/` · `att/`).
See C9 §6.4 for the exposure bound.

Written LF-only / pure-Python to survive the Windows-VM image build, like the rest of the layer.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Final

from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from azure.storage.blob import ContainerSasPermissions, generate_container_sas

from src.services.storage import azure_backend
from src.services.storage.config import AzureStorageConfig
from src.services.storage.constants import MAX_SIGNED_URL_TTL
from src.services.storage.errors import StorageSignError
from src.services.storage.keys import container_name

# The container SAS is minted at the practical maximum Azure allows a user-delegation SAS — 7 days
# (KTD-3). There is no periodic reaper, so a long-lived session can outlive its one-time SAS; when
# it does, file ops fail until the next build re-provisions (recoverable). Refresh-on-long-session
# is a deferred hardening; the container-scoped, time-bound SAS is still the security boundary.
APP_CONTAINER_SAS_TTL: Final = MAX_SIGNED_URL_TTL

# Real Azure reserves a just-deleted container's name for ~30s and returns a 409
# `ContainerBeingDeleted` (an HttpResponseError, NOT the ResourceExistsError we swallow) on a
# recreate inside that window. A re-provision shortly after a project delete would otherwise fail
# the fail-first birth path, so `ensure_container` tolerates it with a bounded retry. Azurite does
# not model the name-lock, so this path is exercised only against real Azure (D7), not locally.
_CONTAINER_BEING_DELETED: Final = "ContainerBeingDeleted"
_RECREATE_MAX_ATTEMPTS: Final = 6
_RECREATE_BACKOFF_SECONDS: Final = 5.0


class AppContainerStore:
    """Per-app container lifecycle + container-scoped SAS minting against the shared account.

    Holds only the config; the client is resolved per-op from `azure_backend`'s shared cache."""

    def __init__(self, config: AzureStorageConfig) -> None:
        self._config = config

    @property
    def provider(self) -> str:
        return self._config.provider

    def container_url(self, app_id: uuid.UUID, *, base_url: str | None = None) -> str:
        """The app's container URL, `{base}/app-{app_id}`. `base_url` defaults to the signing
        account's `account_url`; callers that must hand a *sandbox-reachable* host to the running
        app pass the sandbox-facing Blob base instead (KTD-2 — a container SAS is signed by account
        *name*, not host, so the same SAS is valid against either host)."""
        base = (base_url if base_url is not None else self._config.account_url).rstrip("/")
        return f"{base}/{container_name(app_id)}"

    async def ensure_container(self, app_id: uuid.UUID) -> None:
        """Create the app's container if it does not exist (idempotent). Swallows
        `ResourceExistsError` (already there) and tolerates real-Azure's post-delete
        `ContainerBeingDeleted` 409 with a bounded retry (see module note)."""
        state = await azure_backend.get_client_state(self._config)
        name = container_name(app_id)
        for attempt in range(_RECREATE_MAX_ATTEMPTS):
            try:
                await state.service_client.create_container(name)
                return
            except ResourceExistsError:
                return  # already exists — idempotent
            except HttpResponseError as exc:
                being_deleted = azure_backend._error_code(exc) == _CONTAINER_BEING_DELETED
                if being_deleted and attempt < _RECREATE_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_RECREATE_BACKOFF_SECONDS)
                    continue
                azure_backend.raise_azure(
                    exc, op="ensure_container", key=name, provider=self.provider
                )
            except ServiceRequestError as exc:
                azure_backend.raise_azure(
                    exc, op="ensure_container", key=name, provider=self.provider
                )

    async def mint_container_sas(
        self, app_id: uuid.UUID, *, ttl: timedelta = APP_CONTAINER_SAS_TTL
    ) -> str:
        """Mint a **container-scoped** SAS query string (`sv=…&sig=…`, no leading `?`) granting
        rwld (read+write+list+delete) on this one app's container — never an account SAS. The TTL
        ceiling is enforced fail-closed here (mirrors the ABC's `signed_read_url` guard) before any
        signing runs. Two branches mirror `_signed_read_url_impl`: account-key (Azurite/local — the
        verified path) and user-delegation (real-Azure MI — deferred verification, D7)."""
        name = container_name(app_id)
        if ttl <= timedelta(0):
            raise StorageSignError("ttl must be positive", provider=self.provider, key=name)
        if ttl > MAX_SIGNED_URL_TTL:
            raise StorageSignError(
                f"ttl exceeds the {MAX_SIGNED_URL_TTL} SAS ceiling",
                provider=self.provider,
                key=name,
            )
        now = azure_backend._now()
        permission = ContainerSasPermissions(read=True, write=True, list=True, delete=True)
        if self._config.use_managed_identity:
            state = await azure_backend.get_client_state(self._config)
            udk, key_expiry = await azure_backend.get_delegation_key(
                state, ttl, now, provider=self.provider
            )
            return generate_container_sas(
                account_name=azure_backend._account_name(self._config),
                container_name=name,
                user_delegation_key=udk,
                permission=permission,
                expiry=azure_backend._sas_expiry(now, ttl, key_expiry),
                start=now - azure_backend._CLOCK_SKEW,
            )
        account_key = azure_backend.account_signing_key(self._config)
        if account_key is None:
            raise StorageSignError(
                "no account key available for container-SAS signing",
                provider=self.provider,
                key=name,
            )
        return generate_container_sas(
            account_name=azure_backend._account_name(self._config),
            container_name=name,
            account_key=account_key,
            permission=permission,
            expiry=now + ttl,
            start=now - azure_backend._CLOCK_SKEW,
        )

    async def delete_container(self, app_id: uuid.UUID) -> None:
        """Delete the app's container (idempotent — a missing container is a no-op)."""
        state = await azure_backend.get_client_state(self._config)
        name = container_name(app_id)
        try:
            await state.service_client.delete_container(name)
        except ResourceNotFoundError:
            return  # already gone — idempotent
        except (HttpResponseError, ServiceRequestError) as exc:
            azure_backend.raise_azure(exc, op="delete_container", key=name, provider=self.provider)
