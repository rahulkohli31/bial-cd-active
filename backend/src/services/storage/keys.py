"""Single-tenant object-key builders + metadata normalization.

Builders take `uuid.UUID`, never `str`: a canonical UUID cannot contain `/`, `..`, or a control
char, so path traversal and prefix collision are structurally impossible — the type IS the
validation, and widening a builder to `str` puts both attacks back.

`assert_owned` is the fail-closed read-side guard: it re-checks a stored key lives strictly
under the caller's `att/{user_id}/` prefix via a TRAILING-SLASH boundary (never a bare
`startswith`), so one owner id can never be a prefix of another. `normalize_metadata`/
`normalize_metadata_key` run before metadata reaches the SDK, so the Azure charset round-trips.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

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
    `user_id`, so they live under their own `apps/` namespace.

    NO CURRENT WRITER: the per-app file model was dropped in migration 0017, so nothing
    calls this today. Kept rather than deleted because it is still the only correct
    spelling of the `apps/{app_id}/…` layout."""
    return f"apps/{app_id}/{file_id}"


def container_name(app_id: uuid.UUID) -> str:
    """Azure container name for an app's per-app Blob container: `app-{app_id}`. A UUID
    renders as 36 lowercase hex-and-hyphen chars, so `app-{uuid}` is 40 chars — comfortably
    within Azure's container-name rules (3–63 chars, lowercase alnum/hyphen, starts with a letter,
    no consecutive or trailing hyphens): the `app-` prefix is a letter start, and a UUID's own
    hyphens are always single and flanked by hex, so no `--` can ever form. The UUID type IS the
    validation — a canonical UUID cannot smuggle an uppercase char, `/`, `..`, or a control char.
    """
    return f"app-{app_id}"


def snapshot_key(app_id: uuid.UUID) -> str:
    """Key for a build session's git-bundle snapshot: `snapshots/{app_id}/app.bundle`.
    Overwrite-latest — one bundle per app (the current-tree snapshot the sandbox restore
    pulls). Written only by the session API, but not read only by it: `submit` copies it
    to an immutable `submission_key`, so this key stays mutable and is never what an
    approval pins. Lives under its own `snapshots/` namespace, uuid-typed like
    `app_file_key`."""
    return f"snapshots/{app_id}/app.bundle"


def recovery_key(app_id: uuid.UUID) -> str:
    """Key for an app's AUTOSAVED tree: `recovery/{app_id}/app.bundle`.

    A separate namespace from `snapshot_key`, deliberately: Save is the user's explicit action, and
    writing autosaves to `snapshot_key` would reverse that by the back door — it is the bundle
    `submit` copies and a relaunch restores, so an autosave there IS a save. It IS restored in
    place of the saved bundle when it holds a newer tree (`SessionManager.newest_restore_source`),
    but that is resumption, not promotion: `snapshot_key` stays untouched, so `dirty` stays true
    and only the user's click makes a VERSION. Overwrite-latest — a safety net, not a history."""
    return f"recovery/{app_id}/app.bundle"


def quarantine_prefix(app_id: uuid.UUID) -> str:
    """The `quarantine/{app_id}/` base for the trees set aside before a restore."""
    return f"quarantine/{app_id}/"


def quarantine_key(app_id: uuid.UUID, taken_at: datetime) -> str:
    """One tree parked aside before restoring over it: `quarantine/{app_id}/{stamp}.bundle`.

    Per-occurrence, unlike its two overwrite-latest siblings: a quarantine object is forensic
    evidence — in a false-`REVERTED` case, the only copy of the user's newest work — so a second
    reversion must not destroy the first one's record. Sortable, for the operator surface's
    chronological listing. Microsecond precision is collision-free structurally, not
    probabilistically: writes for one app are serialized by `snapshot._serialized_per_app`, one
    build slot per user, so two quarantine writes for one app can never be in flight together."""
    return f"{quarantine_prefix(app_id)}{_stamp(taken_at)}.bundle"


def version_prefix(app_id: uuid.UUID) -> str:
    """The `versions/{app_id}/` base for one app's saved versions."""
    return f"versions/{app_id}/"


