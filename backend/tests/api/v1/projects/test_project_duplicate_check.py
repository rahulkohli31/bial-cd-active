"""The two live endpoints Slice 4 adds (#191 R31-R39): `POST /v1/projects:check-duplicates`
and `POST /v1/projects:duplicate-check-resolved`.

`find_possible_duplicates`'s own branching (the confidence bar, the SQL shape) is already
covered directly against hand-built rows/compiled SQL in `tests/services/projects/` — these
tests are about the HTTP surface: auth, wiring `EmbedderDep` through to the search, the
never-fails posture reaching the response rather than a 500, and the two R39 log events firing.
"""

from __future__ import annotations

import pytest
from pydantic_ai import Embedder
from pydantic_ai.embeddings.test import TestEmbeddingModel
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from src.db.models.app_registry import AppStatus
from src.db.models.deployment import Deployment, DeploymentStatus
from src.db.models.project import DESCRIPTION_EMBEDDING_DIMENSIONS
from src.services.embeddings import embedder_dependency
from tests.api.v1.projects.conftest import _VALID_DESCRIPTION
from tests.api.v1.projects.test_projects_crud import _auth
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory

_CHECK = "/v1/projects:check-duplicates"
_RESOLVED = "/v1/projects:duplicate-check-resolved"


@pytest.fixture
def fake_embedder(app):
    """Same fixture as `test_project_description_embedding.py` — see there for why
    `dimensions=DESCRIPTION_EMBEDDING_DIMENSIONS` is not optional here: the vector arm binds
    this embedding straight into a `<=>` comparison against the real `vector(1536)` column,
    and pgvector rejects a mismatched width even as a query parameter, not only on write."""
    app.dependency_overrides[embedder_dependency] = lambda: Embedder(
        TestEmbeddingModel(dimensions=DESCRIPTION_EMBEDDING_DIMENSIONS)
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(embedder_dependency, None)


async def _live_app(
    db: AsyncSession, *, owner_email: str, name: str, description: str
) -> Deployment:
    """A project + its one app + a SUCCEEDED, has-a-url deployment — the terminal shape
    `live_app_ids()` (and so `find_possible_duplicates`) requires to count an app as live,
    mirroring `test_marketplace.py::_published_app` for the same reason: no Azure anywhere,
    a published app is just a `deployments` row in the shape the deploy pipeline leaves."""
    owner = await UserFactory.create(db, email=owner_email, display_name="Builder Name")
    project = await ProjectFactory.create(db, owner.id, name=name, description=description)
    app_row = await AppRegistryFactory.create(
        db, user_id=owner.id, project_id=project.id, status=AppStatus.APPROVED
    )
    deployment = Deployment(
        app_id=app_row.id,
        user_id=owner.id,
        status=DeploymentStatus.SUCCEEDED,
        image_digest="sha256:" + "ab" * 32,
        url="https://pub-example.azurecontainerapps.io/",
    )
    db.add(deployment)
    await db.flush()
    await db.refresh(deployment)
    return deployment


async def test_requires_auth_401(client) -> None:
    resp = await client.post(_CHECK, json={"description": _VALID_DESCRIPTION})
    assert resp.status_code == 401


async def test_empty_catalog_returns_no_matches(client, db_session) -> None:
    # The default, day-one case (R31's fall-through path): nothing published yet, so the
    # check must answer cleanly rather than error for lack of a catalog to search.
    headers, _ = await _auth(db_session)
    resp = await client.post(_CHECK, headers=headers, json={"description": _VALID_DESCRIPTION})
    assert resp.status_code == 200
    assert resp.json()["matches"] == []


async def test_a_keyword_matching_live_app_is_returned(client, db_session) -> None:
    # No `fake_embedder` override — keyword-only degrade (R20), same as the marketplace's own
    # baseline. The two descriptions share enough distinctive vocabulary
    # (VIP/movement/terminal/supervisor) to clear `_KEYWORD_SOLO_RANK` on their own.
    await _live_app(
        db_session,
        owner_email="builder@example.com",
        name="VIP Movement Tracker",
        description=_VALID_DESCRIPTION,
    )
    await db_session.commit()

    headers, _ = await _auth(db_session)
    resp = await client.post(_CHECK, headers=headers, json={"description": _VALID_DESCRIPTION})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["matches"]) == 1
    match = body["matches"][0]
    assert match["name"] == "VIP Movement Tracker"
    assert match["builderDisplayName"] == "Builder Name"
    assert match["url"] == "https://pub-example.azurecontainerapps.io/"


