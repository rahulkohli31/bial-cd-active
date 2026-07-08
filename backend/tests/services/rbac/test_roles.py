"""Computed-role RBAC unit tests (pure functions — no DB, no live Settings)."""

from __future__ import annotations

import pytest

from src.services.rbac.roles import Role, is_super_duper_admin, role_for
from tests.factories import UserFactory

_ALLOWLIST = frozenset({"admin@bial.com", "superadmin@bial.com"})


def test_allowlisted_email_is_super_admin() -> None:
    user = UserFactory.build(email="admin@bial.com")
    assert is_super_duper_admin(user, _ALLOWLIST) is True
    assert role_for(user, _ALLOWLIST) is Role.SUPER_ADMIN


def test_absent_email_is_citizen() -> None:
    user = UserFactory.build(email="citizen@bial.com")
    assert is_super_duper_admin(user, _ALLOWLIST) is False
    assert role_for(user, _ALLOWLIST) is Role.CITIZEN


@pytest.mark.parametrize("email", ["ADMIN@BIAL.COM", "Admin@Bial.com", "admin@bial.com"])
def test_matching_is_case_insensitive(email: str) -> None:
    # AE1: the allowlist is lowercased at config load and the user email is
    # lowercased here, so any case the IdP echoes still resolves to super-admin.
    user = UserFactory.build(email=email)
    assert role_for(user, _ALLOWLIST) is Role.SUPER_ADMIN


def test_empty_allowlist_denies_everyone() -> None:
    # Fail-closed: an empty allowlist means there are no super-admins.
    user = UserFactory.build(email="admin@bial.com")
    assert role_for(user, frozenset()) is Role.CITIZEN
