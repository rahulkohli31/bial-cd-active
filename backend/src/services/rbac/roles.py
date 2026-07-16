"""Computed RBAC roles — no stored role column (ADR-0005 drift cleared, KTD).

A user's role is DERIVED per request from `settings.superadmin_emails` vs the
authenticated user's email (case-insensitive), never a mutable DB column. This
module is the pure computation; the FastAPI gate lives in `api/deps_rbac.py`.

Single-tenant, 2-admin model (R7): everyone is a citizen by default; a handful of
env-configured emails are super-admins. Per `.claude/rules/naming.md` the internal
predicate is `is_super_duper_admin`; the public role name stays plain.
"""

from __future__ import annotations

from enum import StrEnum

from src.db.models.user import User


class Role(StrEnum):
    """The two computed roles. Values are stable, professional strings safe to
    surface in an API response (unlike the witty internal predicate name)."""

    CITIZEN = "citizen"
    SUPER_ADMIN = "super_admin"


def is_superadmin_email(email: str, superadmin_emails: frozenset[str]) -> bool:
    """True iff `email` is on the env allowlist (case-insensitive).

    The raw-email half of `is_super_duper_admin` below, split out so callers that
    don't have a `User` row yet — e.g. the SSO callback deciding whether to
    auto-approve a brand-new insert, before any row exists — can share the exact
    same allowlist check instead of re-deriving it.
    """
    return email.lower() in superadmin_emails


def is_super_duper_admin(user: User, superadmin_emails: frozenset[str]) -> bool:
    """True iff the user's email is on the env allowlist (case-insensitive).

    Pure by design: the allowlist is passed in (already lowercased by the config
    validator) so this is trivially unit-testable and the caller owns the source.
    Fail-closed — an email not on the list is a plain citizen.
    """
    return is_superadmin_email(user.email, superadmin_emails)


def role_for(user: User, superadmin_emails: frozenset[str]) -> Role:
    """Compute the caller's role from the allowlist."""
    return Role.SUPER_ADMIN if is_super_duper_admin(user, superadmin_emails) else Role.CITIZEN
