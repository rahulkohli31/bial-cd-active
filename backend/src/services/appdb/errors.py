"""Per-project-database error types.

Mirrors `services/storage/errors.py`: "unconfigured" is a DISTINCT type, not a message —
it is not a substrate failing, it is the ABSENCE of one, and callers that must tell the
two apart branch on the type instead of parsing a string.
"""

from __future__ import annotations


class AppDatabaseError(Exception):
    """Base for every per-project-database failure raised by this package."""


class AppDatabaseUnconfiguredError(AppDatabaseError):
    """No `APP_DB__*` block is configured.

    The provisioning entrypoints never raise this — they no-op, because a project without a
    database is a supported deployment (see `provision.ensure_project_database`). It exists
    for the few helpers that are only reachable once a row already claims a database (DSN
    assembly, password decryption), where an absent key is a genuine contradiction.
    """
