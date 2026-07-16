"""Computed-role RBAC unit tests (pure functions — no DB, no live Settings)."""

from __future__ import annotations

import pytest

from src.services.rbac.roles import Role, is_super_duper_admin, is_superadmin_email, role_for
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


def test_is_superadmin_email_is_the_same_check_is_super_duper_admin_delegates_to() -> None:
    # The raw-email half used by callers with no User row yet (e.g. the SSO callback
    # deciding whether to auto-approve a brand-new insert) — must agree with
    # is_super_duper_admin for the exact same email in every case above.
    assert is_superadmin_email("admin@bial.com", _ALLOWLIST) is True
    assert is_superadmin_email("ADMIN@BIAL.COM", _ALLOWLIST) is True
    assert is_superadmin_email("citizen@bial.com", _ALLOWLIST) is False
    assert is_superadmin_email("admin@bial.com", frozenset()) is False
