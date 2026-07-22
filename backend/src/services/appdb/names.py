"""Deterministic per-project database/role names, their fail-closed inverse parser,
and the ONE helper allowed to interpolate a name into DDL.

Names are derived from the project UUID and nothing else, so provisioning is idempotent
by re-derivation (a retry computes the same name; a duplicate-object SQLSTATE is then the
idempotency signal) and teardown handles are re-derivable with no schema baggage:

    database:  bialapp_<project_id.hex>    #  8 + 32 = 40 chars (PG limit is 63)
    role:      bialrole_<project_id.hex>   #  9 + 32 = 41 chars

The FULL uuid hex (32 lowercase chars, no dashes) is carried, not a truncation, so the
orphan reconciler's diff is an exact registry-existence check rather than a fuzzy prefix
scan — and the prefix + full-uuid anchor is what stops the reconciler from ever
mis-attributing an unrelated database on a shared dev server.

Identifiers CANNOT be bound as query parameters, so `quote_identifier` is the single
sanctioned interpolation seam (`.claude/rules/security.md`): it validates against a closed
character class BEFORE quoting, and every DDL statement in this package builds its
identifiers through it. Same posture for the role password via `quote_password_literal` —
`CREATE ROLE ... PASSWORD` takes a literal, never a bind parameter.
"""

from __future__ import annotations

import re
import uuid
from typing import Final

DATABASE_PREFIX: Final = "bialapp_"
ROLE_PREFIX: Final = "bialrole_"

# PostgreSQL's identifier cap is 63 bytes. Lowercase + [a-z0-9_] only means the name is
# never a reserved word needing case-preservation games and never collides with `pg_*`.
_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# `secrets.token_urlsafe` emits base64url, i.e. exactly this alphabet — no quote, no
# backslash, nothing that can terminate a SQL literal. Validated (never assumed) before
# the password is interpolated into `CREATE ROLE ... PASSWORD '<pw>'`.
_SAFE_PASSWORD = re.compile(r"^[A-Za-z0-9_-]{16,256}$")

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class UnsafeIdentifierError(ValueError):
    """A name or password failed its character-class check and must never reach DDL.

    Raised, not asserted: `python -O` strips `assert`, and this is a SQL-injection guard
    (`.claude/rules/fail-first-python.md`).
    """


def database_name(project_id: uuid.UUID) -> str:
    """`bialapp_<project_id.hex>` — the project's own database."""
    return f"{DATABASE_PREFIX}{project_id.hex}"


def role_name(project_id: uuid.UUID) -> str:
    """`bialrole_<project_id.hex>` — the login role that owns that database."""
    return f"{ROLE_PREFIX}{project_id.hex}"


def project_id_from_database_name(name: str) -> uuid.UUID | None:
    """The owning project id, or `None` when `name` is not one of ours.

    FAIL-CLOSED: anything that does not parse exactly — wrong prefix, wrong length,
    non-hex tail, a uuid the constructor rejects — is reported as unowned/not-actionable
    rather than guessed at. The reconciler (U7) turns `None` into "leave it alone", which
    is what protects the unrelated databases sharing a dev server. Mirrors
    `_owned_by_app_row` in `services/storage/reconcile.py`.

    `None` here is a legitimately-absent result ("this name is not a project database"),
    not an error channel.
    """
    return _project_id_after(name, DATABASE_PREFIX)


def project_id_from_role_name(name: str) -> uuid.UUID | None:
    """The owning project id for a role name, or `None`. Same fail-closed contract as
    `project_id_from_database_name`."""
    return _project_id_after(name, ROLE_PREFIX)


def _project_id_after(name: str, prefix: str) -> uuid.UUID | None:
    if not name.startswith(prefix):
        return None
    tail = name[len(prefix) :]
    if _HEX32.match(tail) is None:
        return None
    try:
        return uuid.UUID(hex=tail)
    except ValueError:
        return None


def quote_identifier(name: str) -> str:
    """Validate `name` against `^[a-z][a-z0-9_]{0,62}$` and return it double-quoted.

    THE only place a database/role name becomes part of a SQL string. Identifiers are not
    bindable, so the safety argument has to be the character class: the closed class admits
    no `"`, so the quoting cannot be escaped out of, and the leading-letter + length rules
    keep the result a legal PostgreSQL identifier.
    """
    if _SAFE_IDENTIFIER.match(name) is None:
        # STATIC message — the rejected value is exactly the thing we refuse to propagate.
        raise UnsafeIdentifierError(
            "refusing to build DDL: identifier must match ^[a-z][a-z0-9_]{0,62}$"
        )
    return f'"{name}"'


def quote_password_literal(password: str) -> str:
    """Validate a `secrets.token_urlsafe` password and return it single-quoted.

    `CREATE ROLE ... PASSWORD` takes a string literal that asyncpg cannot bind (the whole
    statement is DDL), so the same parse-then-interpolate discipline as `quote_identifier`
    applies. The base64url class contains no `'` and no backslash, so the literal is closed.
    """
    if _SAFE_PASSWORD.match(password) is None:
        raise UnsafeIdentifierError(
            "refusing to build DDL: role password must be url-safe base64 "
            "(secrets.token_urlsafe), 16-256 chars"
        )
    return f"'{password}'"
