"""The live classification configuration: the active classes in key order, and the policy."""

from __future__ import annotations

import sqlalchemy as sa

from src.db.models.classification_config import (
    ClassificationClass,
    ClassificationKind,
    ClassificationPolicy,
)
from src.services.classification.config import LiveClass, load_live_config


async def test_the_launch_set_loads_in_key_order_with_the_seeded_policy(db_session) -> None:
    """Key order, never kind or weight order: the reviewer's prompt block is rendered in this
    order, and a weight edit must not reorder it."""
    live = await load_live_config(db_session)

    assert live.threshold == 100
    assert live.owners_can_change_answers is True
    assert [(entry.key, entry.kind, entry.weight) for entry in live.classes] == [
        ("ai_usage", ClassificationKind.SCORED, 20),
        ("confidential_business_data", ClassificationKind.SCORED, 20),
        ("credentials_keys", ClassificationKind.SCORED, 20),
        ("financial_data", ClassificationKind.HARD_BLOCK, None),
        ("integrations", ClassificationKind.SCORED, 20),
        ("pii", ClassificationKind.HARD_BLOCK, None),
        ("public_data", ClassificationKind.SCORED, 20),
    ]
    public_data = live.classes[-1]
    assert public_data == LiveClass(
        key="public_data",
        title="Public data",
        description=(
            "Yes if the app shows or publishes information that is already public, such as "
            "published flight schedules or public reference lists. The app may hold other data as "
            "well; judge the public part on its own. Results worked out from what the user types, "
            "constants built into a tool, and pick-lists on a form do not count, so an app that "
            "handles no data answers No. Yes: a page of the airport's published shop opening "
            "hours. Yes: a list of public holidays. Yes: a dashboard showing the published flight "
            "schedule beside internal targets. No: a calculator. No: a unit converter. No: a "
            "complaint form with a drop-down of terminals."
        ),
        kind=ClassificationKind.SCORED,
        weight=20,
    )


async def test_keys_sort_in_byte_order_whatever_the_collation(db_session) -> None:
    """A linguistic collation ignores the underscore and puts `zza` first; byte order puts
    `zz_z` first. The order must not change with the server it runs on."""
    for key in ("zza", "zz_z"):
        db_session.add(
            ClassificationClass(
                key=key,
                title=key,
                description="Yes if it does. No: a calculator.",
                kind=ClassificationKind.HARD_BLOCK,
            )
        )
    await db_session.flush()

    live = await load_live_config(db_session)

    assert [entry.key for entry in live.classes][-2:] == ["zz_z", "zza"]


async def test_an_inactive_class_is_not_live(db_session) -> None:
    await db_session.execute(
        sa.update(ClassificationClass)
        .where(ClassificationClass.key == "public_data")
        .values(active=False)
    )

    live = await load_live_config(db_session)

    assert [entry.key for entry in live.classes] == [
        "ai_usage",
        "confidential_business_data",
        "credentials_keys",
        "financial_data",
        "integrations",
        "pii",
    ]


async def test_the_policy_is_read_live(db_session) -> None:
    await db_session.execute(
        sa.update(ClassificationPolicy).values(threshold=40, owners_can_change_answers=False)
    )

    live = await load_live_config(db_session)

    assert (live.threshold, live.owners_can_change_answers) == (40, False)
