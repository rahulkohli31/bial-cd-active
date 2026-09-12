"""`shr_name_for` — the shared-runtime sandbox's naming (#198 slice 2), and the tag/shape
guards it has to stay in lockstep with.

Modeled on `tests/services/deploy/test_names.py`, which exists for the same reason: naming
collisions between lineages are exactly the kind of regression a future refactor introduces by
accident, so they get property tests over many ids rather than one hand-picked example.
"""

from __future__ import annotations

import re
import uuid

from src.services.build_sessions import app_name_for, shr_name_for
from src.services.build_sessions.reaper import is_a_sandbox_name, is_a_shared_sandbox_name
from src.services.deploy.names import published_app_name
from src.services.sandbox.base import (
    KIND_SHARED_SANDBOX,
    TAG_APP_ID,
    TAG_CONTROL_PLANE,
    TAG_CREATED_AT,
    TAG_KIND,
    TAG_USER_ID,
    control_plane_segment,
    shared_sandbox_tags,
)

# ACA container-app names: 2–32 chars, lowercase alphanumeric with internal hyphens, must
# start with a letter and end alphanumeric.
_ACA_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_ACA_NAME_MAX = 32


def test_shared_names_are_aca_legal_and_never_collide_with_the_other_two_lineages() -> None:
    for _ in range(1000):
        app_id, recipient_id = uuid.uuid4(), uuid.uuid4()
        shared = shr_name_for(app_id, recipient_id)

        assert shared != app_name_for(app_id)
        assert shared != published_app_name(app_id)
        assert not shared.startswith("sbx-")
        assert not shared.startswith("pub-")
        assert shared.startswith("shr-")
        assert len(shared) <= _ACA_NAME_MAX
        assert _ACA_NAME_RE.fullmatch(shared), shared


def test_the_shared_name_is_exactly_at_the_aca_ceiling() -> None:
    """`shr-` + 28 hex = 32, same budget as the other two lineages. Pinned so the next person
    to widen the digest slice produces names Azure rejects outright."""
    assert len(shr_name_for(uuid.uuid4(), uuid.uuid4())) == _ACA_NAME_MAX


def test_the_shared_name_is_stable_for_the_same_pair_forever() -> None:
    """A recipient relaunching or refreshing their view must find the SAME container back, not
    mint a new one every time — this is what a Launch/Refresh endpoint keys its lookup on."""
    app_id, recipient_id = uuid.uuid4(), uuid.uuid4()
    assert shr_name_for(app_id, recipient_id) == shr_name_for(app_id, recipient_id)


def test_two_recipients_of_the_same_app_get_different_containers() -> None:
    """The whole reason this hashes the PAIR rather than slicing the app id alone: one shared
    app, two colleagues, must mint two containers — one per recipient's own restricted view."""
    app_id = uuid.uuid4()
    a, b = uuid.uuid4(), uuid.uuid4()
    assert shr_name_for(app_id, a) != shr_name_for(app_id, b)


def test_the_same_recipient_of_two_different_apps_gets_different_containers() -> None:
    recipient_id = uuid.uuid4()
    a, b = uuid.uuid4(), uuid.uuid4()
    assert shr_name_for(a, recipient_id) != shr_name_for(b, recipient_id)


def test_names_do_not_collide_over_many_pairs() -> None:
    names = {shr_name_for(uuid.uuid4(), uuid.uuid4()) for _ in range(2000)}
    assert len(names) == 2000


# --- the shape guard ---------------------------------------------------------------


def test_is_a_shared_sandbox_name_accepts_exactly_what_the_minter_produces() -> None:
    for _ in range(200):
        name = shr_name_for(uuid.uuid4(), uuid.uuid4())
        assert is_a_shared_sandbox_name(name)
        # NEVER cross-accepted by the sbx- guard — the two shape checks must stay disjoint,
        # or a fail-closed delete guard is only fail-closed against one of its two lineages.
        assert not is_a_sandbox_name(name)


def test_is_a_shared_sandbox_name_rejects_the_sbx_shape() -> None:
    assert not is_a_shared_sandbox_name(app_name_for(uuid.uuid4()))


def test_is_a_shared_sandbox_name_rejects_near_misses() -> None:
    good = shr_name_for(uuid.uuid4(), uuid.uuid4())
    slug = good[len("shr-") :]
    for bad in (
        f"shr-{slug[:-1]}",  # 27 hex
        f"shr-{slug}a",  # 29 hex
        f"shr-{slug.upper()}",  # uppercase
        f"shr-{slug[:-1]}g",  # non-hex
        "shr-",  # prefix alone
        slug,  # no prefix at all
    ):
        assert not is_a_shared_sandbox_name(bad), bad


# --- the ARM tags --------------------------------------------------------------------


def test_shared_sandbox_tags_name_the_recipient_not_the_app_owner() -> None:
    """#198 KIND_SHARED_SANDBOX's own design note: `TAG_USER_ID` on this kind names the
    RECIPIENT — whose per-slot occupancy this container's lifecycle tracks — never the
    project's owner, who is recoverable from `TAG_APP_ID` via the app row instead."""
    app_id, recipient_id = uuid.uuid4(), uuid.uuid4()
    tags = shared_sandbox_tags(recipient_id=recipient_id, app_id=app_id)

    assert tags[TAG_KIND] == KIND_SHARED_SANDBOX
    assert tags[TAG_USER_ID] == str(recipient_id)
    assert tags[TAG_APP_ID] == str(app_id)
    assert tags[TAG_CONTROL_PLANE] == control_plane_segment()
    assert TAG_CREATED_AT in tags  # the age clock every reclaim tier runs off
