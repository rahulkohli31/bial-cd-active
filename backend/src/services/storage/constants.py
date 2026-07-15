"""Object-storage safety bounds — fixed in every deploy, so they are code
constants, not env config (12-factor: config is what varies between deploys).
Changed only by a code edit + review, never by ops at runtime.

(badger's per-segment / total object-key byte ceilings are dropped here: they
guarded the multi-tenant `scoped_key`, whose forgeable string axes this
single-tenant port replaces with UUID-typed key builders — a canonical UUID
cannot carry `/`, `..`, or control chars, so the length/traversal guards have
nothing left to guard. See `keys.py`.)
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from src.services.storage.errors import StorageSignError

# Hard ceiling on signed read-URL lifetime. The ABC rejects any larger
# `expires_in` with StorageSignError BEFORE delegating to the backend — fail
# closed, never silently clamped. Matches the Azure Blob user-delegation SAS
# 7-day maximum, so a leaked URL self-expires within a week.
MAX_SIGNED_URL_TTL: Final = timedelta(days=7)


def validate_sas_ttl(ttl: timedelta, *, provider: str, key: str) -> None:
    """Fail-closed TTL guard shared by BOTH SAS/signed-URL paths — `ObjectStorage.signed_read_url`
    (blob-level) and `AppContainerStore.mint_container_sas` (container-level). The TTL must be
    positive and within `MAX_SIGNED_URL_TTL`; enforced in ONE place so the two paths can never
    drift, and a leaked URL/SAS always self-expires within the ceiling."""
    if ttl <= timedelta(0):
        raise StorageSignError("SAS TTL must be positive", provider=provider, key=key)
    if ttl > MAX_SIGNED_URL_TTL:
        raise StorageSignError(
            f"SAS TTL exceeds the {MAX_SIGNED_URL_TTL} ceiling", provider=provider, key=key
        )


# Upper bound on a single `put`. 5 GiB is a conservative single-request ceiling
# for an Azure block blob; larger objects would need staged block commits (a
# deferred follow-up).
MAX_PUT_BYTES: Final = 5 * 1024 * 1024 * 1024  # 5 GiB

# Default page size for prefix listings when the caller does not specify one.
DEFAULT_PAGE_SIZE: Final = 1000
