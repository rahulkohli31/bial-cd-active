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
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Final, NamedTuple

from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from azure.storage.blob import AccessPolicy, ContainerSasPermissions, generate_container_sas

from src.services.storage import azure_backend
from src.services.storage.config import AzureStorageConfig
from src.services.storage.constants import DEPLOY_SAS_TTL, MAX_SIGNED_URL_TTL, validate_sas_ttl
from src.services.storage.errors import StorageSignError
from src.services.storage.keys import container_name

# The container SAS is minted at the practical maximum Azure allows a user-delegation SAS — 7 days
# (KTD-3). There is no periodic reaper, so a long-lived session can outlive its one-time SAS; when
# it does, file ops fail until the next build re-provisions (recoverable). Refresh-on-long-session
# is a deferred hardening; the container-scoped, time-bound SAS is still the security boundary.
APP_CONTAINER_SAS_TTL: Final = MAX_SIGNED_URL_TTL

# Real Azure reserves a just-deleted container's name for ~30s and returns a 409
# `ContainerBeingDeleted` on a recreate inside that window. The SDK raises that 409 as a
# `ResourceExistsError` — the SAME type as a genuine ContainerAlreadyExists — so `ensure_container`
# tells the two apart by error code: the transient name-lock gets a bounded retry (a re-provision
# shortly after a project delete would otherwise fail the fail-first birth path), a real
# already-exists is idempotent success. Azurite does not model the name-lock, so the retry path is
# exercised only against real Azure (D7); the unit test drives it with a ResourceExistsError
# carrying this error code.
_CONTAINER_BEING_DELETED: Final = "ContainerBeingDeleted"
_RECREATE_MAX_ATTEMPTS: Final = 6
_RECREATE_BACKOFF_SECONDS: Final = 5.0

# The id of the per-app stored access policy the DEPLOY credential is minted against (U2/R2) —
# `deploy-{random}`, freshly drawn on EVERY mint.
#
# Azure can revoke an issued service SAS ONLY through a stored access policy the SAS referenced
# AT MINT TIME (`si=` in the query string) — an ad-hoc SAS is irrevocable short of rotating the
# whole account key, which would kill every other app's credential too. So the deploy SAS carries
# nothing but this policy id, and the policy carries the permissions + expiry: deleting or
# back-dating it is the per-app kill switch the go-live runbook documents. One app = one
# container, so the 5-policies-per-container limit is a non-issue.
#
# The id is RANDOM PER MINT because a service SAS is a deterministic function of (account,
# container, account_key, policy_id): under a CONSTANT id every mint returns a byte-identical
# token, which quietly made the runbook's revoke-then-re-mint a no-op — it deleted the policy
# (killing the leaked credential), re-minted THE SAME STRING, and handed the leak straight back.
# A fresh id makes each mint a genuinely different token, and `set_container_access_policy`
# REPLACES the container's whole policy set, so a mint also revokes its predecessor: one live
# credential per app, and re-mint is real rotation rather than resurrection.
DEPLOY_POLICY_PREFIX: Final = "deploy-"


def _mint_deploy_policy_id() -> str:
    """A fresh id for one deploy credential's stored access policy. Secure-random (ADR-0006), not
    a counter or a timestamp: this is the handle a leaked SAS is revoked BY, so two mints must
    never be able to land on the same id and re-issue the same token."""
    return f"{DEPLOY_POLICY_PREFIX}{secrets.token_hex(8)}"


class DeployCredential(NamedTuple):
    """A minted long-lived deploy credential: the SAS query string (`sv=…&si=…&sig=…`, no leading
    `?`) and the moment it dies. `expires_at` is reported by the MINTER, not parsed back out of
    the SAS — a policy-referencing SAS carries no `se=` of its own (the expiry lives in the stored
    access policy), which is exactly what keeps it revocable."""

    sas: str
    expires_at: datetime