async def test_a_differently_worded_near_duplicate_is_found_by_the_keyword_arm_alone(
    client, db_session
) -> None:
    # R31 vs blocker #6 (review of #191, agc129, round 2): `_tsquery` used to AND every term
    # in the citizen's own word-bounded description, so it only matched a stored description
    # containing EVERY one of those stemmed words — near-verbatim copy-paste. This app shares
    # SOME but not all of `_VALID_DESCRIPTION`'s vocabulary (VIP, movement, terminal,
    # supervisor, approve/approves) and is otherwise worded differently — exactly the "real
    # second author, different words" case the AND-query left permanently invisible. No
    # `fake_embedder`: this isolates the keyword arm, so a regression back to
    # `websearch_to_tsquery` fails this test even though the vector arm is absent entirely.
    await _live_app(
        db_session,
        owner_email="builder@example.com",
        name="Terminal VIP Log",
        description=(
            "Whenever a VIP movement is planned at any terminal, ground staff record it here "
            "so the duty supervisor can approve it before the visit and everyone downstream "
            "knows who is coming through and when."
        ),
    )
    await db_session.commit()

    headers, _ = await _auth(db_session)
    resp = await client.post(_CHECK, headers=headers, json={"description": _VALID_DESCRIPTION})
    assert resp.status_code == 200
    assert len(resp.json()["matches"]) == 1


async def test_both_arms_agree_on_a_genuine_duplicate_with_an_embedder_configured(
    client, db_session, fake_embedder
) -> None:
    # The `fake_embedder` fixture existed but was never requested by any test in this file
    # (review of #191, agc129, round 2) — wiring it in proves the vector arm and the fixed
    # keyword arm actually combine end to end, not just each in isolation.
    await _live_app(
        db_session,
        owner_email="builder@example.com",
        name="Terminal VIP Log",
        description=(
            "Whenever a VIP movement is planned at any terminal, ground staff record it here "
            "so the duty supervisor can approve it before the visit."
        ),
    )
    await db_session.commit()

    headers, _ = await _auth(db_session)
    resp = await client.post(_CHECK, headers=headers, json={"description": _VALID_DESCRIPTION})
    assert resp.status_code == 200
    assert len(resp.json()["matches"]) == 1


async def test_an_unrelated_live_app_is_not_returned(client, db_session) -> None:
    await _live_app(
        db_session,
        owner_email="builder@example.com",
        name="Cafeteria Menu Poll",
        description=(
            "Staff vote on next week's cafeteria menu from a shortlist. Catering totals the "
            "votes each Friday and posts the winning menu to the break room screens."
        ),
    )
    await db_session.commit()

    headers, _ = await _auth(db_session)
    resp = await client.post(_CHECK, headers=headers, json={"description": _VALID_DESCRIPTION})
    assert resp.status_code == 200
    assert resp.json()["matches"] == []


async def test_own_or_other_owners_live_apps_are_both_searched(client, db_session) -> None:
    # R32: scoped to the LIVE MARKETPLACE SET, not to the caller's own projects — the whole
    # point of the check is to catch a citizen about to rebuild someone else's published app.
    await _live_app(
        db_session,
        owner_email="other-builder@example.com",
        name="VIP Movement Tracker",
        description=_VALID_DESCRIPTION,
    )
    await db_session.commit()

    headers, _ = await _auth(db_session)
    resp = await client.post(_CHECK, headers=headers, json={"description": _VALID_DESCRIPTION})
    assert resp.status_code == 200
    assert len(resp.json()["matches"]) == 1


