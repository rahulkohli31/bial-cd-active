"""Deterministic names, the fail-closed inverse parser, and the DDL quoting seam."""

from __future__ import annotations

import uuid

import pytest

from src.services.appdb.names import (
    DATABASE_PREFIX,
    ROLE_PREFIX,
    UnsafeIdentifierError,
    database_name,
    project_id_from_database_name,
    project_id_from_role_name,
    quote_identifier,
    quote_password_literal,
    role_name,
)

_PROJECT = uuid.UUID("018f3b2c-1a2b-7c3d-8e4f-5a6b7c8d9e0f")


def test_names_are_deterministic_and_carry_the_full_uuid() -> None:
    # Full hex, not a truncation: the reconciler's diff is then an exact registry
    # existence check rather than a fuzzy prefix scan.
    assert database_name(_PROJECT) == f"{DATABASE_PREFIX}{_PROJECT.hex}"
    assert role_name(_PROJECT) == f"{ROLE_PREFIX}{_PROJECT.hex}"
    assert database_name(_PROJECT) == database_name(_PROJECT)


def test_names_fit_postgres_identifier_limit_and_need_no_quoting() -> None:
    for name in (database_name(_PROJECT), role_name(_PROJECT)):
        assert len(name) <= 63
        # Lowercase [a-z0-9_] only — never a reserved word, never a `pg_*` collision.
        assert quote_identifier(name) == f'"{name}"'


def test_round_trip_through_the_inverse_parser() -> None:
    assert project_id_from_database_name(database_name(_PROJECT)) == _PROJECT
    assert project_id_from_role_name(role_name(_PROJECT)) == _PROJECT


@pytest.mark.parametrize(
    "name",
    [
        "postgres",
        "template0",
        "template1",
        "citizen_one",
        "citizen_one_test",
        "azure_maintenance",
        # Real, unrelated databases on the shared dev cluster — the name anchor is the
        # ONLY thing standing between them and a reconciler's DROP.
        "ascend",
        "badger",
        "knit",
        "neox",
        "singularity",
        "tod",
        # Near misses: right prefix, wrong payload.
        "bialapp_",
        "bialapp_not-hex",
        f"bialapp_{_PROJECT.hex[:31]}",
        f"bialapp_{_PROJECT.hex}extra",
        f"bialapp_{_PROJECT.hex.upper()}",
        # A role name is not a database name.
        f"bialrole_{_PROJECT.hex}",
    ],
)
def test_inverse_parser_fails_closed_on_anything_not_ours(name: str) -> None:
    # `None` means "not actionable" — the reconciler leaves it alone. Anything that does
    # not parse EXACTLY must land here; a lenient parser would be a licence to drop an
    # unrelated database.
    assert project_id_from_database_name(name) is None


def test_role_parser_rejects_a_database_name() -> None:
    assert project_id_from_role_name(database_name(_PROJECT)) is None


@pytest.mark.parametrize(
    "name",
    [
        "",
        "1leading_digit",
        "Uppercase",
        'quote"injection',
        "semi;colon",
        "space name",
        "dash-name",
        "a" * 64,
        'x" OR "1"="1',
    ],
)
def test_quote_identifier_refuses_anything_outside_the_closed_class(name: str) -> None:
    # Identifiers are not bindable, so this check IS the injection defence.
    with pytest.raises(UnsafeIdentifierError):
        quote_identifier(name)


def test_quote_identifier_message_does_not_echo_the_rejected_value() -> None:
    with pytest.raises(UnsafeIdentifierError) as caught:
        quote_identifier("'; DROP DATABASE citizen_one; --")
    assert "DROP DATABASE" not in str(caught.value)


def test_quote_password_literal_accepts_a_token_urlsafe_password() -> None:
    import secrets as stdlib_secrets

    token = stdlib_secrets.token_urlsafe(32)
    assert quote_password_literal(token) == f"'{token}'"


@pytest.mark.parametrize(
    "password",
    ["", "short", "has'quote" + "x" * 20, "has\\backslash" + "x" * 20, "has space" + "x" * 20],
)
def test_quote_password_literal_refuses_anything_that_could_close_the_literal(
    password: str,
) -> None:
    with pytest.raises(UnsafeIdentifierError):
        quote_password_literal(password)
