"""The live classification configuration: the active classes in creation order, and the policy."""

from __future__ import annotations

import sqlalchemy as sa

from src.db.models.classification_config import (
    ClassificationClass,
    ClassificationKind,
    ClassificationPolicy,
)
from src.services.classification.config import LiveClass, load_live_config


async def test_the_launch_set_loads_in_creation_order_with_the_seeded_policy(db_session) -> None:
    """Creation order, never kind or weight order: the screens break weight ties by it, and a
    weight edit must not reorder it."""
    live = await load_live_config(db_session)

    assert live.threshold == 100
    assert live.owners_can_change_answers is True
    assert [(entry.key, entry.kind, entry.weight) for entry in live.classes] == [
        ("pii", ClassificationKind.HARD_BLOCK, None),
        ("financial_data", ClassificationKind.HARD_BLOCK, None),
        ("credentials_keys", ClassificationKind.SCORED, 20),
        ("confidential_business_data", ClassificationKind.SCORED, 20),
        ("ai_usage", ClassificationKind.SCORED, 20),
        ("integrations", ClassificationKind.SCORED, 20),
        ("public_data", ClassificationKind.SCORED, 20),
    ]
    public_data = live.classes[-1]
    assert public_data == LiveClass(
        key="public_data",
        title="Public data",
        description=(
            "Yes if the app shows or publishes information that is already public, such as the "
            "airport's published opening hours or public reference lists. The app may hold "
            "other data as well; judge the public part on its own. Data from the platform's "
            "own connections, such as Flight Fact Data (DICE), is internal, not public. "
            "Results worked out from what the user types, constants built into a tool, and "
            "pick-lists on a form do not count either, so an app that handles no data answers "
            "No. Yes: a page of the airport's published shop opening hours. Yes: a list of "
            "public holidays. Yes: a staff roster that shows the public holiday list beside "
            "internal shifts. No: a flight board fed by Flight Fact Data. No: a calculator. "
            "No: a complaint form with a drop-down of terminals."
        ),
        kind=ClassificationKind.SCORED,
        weight=20,
    )


async def test_a_class_added_later_comes_last_whatever_its_key(db_session) -> None:
    db_session.add(
        ClassificationClass(
            key="aaa_first_by_key",
            title="File uploads",
            description="Yes if the app accepts file uploads. No: a calculator.",
            kind=ClassificationKind.SCORED,
            weight=20,
        )
    )
    await db_session.flush()

    live = await load_live_config(db_session)

    assert live.classes[-1].key == "aaa_first_by_key"


async def test_an_inactive_class_is_not_live(db_session) -> None:
    await db_session.execute(
        sa.update(ClassificationClass)
        .where(ClassificationClass.key == "public_data")
        .values(active=False)
    )

    live = await load_live_config(db_session)

    assert [entry.key for entry in live.classes] == [
        "pii",
        "financial_data",
        "credentials_keys",
        "confidential_business_data",
        "ai_usage",
        "integrations",
    ]


async def test_the_policy_is_read_live(db_session) -> None:
    await db_session.execute(
        sa.update(ClassificationPolicy).values(threshold=40, owners_can_change_answers=False)
    )

    live = await load_live_config(db_session)

    assert (live.threshold, live.owners_can_change_answers) == (40, False)
