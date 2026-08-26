"""The move of already-published apps onto the shared apps hostname.

THE ONE RULE THIS FILE EXISTS TO PIN: republish before rewrite. An image built before the base
path shipped serves at `/`, so pointing a live link at `/a/pub-<key>/` makes the app answer 404 —
turning today's honest name-resolution failure into a page that reads as "the platform broke my
app". The gate is structural, not procedural: a human-recorded address may only move once the
platform's OWN address has already moved, which is the observable proof a republished image
exists.
"""

from __future__ import annotations

import uuid

import pytest

from src.services.deploy.backfill import AddressState, AppAddresses, classify, decide
from src.services.deploy.names import published_app_name

APPS = "https://citizenapps.bialairport.com"
APP_ID = uuid.UUID("01a03cfd-93bd-74ea-a3ad-3be74c8cdb3a")
NAME = published_app_name(APP_ID)
OLD = f"https://{NAME}.proudrock-a96baf92.centralindia.azurecontainerapps.io/"
NEW = f"{APPS}/a/{NAME}/"


# --- classify ---------------------------------------------------------------------------------


def test_the_old_container_address_is_recognised_as_ours() -> None:
    assert classify(OLD, app_id=APP_ID, apps_base_url=APPS) is AddressState.NAMES_ITS_CONTAINER


def test_the_new_address_is_recognised_as_already_moved() -> None:
    """Idempotency. Re-running the pass must be a no-op, not a second rewrite."""
    assert classify(NEW, app_id=APP_ID, apps_base_url=APPS) is AddressState.ALREADY_MOVED


@pytest.mark.parametrize("recorded", [None, "", "   "])
def test_nothing_recorded_is_nothing_to_move(recorded: str | None) -> None:
    assert classify(recorded, app_id=APP_ID, apps_base_url=APPS) is AddressState.ABSENT


@pytest.mark.parametrize(
    "recorded",
    [
        "https://someapp.bialairport.com/",  # a genuinely different location
        "https://internal-tool.corp.example/apps/finance",
        "not a url at all",
    ],
)
def test_a_human_typed_address_elsewhere_is_left_alone(recorded: str) -> None:
    """An operator may have recorded a genuinely different location. Silently repointing it at a
    container would be worse than leaving a stale note — this backfill corrects OUR mistake, not
    somebody else's record."""
    assert classify(recorded, app_id=APP_ID, apps_base_url=APPS) is AddressState.SOMEWHERE_ELSE


def test_another_apps_container_is_not_treated_as_ours() -> None:
    """The match is per-row and exact. A suffix rule over the Container Apps domain would have
    rewritten this row to the WRONG app's address, which is the one outcome worse than leaving
    it stale."""
    other = uuid.UUID("01a03cfd-0000-7000-8000-000000000001")
    stranger = f"https://{published_app_name(other)}.centralindia.azurecontainerapps.io/"
    assert classify(stranger, app_id=APP_ID, apps_base_url=APPS) is AddressState.SOMEWHERE_ELSE


def test_the_right_host_carrying_the_wrong_key_is_not_called_moved() -> None:
    """A real defect, and calling it "already moved" would hide it behind a no-op forever."""
    other = published_app_name(uuid.UUID("01a03cfd-0000-7000-8000-000000000001"))
    assert (
        classify(f"{APPS}/a/{other}/", app_id=APP_ID, apps_base_url=APPS)
        is AddressState.SOMEWHERE_ELSE
    )


# --- decide: the republish-before-rewrite gate -------------------------------------------------


def test_a_recorded_address_does_not_move_before_the_app_is_republished() -> None:
    """★ THE MUTANT THAT MUST FAIL. Drop the gate and this app's live link stops resolving and
    starts 404ing instead — the platform confidently answering "not here" about an app that is
    there, at a different path, because its image predates the base path."""
    action = decide(AppAddresses(APP_ID, platform_url=OLD, recorded_url=OLD), apps_base_url=APPS)
    assert action.rewrite_recorded_to is None
    assert action.blocked_reason is not None
    assert "republished" in action.blocked_reason


def test_a_republished_app_moves_its_recorded_address() -> None:
    """The platform address having moved IS the proof an image serving under the key exists —
    the pipeline's success terminal writes it, so nothing else has to be trusted."""
    action = decide(AppAddresses(APP_ID, platform_url=NEW, recorded_url=OLD), apps_base_url=APPS)
    assert action.rewrite_recorded_to == NEW
    assert action.blocked_reason is None


def test_a_republished_app_with_nothing_recorded_is_left_alone() -> None:
    """`deployed_url` is only written when an admin ran the manual go-live. An absent value is
    not a stale value, and inventing one would publish a link nobody agreed to."""
    action = decide(AppAddresses(APP_ID, platform_url=NEW, recorded_url=None), apps_base_url=APPS)
    assert action.rewrite_recorded_to is None
    assert action.blocked_reason is None


def test_running_the_pass_twice_writes_nothing_the_second_time() -> None:
    first = decide(AppAddresses(APP_ID, platform_url=NEW, recorded_url=OLD), apps_base_url=APPS)
    assert first.rewrite_recorded_to == NEW
    second = decide(
        AppAddresses(APP_ID, platform_url=NEW, recorded_url=first.rewrite_recorded_to),
        apps_base_url=APPS,
    )
    assert second.rewrite_recorded_to is None
    assert second.blocked_reason is None


def test_a_human_typed_elsewhere_survives_even_after_the_republish() -> None:
    """The gate opening does not make every row fair game. `SOMEWHERE_ELSE` is never rewritten,
    at any point in the sequence."""
    elsewhere = "https://finance-portal.corp.example/apps/expenses"
    action = decide(
        AppAddresses(APP_ID, platform_url=NEW, recorded_url=elsewhere), apps_base_url=APPS
    )
    assert action.rewrite_recorded_to is None
    assert action.recorded is AddressState.SOMEWHERE_ELSE
