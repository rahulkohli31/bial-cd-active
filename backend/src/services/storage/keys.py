"""Single-tenant object-key builders + metadata normalization.

Single-tenant (ADR-0004): there is no `org_id`. Isolation is by the owning
`user_id` prefix, so badger's forgeable-string `ScopedStorage(org_id, project_id)`
is replaced by UUID-typed builders. The builders take `uuid.UUID`, never `str`:
a canonical UUID cannot contain `/`, `..`, or a control char, so the path-
traversal and prefix-collision attacks the multi-tenant `scoped_key` had to
defend against are structurally impossible here — the type IS the validation.

`assert_owned` is the fail-closed read-side guard: it re-checks that a stored key
lives strictly under the caller's `att/{user_id}/` prefix, using a TRAILING-SLASH
boundary (never a bare `startswith`) so one owner id can never be a prefix of
another. A dropped ownership check is a cross-user leak, not a style nit.

`normalize_metadata` / `normalize_metadata_key` are carried over verbatim from
badger — the backend calls `normalize_metadata` before handing user metadata to
the SDK, so the Azure metadata charset round-trips deterministically.
"""

from __future__ import annotations

import re
import uuid

from src.services.storage.errors import StorageError

# Azure metadata names must be valid C# identifiers (letters/digits/underscore,
# no leading digit, no hyphen). Lowercasing first, then enforcing this charset,
# makes a metadata round-trip deterministic.
_METADATA_KEY_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def owner_prefix(user_id: uuid.UUID) -> str:
    """The `att/{user_id}/` base for one user's attachments. The TRAILING SLASH
    is load-bearing: it is what `assert_owned` uses to stop one owner id being a
    prefix of another (UUIDs are fixed-length so a bare prefix collision cannot
    happen today, but the slash keeps the boundary honest under any future id)."""
    return f"att/{user_id}/"


def attachment_key(user_id: uuid.UUID, attachment_id: uuid.UUID) -> str:
    """Owner-scoped key for a user's attachment: `att/{user_id}/{attachment_id}`.
    Both axes are UUIDs, so the key is structurally traversal-safe."""
    return f"{owner_prefix(user_id)}{attachment_id}"


def app_file_key(app_id: uuid.UUID, file_id: uuid.UUID) -> str:
    """Key for a generated-app file: `apps/{app_id}/{file_id}`. App files are
    scoped by the owning app (whose own row is user-scoped), not directly by
    `user_id`, so they live under their own `apps/` namespace."""
    return f"apps/{app_id}/{file_id}"


def snapshot_key(app_id: uuid.UUID) -> str:
    """Key for a build session's C4 git-bundle snapshot: `snapshots/{app_id}/app.bundle`.
    Overwrite-latest — one bundle per app (the current-tree snapshot the sandbox restore
    pulls). An OPAQUE, SESSION-API-only artifact (C4) that no other track reads, so it
    lives under its own `snapshots/` namespace, uuid-typed like `app_file_key`."""
    return f"snapshots/{app_id}/app.bundle"


def assert_owned(key: str, user_id: uuid.UUID) -> None:
    """Fail-closed guard: raise unless `key` lives strictly under this user's
    `att/{user_id}/` prefix. The trailing slash + the length check defeat both a
    sibling-owner prefix collision and the bare owner root (which is not itself an
    object key)."""
    prefix = owner_prefix(user_id)
    if not key.startswith(prefix) or len(key) <= len(prefix):
        raise StorageError("key is outside the caller's owner scope")


def normalize_metadata_key(key: str) -> str:
    """Lowercase + validate one user-metadata key to the Azure metadata charset
    (a valid C# identifier) so it round-trips deterministically."""
    lowered = key.lower()
    if not _METADATA_KEY_RE.match(lowered):
        raise StorageError(
            f"invalid metadata key {key!r}: must match [a-z_][a-z0-9_]* after lowercasing"
        )
    return lowered


def normalize_metadata(metadata: dict[str, str] | None) -> dict[str, str] | None:
    """Normalize every key in a metadata mapping (values pass through). Backends
    call this before handing metadata to the SDK."""
    if metadata is None:
        return None
    return {normalize_metadata_key(k): v for k, v in metadata.items()}