def _rwld() -> ContainerSasPermissions:
    """read + write + list + delete on one app container — the destructive surface, granted
    identically to the session SAS and the deploy SAS so the two can never drift apart."""
    return ContainerSasPermissions(read=True, write=True, list=True, delete=True)


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
        """Create the app's container if it does not exist (idempotent). Treats a genuine
        `ContainerAlreadyExists` as success and tolerates real-Azure's post-delete
        `ContainerBeingDeleted` 409 with a bounded retry — the SDK raises BOTH as
        `ResourceExistsError`, so they are told apart by error code (see module note)."""
        state = await azure_backend.get_client_state(self._config)
        name = container_name(app_id)
        for attempt in range(_RECREATE_MAX_ATTEMPTS):
            try:
                await state.service_client.create_container(name)
                return
            except ResourceExistsError as exc:
                # The SDK maps BOTH ContainerAlreadyExists AND the post-delete name-lock (409
                # ContainerBeingDeleted) to ResourceExistsError, so distinguish by code: a real
                # "already exists" is idempotent success; the transient name-lock gets a bounded
                # retry, then a sanitized raise if it never clears within the window.
                if azure_backend._error_code(exc) != _CONTAINER_BEING_DELETED:
                    return  # genuine ContainerAlreadyExists — idempotent
                if attempt < _RECREATE_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_RECREATE_BACKOFF_SECONDS)
                    continue
                azure_backend.raise_azure(
                    exc, op="ensure_container", key=name, provider=self.provider
                )
            except (HttpResponseError, ServiceRequestError) as exc:
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
        validate_sas_ttl(ttl, provider=self.provider, key=name)
        now = azure_backend._now()
        permission = _rwld()
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

    async def mint_deploy_container_sas(
        self, app_id: uuid.UUID, *, ttl: timedelta = DEPLOY_SAS_TTL
    ) -> DeployCredential:
        """Mint the LONG-LIVED, container-scoped deploy credential for an app that has gone live
        (U2/R2) — the same rwld grant as the session SAS, but at `DEPLOY_SAS_TTL` (365d) instead of
        the 7-day session ceiling, so a deployed container reaches its own Blob storage DIRECTLY,
        with no platform proxy in the data path.

        Deliberately does NOT call `validate_sas_ttl`: that ceiling mirrors Azure's hard 7-day cap
        on *user-delegation* SAS and stays authoritative for every session mint. This is an
        **account-key service SAS**, which Azure imposes no expiry cap on — so the ceiling is
        replaced by two other guards: the account-key-only branch below (a managed-identity config
        physically cannot mint one and says so), and a unit test pinning `DEPLOY_SAS_TTL` under
        400 days.

        Revocable BY CONSTRUCTION: the SAS references a per-app stored access policy rather than
        inlining its own expiry/permissions, which is the ONLY way Azure will honour a later
        revocation of an already-issued service SAS.

        ROTATABLE by construction too, which the constant policy id it used to sign against made
        impossible: each mint draws a fresh id (see `DEPLOY_POLICY_PREFIX`) and
        `set_container_access_policy` replaces the container's whole policy set, so a mint issues a
        DIFFERENT token and revokes the previous one in the same act — one live credential per app,
        by design.
        """
        name = container_name(app_id)
        # Checked BEFORE provisioning anything: a managed-identity config can only sign with a
        # delegation key, which Azure caps at 7 days — there is no long-lived SAS to mint here,
        # and failing now avoids creating a container for a doomed mint.
        account_key = azure_backend.account_signing_key(self._config)
        if account_key is None:
            raise StorageSignError(
                "a long-lived deploy SAS requires an account key: a managed-identity "
                "(user-delegation) SAS cannot outlive Azure's 7-day cap",
                provider=self.provider,
                key=name,
            )
        # Sign only for a container that EXISTS. `generate_container_sas` is pure local crypto —
        # it would happily sign for a never-provisioned container and hand back a credential that
        # 404s at runtime. `ensure_container` is idempotent, so this is a no-op for the normal
        # already-provisioned app; it also gives the policy write below something to write to.
        await self.ensure_container(app_id)
        now = azure_backend._now()
        expires_at = now + ttl
        # Drawn BEFORE the policy write and threaded into the signing below — the policy and the
        # SAS must name the same id or the credential references a policy that does not exist.
        policy_id = _mint_deploy_policy_id()
        await self._upsert_deploy_policy(name, policy_id, now=now, expires_at=expires_at)
        # No `permission=`/`expiry=` here ON PURPOSE: both live in the stored access policy, and a
        # SAS that inlined them would be irrevocable (Azure honours a policy revocation only for
        # the fields the SAS delegated to it).
        sas = generate_container_sas(
            account_name=azure_backend._account_name(self._config),
            container_name=name,
            account_key=account_key,
            policy_id=policy_id,
        )
        return DeployCredential(sas=sas, expires_at=expires_at)

    async def _upsert_deploy_policy(
        self, name: str, policy_id: str, *, now: datetime, expires_at: datetime
    ) -> None:
        """Write `policy_id` as the app's ONLY deploy stored access policy.

        The single-key dict is doing real work: `set_container_access_policy` REPLACES the set, so
        writing the new id is also what deletes the previous mint's — which is what makes a re-mint
        supersede rather than accumulate (Azure caps a container at 5 policies).

        `public_access` is omitted, which Azure reads as PRIVATE — the fail-closed value, and the
        one our containers are created with; this call must never be the thing that makes a
        container public."""
        state = await azure_backend.get_client_state(self._config)
        container_client = state.service_client.get_container_client(name)
        policy = AccessPolicy(
            permission=_rwld(), expiry=expires_at, start=now - azure_backend._CLOCK_SKEW
        )
        try:
            await container_client.set_container_access_policy({policy_id: policy})
        except (HttpResponseError, ServiceRequestError) as exc:
            azure_backend.raise_azure(
                exc, op="set_deploy_policy", key=name, provider=self.provider
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
