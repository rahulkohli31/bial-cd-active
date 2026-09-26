"""The classification configuration as the migration leaves it: the seeded launch set, the one
policy row, the rules the database itself holds, and the review's fingerprint column.

Pinned against the migrated schema in raw SQL rather than through the models, because the
migration and the model each state these rules once and autogenerate only notices when the two
disagree.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_REVISION = "0049_classification_config"

_INSERT_CLASS = sa.text(
    "INSERT INTO classification_classes (key, title, description, kind, weight) "
    "VALUES (:key, :title, 'Yes if it does. No: a calculator.', "
    "CAST(:kind AS classification_kind), :weight)"
)


async def test_the_seed_is_two_hard_blocks_and_five_classes_scored_at_twenty(db_session) -> None:
    rows = (
        await db_session.execute(
            sa.text("SELECT key, kind::text AS kind, weight, active FROM classification_classes")
        )
    ).all()
    assert sorted((row.key, row.kind, row.weight, row.active) for row in rows) == [
        ("ai_usage", "scored", 20, True),
        ("confidential_business_data", "scored", 20, True),
        ("credentials_keys", "scored", 20, True),
        ("financial_data", "hard_block", None, True),
        ("integrations", "scored", 20, True),
        ("pii", "hard_block", None, True),
        ("public_data", "scored", 20, True),
    ]


async def test_the_policy_is_one_row_at_threshold_one_hundred_with_owners_able_to_change(
    db_session,
) -> None:
    rows = (
        await db_session.execute(
            sa.text("SELECT threshold, owners_can_change_answers FROM classification_policy")
        )
    ).all()
    assert [(row.threshold, row.owners_can_change_answers) for row in rows] == [(100, True)]


async def test_the_public_data_description_carries_the_calculator_probe(db_session) -> None:
    description = await db_session.scalar(
        sa.text("SELECT description FROM classification_classes WHERE key = 'public_data'")
    )
    assert description is not None
    assert "No: a calculator." in description


async def test_the_database_refuses_a_second_policy_row(db_session) -> None:
    with pytest.raises(IntegrityError, match="uq_classification_policy_one_row"):
        async with db_session.begin_nested():
            await db_session.execute(
                sa.text(
                    "INSERT INTO classification_policy (threshold, owners_can_change_answers) "
                    "VALUES (50, false)"
                )
            )


async def test_the_database_refuses_a_title_that_differs_only_in_case(db_session) -> None:
    with pytest.raises(IntegrityError, match="uq_classification_classes_title"):
        async with db_session.begin_nested():
            await db_session.execute(
                _INSERT_CLASS,
                {"key": "pii_2", "title": "pIi", "kind": "hard_block", "weight": None},
            )


async def test_the_database_refuses_a_second_class_with_the_same_key(db_session) -> None:
    with pytest.raises(IntegrityError, match="uq_classification_classes_key"):
        async with db_session.begin_nested():
            await db_session.execute(
                _INSERT_CLASS,
                {"key": "pii", "title": "Other", "kind": "hard_block", "weight": None},
            )


@pytest.mark.parametrize(
    ("kind", "weight"),
    [("scored", None), ("scored", 101), ("scored", -1), ("hard_block", 20), ("hard_block", 0)],
)
async def test_the_database_holds_the_weight_to_the_kind(db_session, kind, weight) -> None:
    """A NULL weight on a scored class would pass a bare range check, because a CHECK passes on
    UNKNOWN; the constraint has to say NOT NULL in the scored arm."""
    with pytest.raises(IntegrityError, match="ck_classification_classes_weight_for_kind"):
        async with db_session.begin_nested():
            await db_session.execute(
                _INSERT_CLASS, {"key": "probe", "title": "Probe", "kind": kind, "weight": weight}
            )


async def test_the_database_holds_the_threshold_to_zero_through_one_hundred(db_session) -> None:
    for threshold in (-1, 101):
        with pytest.raises(IntegrityError, match="ck_classification_policy_threshold"):
            async with db_session.begin_nested():
                await db_session.execute(
                    sa.text("UPDATE classification_policy SET threshold = :threshold"),
                    {"threshold": threshold},
                )


async def test_reviews_gain_a_nullable_definitions_fingerprint(db_session) -> None:
    row = (
        await db_session.execute(
            sa.text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'classification_reviews' "
                "AND column_name = 'definitions_fingerprint'"
            )
        )
    ).one()
    assert row.is_nullable == "YES"
    assert row.column_default is None


def _snapshot() -> dict[str, Any]:
    """What this revision owns, read on a fresh NullPool engine because alembic's env.py owns the
    event loop while the commands run."""

    async def _read() -> dict[str, Any]:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                classes = await conn.scalar(
                    sa.text("SELECT to_regclass('classification_classes')")
                )
                policy = await conn.scalar(sa.text("SELECT to_regclass('classification_policy')"))
                labels = (
                    await conn.scalars(
                        sa.text(
                            "SELECT enumlabel FROM pg_enum e "
                            "JOIN pg_type t ON t.oid = e.enumtypid "
                            "WHERE t.typname = 'classification_kind' "
                            "ORDER BY e.enumsortorder"
                        )
                    )
                ).all()
                fingerprint = await conn.scalar(
                    sa.text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'classification_reviews' "
                        "AND column_name = 'definitions_fingerprint'"
                    )
                )
                seeded = policies = None
                if classes is not None and policy is not None:
                    seeded = await conn.scalar(
                        sa.text("SELECT count(*) FROM classification_classes")
                    )
                    policies = await conn.scalar(
                        sa.text("SELECT count(*) FROM classification_policy")
                    )
        finally:
            await engine.dispose()
        return {
            "classes": classes,
            "policy": policy,
            "labels": list(labels),
            "fingerprint": fingerprint,
            "seeded": seeded,
            "policies": policies,
        }

    return asyncio.run(_read())


@pytest.mark.destructive_migration
def test_the_configuration_round_trips() -> None:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    previous = ScriptDirectory.from_config(config).get_revision(_REVISION).down_revision
    assert isinstance(previous, str)

    try:
        command.downgrade(config, previous)
        removed = _snapshot()
        assert removed["classes"] is None
        assert removed["policy"] is None
        assert removed["labels"] == []
        assert removed["fingerprint"] is None
    finally:
        command.upgrade(config, "head")

    restored = _snapshot()
    assert restored["classes"] is not None
    assert restored["policy"] is not None
    assert restored["labels"] == ["hard_block", "scored"]
    assert restored["fingerprint"] == 1
    assert restored["seeded"] == 7
    assert restored["policies"] == 1