async def test_a_failed_embed_call_still_returns_200_with_whatever_keyword_finds(
    client, db_session, app
) -> None:
    # R37: a search/embedding failure must never be the reason a citizen cannot proceed.
    class _Boom:
        async def embed_documents(self, *_a, **_k):
            raise RuntimeError("simulated Foundry timeout")

    await _live_app(
        db_session,
        owner_email="builder@example.com",
        name="VIP Movement Tracker",
        description=_VALID_DESCRIPTION,
    )
    await db_session.commit()

    app.dependency_overrides[embedder_dependency] = lambda: _Boom()
    try:
        headers, _ = await _auth(db_session)
        resp = await client.post(_CHECK, headers=headers, json={"description": _VALID_DESCRIPTION})
        assert resp.status_code == 200, resp.text
        # The keyword arm alone still finds the strong lexical match — the embed failure only
        # took the vector arm down with it, not the whole check.
        assert len(resp.json()["matches"]) == 1
    finally:
        app.dependency_overrides.pop(embedder_dependency, None)


async def test_an_empty_catalog_never_calls_the_embedder(client, db_session, app) -> None:
    # Blocker #7 (review of #191, agc129, round 2): day-one production has no published apps
    # at all, and the check used to embed unconditionally before ever asking whether there
    # was anything to search — a Foundry round trip (and its own outage mode) spent ranking
    # nothing. Proven the same way `test_patch_does_not_re_embed_when_the_description_is_
    # unchanged` proves its own "must not be called" claim: an embedder that raises if it is
    # EVER invoked, against a catalog that is genuinely empty.
    class _MustNotBeCalled:
        async def embed_documents(self, *_a, **_k):  # pragma: no cover - the point is it isn't
            raise AssertionError("the embedder must not be called against an empty catalog")

    app.dependency_overrides[embedder_dependency] = lambda: _MustNotBeCalled()
    try:
        headers, _ = await _auth(db_session)
        resp = await client.post(_CHECK, headers=headers, json={"description": _VALID_DESCRIPTION})
        assert resp.status_code == 200, resp.text
        assert resp.json()["matches"] == []
    finally:
        app.dependency_overrides.pop(embedder_dependency, None)


async def test_too_short_a_description_is_rejected_before_any_search(client, db_session) -> None:
    # The same `_clean_description` boundary `ProjectCreate` uses (R31's own note: a request
    # that fails this here is a caller bypassing the form, not a citizen typing).
    headers, _ = await _auth(db_session)
    resp = await client.post(_CHECK, headers=headers, json={"description": "too short"})
    assert resp.status_code == 422


async def test_matches_shown_event_is_logged_with_the_match_count(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    with capture_logs() as logs:
        resp = await client.post(_CHECK, headers=headers, json={"description": _VALID_DESCRIPTION})
    assert resp.status_code == 200
    entry = next(e for e in logs if e["event"] == "duplicate_check_matches_shown")
    assert entry["match_count"] == 0


async def test_resolved_requires_auth_401(client) -> None:
    resp = await client.post(_RESOLVED, json={"resolution": "created_anyway"})
    assert resp.status_code == 401


@pytest.mark.parametrize("resolution", ["opened_existing", "created_anyway"])
async def test_resolved_accepts_both_closed_set_values_and_logs_them(
    client, db_session, resolution
) -> None:
    headers, _ = await _auth(db_session)
    with capture_logs() as logs:
        resp = await client.post(_RESOLVED, headers=headers, json={"resolution": resolution})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    entry = next(e for e in logs if e["event"] == "duplicate_check_resolved")
    assert entry["resolution"] == resolution


async def test_resolved_rejects_a_value_outside_the_closed_set(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post(_RESOLVED, headers=headers, json={"resolution": "ignored_it"})
    assert resp.status_code == 422