def version_key(app_id: uuid.UUID, saved_at: datetime) -> str:
    """One version the citizen saved: `versions/{app_id}/{stamp}.bundle`.

    ★ NEVER DELETED BY THE VERSION LIST, and that is the rule the whole feature rests on. The
    list offers the two most recent plus whatever is live; a version that falls off it stops being
    OFFERED, not stored. Only the app going away takes these with it, through the three sweepers.

    Per-occurrence like `quarantine_key`, and for the sibling reason: `snapshot_key` is
    overwrite-latest because an app has one current tree, while a history has one object per save
    and a second save must not destroy the first. Sortable, so a listing reads chronologically
    without consulting the database. Microsecond precision is collision-free structurally rather
    than probabilistically: writes for one app are serialized by `snapshot._serialized_per_app`.

    A ROLLBACK MINTS A ROW THAT SHARES THIS KEY rather than copying the bytes. The restored
    content is identical, the store has no server-side copy, and the never-deleted rule above is
    what makes the sharing safe — so a key is in use while ANY row names it, never only the row
    that wrote it.
    """
    return f"{version_prefix(app_id)}{_stamp(saved_at)}.bundle"


def divert_prefix(app_id: uuid.UUID) -> str:
    """The `divert/{app_id}/` base for trees the recovery guard refused to promote."""
    return f"divert/{app_id}/"


def divert_key(app_id: uuid.UUID, taken_at: datetime) -> str:
    """One tree the recovery guard refused to write over a good recovery copy:
    `divert/{app_id}/{stamp}.bundle`.

    Mirrors `quarantine_key`, including the per-occurrence rule and for the same reason: a shared
    overwrite-latest key means a second refusal destroys the forensic evidence the alarm exists to
    preserve."""
    return f"{divert_prefix(app_id)}{_stamp(taken_at)}.bundle"


def _stamp(taken_at: datetime) -> str:
    """A sortable, path-safe UTC instant: `20260823T134500123456Z`.

    NORMALIZED TO UTC rather than trusting the caller's tzinfo, because two objects stamped from
    different offsets would sort by wall clock rather than by when they happened — and a naive
    datetime raises rather than being silently read as UTC, which is the one reading that would
    quietly reorder an operator's evidence."""
    if taken_at.tzinfo is None:
        raise StorageError("a stamped storage key needs an aware datetime")
    return taken_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def submissions_prefix(app_id: uuid.UUID) -> str:
    """The `submissions/{app_id}/` base for one app's immutable submission bundles.
    The TRAILING SLASH is load-bearing (as `owner_prefix` documents): it keeps the
    boundary honest under any future id shape, and the delete-path prefix sweep
    lists exactly this."""
    return f"submissions/{app_id}/"


def submission_key(app_id: uuid.UUID, submission_id: uuid.UUID) -> str:
    """Key for ONE immutable submission bundle:
    `submissions/{app_id}/{submission_id}.bundle`. Written exactly once at submit —
    immutability comes from key derivation (a fresh `submission_id` per submit; ids
    are never reused), never from the store (`put` is overwrite-always). Both axes
    are UUIDs, so the key is structurally traversal-safe — the type IS the
    validation. The key is DERIVABLE from the registry row's
    `(app_id, submission_id)`, so it is never stored."""
    return f"{submissions_prefix(app_id)}{submission_id}.bundle"


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


SNAPSHOT_HEAD_METADATA_KEY = "head_sha"
"""The user-metadata key `write_snapshot` stamps a stored bundle's HEAD commit with
(`build_sessions/snapshot.py`). Named here, beside the key builders, because it is the
one thing a reader and the writer must agree on across four modules that never call
each other — and a metadata key that is only ever a string literal drifts silently: a
typo reads as "no stamp", which every caller is written to tolerate."""


def head_sha_from_metadata(metadata: Mapping[str, str] | None) -> str | None:
    """The tree a stored bundle holds, from the metadata the writer stamped on it.

    None means NO CLAIM — the object predates the stamp, or carries an empty one — and
    every caller treats that as "cannot compare" rather than as a version. Callers pass
    `meta.metadata` from a `head()`; the storage call and what an unreadable store means
    stay theirs, because the answers genuinely differ (a review says "nothing to check
    yet", the publish gate says "nothing to deploy")."""
    if not metadata:
        return None
    return metadata.get(SNAPSHOT_HEAD_METADATA_KEY) or None
