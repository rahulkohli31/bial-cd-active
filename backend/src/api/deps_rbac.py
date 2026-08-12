"""RBAC FastAPI dependencies — the privileged-action gate (R8).

`requires_superadmin` layers AFTER `current_user` (authentication) and denies with
a plain 403 when the caller is not a super-admin. Plan A ships this dependency;
Plan B's admin routes consume it. The allowlist is injected via a tiny provider
dependency so tests override it with `dependency_overrides` — no global mutation of
the Settings singleton. FastAPI resolves the provider (and thus `current_user`)
before this gate runs, satisfying the "gate after identity" ordering.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status

from src.api.deps import CurrentUser
from src.config import settings
from src.db.models.user import User
from src.services.rbac.roles import is_super_duper_admin


def superadmin_allowlist() -> frozenset[str]:
    """The configured super-admin email allowlist. A dependency (not a bare
    settings read at the callsite) so tests override it via `dependency_overrides`
    without mutating the shared Settings singleton."""
    return settings.superadmin_emails


async def requires_superadmin(
    user: CurrentUser,
    allowlist: Annotated[frozenset[str], Depends(superadmin_allowlist)],
) -> User:
    """Gate a privileged action to super-admins, returning the live `User` so the
    endpoint can use it. Denies with a plain, non-leaking 403 otherwise. Fail-closed:
    anyone not on the allowlist is a citizen and is denied (AE1)."""
    if not is_super_duper_admin(user, allowlist):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super-admin privileges required.",
        )
    return user


CurrentSuperadmin = Annotated[User, Depends(requires_superadmin)]
