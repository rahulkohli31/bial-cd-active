"""The ARM identity tag schema: what is written, what is read back, and what is
refused at the boundary.

These are pure-function tests with no Azure and no Redis, which is the point: the whole reason
identity lives on the resource is that it must be judgeable when the coordination store is gone,
so the parser has to work from a plain dict and nothing else.

The one scenario worth naming up front is `test_absent_tags_parse_to_no_identity`. On an untagged
container ARM omits the `tags` key entirely — not `{}`, not `null` — and that shape IS the orphan
population. A parser that raised on it would blind the fleet sweep to exactly the containers it
exists to collect.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from src.config import settings
from src.services.sandbox.base import (
    KIND_BUILD_SANDBOX,
    KIND_PUBLISHED_APP,
    MAX_TAG_VALUE_LENGTH,
    TAG_APP_ID,
    TAG_BACKFILLED_AT,
    TAG_CONTROL_PLANE,
    TAG_CREATED_AT,
    TAG_KIND,
    TAG_USER_ID,
    SandboxTagError,
    checked_tags,
    identity_from_tags,
    published_app_tags,
    sandbox_tags,
)

USER = uuid.uuid7()
APP = uuid.uuid7()


# --- what a fresh sandbox carries ------------------------------------------------


def test_a_new_sandbox_carries_the_whole_identity() -> None:
    """The five questions, answerable from the resource alone: what is it, who owns it, which
    app does it serve, which control plane made it, and how old is it."""
    tags = sandbox_tags(user_id=USER, app_id=APP)

    assert tags[TAG_KIND] == KIND_BUILD_SANDBOX
    assert tags[TAG_USER_ID] == str(USER)
    assert tags[TAG_APP_ID] == str(APP)
    assert tags[TAG_CONTROL_PLANE] == str(settings.ENVIRONMENT)
    assert TAG_CREATED_AT in tags


def test_the_values_round_trip_through_the_parser() -> None:
    # Written by one process, read by another with no shared memory — the round trip is the
    # whole contract, so it is asserted rather than assumed from the key names lining up.
    before = dt.datetime.now(dt.UTC)
    identity = identity_from_tags(sandbox_tags(user_id=USER, app_id=APP))

    assert identity.user_id == USER
    assert identity.app_id == APP
    assert identity.control_plane == str(settings.ENVIRONMENT)
    assert identity.created_at is not None
    assert before <= identity.created_at <= dt.datetime.now(dt.UTC)
    assert identity.kind == KIND_BUILD_SANDBOX


def test_a_fresh_sandbox_is_not_marked_backfilled() -> None:
    identity = identity_from_tags(sandbox_tags(user_id=USER, app_id=APP))
    assert identity.backfilled_at is None


# --- a published app is a different animal, and says so --------------------------


def test_a_published_app_is_tagged_as_one() -> None:
    """Today only the `sbx-`/`pub-` name prefix distinguishes these, which is a convention.
    A destructive pass that mistook a citizen's live application for a sandbox would take
    production down, so the distinction becomes a RECORD."""
    tags = published_app_tags(app_id=APP)

    assert tags[TAG_KIND] == KIND_PUBLISHED_APP
    assert tags[TAG_APP_ID] == str(APP)
    assert identity_from_tags(tags).kind == KIND_PUBLISHED_APP


def test_a_published_app_carries_no_creation_stamp() -> None:
    # The publish path is a full PUT on every redeploy, so a creation stamp here would be
    # rewritten each time the citizen ships. An age that resets on publish is not an age.
    assert TAG_CREATED_AT not in published_app_tags(app_id=APP)


# --- the parser, on every shape ARM actually returns ------------------------------


def test_absent_tags_parse_to_no_identity() -> None:
    """THE ORPHAN SHAPE. ARM omits `tags` entirely on an untagged app, and every container that
    predates the tag schema looks exactly like this. It must parse without raising."""
    identity = identity_from_tags(None)

    assert identity.kind is None
    assert identity.user_id is None
    assert identity.app_id is None
    assert identity.created_at is None


def test_an_empty_tag_dict_parses_the_same_way() -> None:
    assert identity_from_tags({}) == identity_from_tags(None)


@pytest.mark.parametrize("junk", ["", "not-a-uuid", "1234", "sbx-abc"])
def test_an_unparseable_owner_reads_as_no_owner_rather_than_raising(junk: str) -> None:
    """An unreadable signal reads as ABSENT rather than raising and taking the whole caller
    down with it. A malformed owner tag means the platform cannot prove who owns the
    container."""
    identity = identity_from_tags({TAG_KIND: KIND_BUILD_SANDBOX, TAG_USER_ID: junk})

    assert identity.user_id is None


@pytest.mark.parametrize("junk", ["", "yesterday", "2026-13-45T99:99:99"])
def test_an_unparseable_timestamp_reads_as_no_age(junk: str) -> None:
    identity = identity_from_tags(
        {TAG_KIND: KIND_BUILD_SANDBOX, TAG_USER_ID: str(USER), TAG_APP_ID: str(APP)}
        | {TAG_CREATED_AT: junk}
    )

    assert identity.created_at is None


def test_a_naive_timestamp_is_read_as_utc() -> None:
    # We only ever write aware ISO strings, but a hand-edited tag from the portal would not be.
    # A naive datetime compared against an aware `now` raises, which would turn one operator's
    # typo into a crashing fleet pass.
    identity = identity_from_tags({TAG_CREATED_AT: "2026-08-01T10:00:00"})

    assert identity.created_at == dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.UTC)


# --- the backfill's own tag shape -------------------------------------------------


def test_a_backfilled_container_with_no_owner_parses_kind_and_backfill_only() -> None:
    """The exact shape the backfill writes for a container matching no app row: kind and a
    backfill marker, and nothing else."""
    identity = identity_from_tags(
        {TAG_KIND: KIND_BUILD_SANDBOX, TAG_BACKFILLED_AT: "2026-08-11T00:00:00+00:00"}
    )

    assert identity.kind == KIND_BUILD_SANDBOX
    assert identity.user_id is None
    assert identity.app_id is None
    assert identity.backfilled_at is not None


# --- the ARM value ceiling, enforced here rather than by a 400 -------------------


def test_a_value_at_the_limit_is_accepted() -> None:
    at_the_line = "x" * MAX_TAG_VALUE_LENGTH
    assert checked_tags({TAG_CONTROL_PLANE: at_the_line})[TAG_CONTROL_PLANE] == at_the_line


def test_a_value_past_the_limit_is_refused_at_the_boundary() -> None:
    """Refused HERE, where the error can name the tag — not by an opaque ARM 400 halfway
    through a container create that has already half-succeeded."""
    with pytest.raises(SandboxTagError) as ei:
        checked_tags({TAG_CONTROL_PLANE: "x" * (MAX_TAG_VALUE_LENGTH + 1)})

    assert TAG_CONTROL_PLANE in str(ei.value)


def test_checked_tags_copies_rather_than_aliasing() -> None:
    # The returned dict goes onto an ARM envelope; a caller mutating its own dict afterwards
    # must not retroactively change what was stamped.
    source = {TAG_KIND: KIND_BUILD_SANDBOX}
    copied = checked_tags(source)
    source[TAG_KIND] = "tampered"

    assert copied[TAG_KIND] == KIND_BUILD_SANDBOX
