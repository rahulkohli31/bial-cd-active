"""The marketplace catalog and its keyword search (#145).

The load-bearing tests here are the two that no other suite in this repo can have: that a
read DELIBERATELY crosses the ownership boundary, and that crossing it exposes exactly four
fields and nothing else. Every other list is scoped by `user_id` (ADR-0004), so "user B can
see user A's row" is normally a bug; here it is the feature, and the cost of getting the
second half wrong is leaking one citizen's work to another.

No Azure anywhere: a published app is a `deployments` row with a URL, so the catalog is
exercised by inserting rows in the terminal shape the deploy pipeline leaves behind.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_MARKETPLACE = "/v1/marketplace"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _signed_in(db: AsyncSession, email: str) -> dict[str, str]:
    user = await UserFactory.create(db, email=email)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _published_app(
    db: AsyncSession,
    *,
    owner_email: str,
    name: str,
    description: str | None,
    display_name: str = "Some Builder",
    status: DeploymentStatus = DeploymentStatus.SUCCEEDED,
    url: str | None = "https://pub-example.azurecontainerapps.io/",
    unpublished_at: datetime | None = None,
) -> Deployment:
    """A project + its one app + a deployment row in the shape a finished deploy leaves.

    Defaults describe a LIVE app — succeeded, has a URL, not taken down — so each test
    overrides only the one axis it is about, and a reader can see which.
    """
    owner = await UserFactory.create(db, email=owner_email, display_name=display_name)
    project = await ProjectFactory.create(db, owner.id, name=name, description=description)
    app = await AppRegistryFactory.create(db, user_id=owner.id, project_id=project.id)
    row = Deployment(
        app_id=app.id,
        user_id=owner.id,
        status=status,
        image_digest="sha256:" + "ab" * 32,
        url=url,
        unpublished_at=unpublished_at,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def test_a_signed_in_user_sees_an_app_built_by_someone_else(app, client, db_session) -> None:
    """THE POINT OF THE FEATURE, and the first read on this platform that is correct
    BECAUSE it is not owner-scoped.

    Mutation receipt: add `Deployment.user_id == user.id` to `_live_catalog_query`'s
    `where(...)` and this goes red with an empty catalog."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    await _published_app(
        db_session,
        owner_email="builder@rvaiglobal.com",
        name="Runway Inspection Log",
        description="Log runway inspections and defects.",
        display_name="Priya Builder",
    )

    resp = await client.get(_MARKETPLACE, headers=headers)

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [item["name"] for item in items] == ["Runway Inspection Log"]
    assert items[0]["builderDisplayName"] == "Priya Builder"


async def test_the_entry_exposes_four_fields_and_no_more(app, client, db_session) -> None:
    """THE EXPOSURE BOUNDARY, asserted as an EXACT key set rather than a list of absences.

    A `not in` assertion only catches the leaks someone thought to name; comparing the whole
    key set means a column added to `Project`, `Deployment` or `AppRegistry` later fails
    this test instead of silently widening a cross-user response.

    Mutation receipt: add any field to `MarketplaceEntry` (e.g. `app_id: uuid.UUID`) and
    this goes red on the set comparison."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    await _published_app(
        db_session,
        owner_email="builder@rvaiglobal.com",
        name="Gate Roster",
        description="Who is on which gate.",
    )

    resp = await client.get(_MARKETPLACE, headers=headers)

    entry = resp.json()["items"][0]
    assert set(entry) == {"name", "description", "builderDisplayName", "url"}
    # Named explicitly as well as by set: these are the identifiers the rest of the platform
    # is careful never to hand across a user boundary, so their absence is worth stating.
    body = resp.text
    assert "builder@rvaiglobal.com" not in body
    assert "appKey" not in body and "app_key" not in body
    assert "submissionId" not in body and "azureOid" not in body


async def test_an_unpublished_app_leaves_the_catalog(app, client, db_session) -> None:
    """`unpublished_at` is a SECOND AXIS from `status` (#113): a taken-down app keeps its
    `succeeded` status, so a catalog filtering on status alone would keep listing an app an
    admin deliberately pulled.

    Mutation receipt: drop the `unpublished_at.is_(None)` predicate and this goes red."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    await _published_app(
        db_session,
        owner_email="builder@rvaiglobal.com",
        name="Taken Down Tool",
        description="This one was pulled.",
        unpublished_at=datetime.now(UTC),
    )

    resp = await client.get(_MARKETPLACE, headers=headers)

    assert resp.json()["items"] == []


async def test_only_a_succeeded_deploy_with_a_url_is_listed(app, client, db_session) -> None:
    """Membership is DERIVED from a live deployment, not stored: a failed attempt and a
    succeeded one that never produced a URL are both absent, with no flag to unset."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    await _published_app(
        db_session,
        owner_email="a@rvaiglobal.com",
        name="Failed Attempt",
        description="Never made it.",
        status=DeploymentStatus.FAILED,
    )
    await _published_app(
        db_session,
        owner_email="b@rvaiglobal.com",
        name="No Address",
        description="Succeeded but urlless.",
        url=None,
    )

    resp = await client.get(_MARKETPLACE, headers=headers)

    assert resp.json()["items"] == []


async def test_search_ranks_the_best_description_match_first(app, client, db_session) -> None:
    """Relevance, NOT recency — the ordering flips from the unfiltered catalog's.

    BOTH apps match the query, and the STRONGER match is seeded FIRST so it holds the LOWER
    (older) id. That is what makes the assertion discriminating: under `id DESC` the weaker,
    newer app would come first, so a regression to recency ordering fails here instead of
    passing by luck.

    An earlier version of this test seeded the only match last, which `id DESC` also put
    first — it passed under both orderings and pinned nothing.

    Mutation receipt: replace `order_by(rank.desc(), Deployment.id.desc())` with
    `order_by(Deployment.id.desc())` and this goes red on the first item."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    await _published_app(
        db_session,
        owner_email="a@rvaiglobal.com",
        name="Baggage Belt Faults",
        description="Report baggage belt faults. Baggage crews log every baggage jam here.",
    )
    await _published_app(
        db_session,
        owner_email="b@rvaiglobal.com",
        name="Manifest Reconciliation",
        description="Reconcile baggage against the manifest.",
    )

    resp = await client.get(_MARKETPLACE, headers=headers, params={"q": "baggage"})

    items = [item["name"] for item in resp.json()["items"]]
    # Both match; the denser one wins. Asserting the full order, not just the head, so the
    # test also fails if the weaker match stops being returned at all.
    assert items == ["Baggage Belt Faults", "Manifest Reconciliation"]


async def test_an_app_without_a_description_is_unsearchable_but_still_listed(
    app, client, db_session
) -> None:
    """The knowingly-accepted consequence of not generating descriptions (#145): an app with
    none cannot be FOUND by typing, but it is still IN the catalog. Both halves matter — the
    second is what stops a missing description hiding an app entirely."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    await _published_app(
        db_session,
        owner_email="builder@rvaiglobal.com",
        name="Undescribed Tool",
        description=None,
    )

    listed = await client.get(_MARKETPLACE, headers=headers)
    searched = await client.get(_MARKETPLACE, headers=headers, params={"q": "tool"})

    assert [item["name"] for item in listed.json()["items"]] == ["Undescribed Tool"]
    assert searched.json()["items"] == []


async def test_search_honours_quoted_phrases_and_negation(app, client, db_session) -> None:
    """`websearch_to_tsquery`, not `plainto_tsquery` — the reason the issue names it is that
    people type search-box syntax and expect it to work. A plain parser would treat the
    `-belt` below as another term and return the row this asserts is excluded."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    await _published_app(
        db_session,
        owner_email="a@rvaiglobal.com",
        name="Belt Faults",
        description="Report baggage belt faults.",
    )
    await _published_app(
        db_session,
        owner_email="b@rvaiglobal.com",
        name="Baggage Reconciliation",
        description="Reconcile baggage against the manifest.",
    )

    resp = await client.get(_MARKETPLACE, headers=headers, params={"q": "baggage -belt"})

    assert [item["name"] for item in resp.json()["items"]] == ["Baggage Reconciliation"]


async def test_the_unfiltered_catalog_is_newest_first_and_pages(app, client, db_session) -> None:
    """OFFSET pagination, unlike every other list here — page 2 holds the rows page 1 did
    not, and the envelope carries the totals a numbered control needs."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    for n in range(3):
        await _published_app(
            db_session,
            owner_email=f"b{n}@rvaiglobal.com",
            name=f"App {n}",
            description=f"Number {n}.",
        )

    first = (await client.get(_MARKETPLACE, headers=headers, params={"limit": 2})).json()

    assert [item["name"] for item in first["items"]] == ["App 2", "App 1"]
    assert (first["page"], first["pageSize"], first["total"], first["totalPages"]) == (1, 2, 3, 2)

    second = (
        await client.get(_MARKETPLACE, headers=headers, params={"limit": 2, "page": 2})
    ).json()
    assert [item["name"] for item in second["items"]] == ["App 0"]
    assert second["page"] == 2


async def test_the_total_counts_the_filter_not_the_catalog(app, client, db_session) -> None:
    """`total` must describe the CURRENT filter. A total that ignored `q` would render page
    numbers the user can click and find empty, and only at a page boundary.

    Mutation receipt: build the COUNT off `_live_catalog(None)` instead of
    `_live_catalog(search)` and this goes red — total 3, totalPages 2, for one match."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    await _published_app(
        db_session, owner_email="a@rvaiglobal.com", name="Belt", description="baggage belt."
    )
    for n in range(2):
        await _published_app(
            db_session,
            owner_email=f"c{n}@rvaiglobal.com",
            name=f"Other {n}",
            description="catering trolleys.",
        )

    body = (
        await client.get(_MARKETPLACE, headers=headers, params={"q": "baggage", "limit": 2})
    ).json()

    assert [item["name"] for item in body["items"]] == ["Belt"]
    assert body["total"] == 1 and body["totalPages"] == 1


async def test_sort_by_name_is_case_insensitive_and_alphabetical(app, client, db_session) -> None:
    """The browse-order control. Seeded so the alphabetical answer differs from the default
    newest-first one — otherwise the test would pass without the sort being applied at all.

    Mutation receipt: drop the `sort == "name"` branch (falling through to `id DESC`) and
    this goes red."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    # Mixed case on purpose, though note what this does NOT pin: under this database's
    # `en_US.utf8` collation a bare `ORDER BY name` sorts identically to `ORDER BY
    # lower(name)`, so removing the `lower()` leaves this green. What it does pin is that
    # SOME alphabetical ordering is applied at all rather than the default newest-first.
    for name in ("Zebra log", "apple checklist", "Mango tracker"):
        await _published_app(
            db_session,
            owner_email=f"{name.split()[0].lower()}@rvaiglobal.com",
            name=name,
            description="A tool.",
        )

    body = (await client.get(_MARKETPLACE, headers=headers, params={"sort": "name"})).json()

    assert [item["name"] for item in body["items"]] == [
        "apple checklist",
        "Mango tracker",
        "Zebra log",
    ]


async def test_relevance_outranks_sort_while_searching(app, client, db_session) -> None:
    """A search box that returned alphabetical matches instead of good ones is not a search
    box. `sort=name` is honoured while BROWSING and ignored while searching.

    Mutation receipt: reorder the branches so `sort == "name"` wins over the search ordering
    and this goes red."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    await _published_app(
        db_session,
        owner_email="z@rvaiglobal.com",
        name="Zulu Baggage Desk",
        description="Baggage. Baggage everywhere, baggage all day.",
    )
    await _published_app(
        db_session,
        owner_email="a@rvaiglobal.com",
        name="Alpha Manifest",
        description="Reconcile baggage against the manifest.",
    )

    body = (
        await client.get(_MARKETPLACE, headers=headers, params={"q": "baggage", "sort": "name"})
    ).json()

    # Alphabetically "Alpha Manifest" would lead; by relevance the denser one does.
    assert [item["name"] for item in body["items"]][0] == "Zulu Baggage Desk"


async def test_a_page_past_the_end_is_empty_not_an_error(app, client, db_session) -> None:
    """A client can legitimately ask for page 9 while the catalog shrinks underneath it.
    That is a normal race, not something to show the user an error for."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    await _published_app(
        db_session, owner_email="a@rvaiglobal.com", name="Only One", description="Solo."
    )

    body = (await client.get(_MARKETPLACE, headers=headers, params={"page": 9})).json()

    assert body["items"] == []
    assert body["total"] == 1


async def test_anonymous_is_rejected(app, client, db_session) -> None:
    """Authenticated, not public. Dropping the OWNER scope is not the same as dropping the
    AUTH gate, and this is the test that keeps the two apart."""
    await _published_app(
        db_session,
        owner_email="builder@rvaiglobal.com",
        name="Private To The Platform",
        description="Signed-in BIAL users only.",
    )

    resp = await client.get(_MARKETPLACE)

    assert resp.status_code == 401


async def test_a_non_positive_page_is_a_422(app, client, db_session) -> None:
    """Rejected, never silently clamped to page one — the same contract `clean_limit` keeps."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")

    resp = await client.get(_MARKETPLACE, headers=headers, params={"page": 0})

    assert resp.status_code == 422


async def test_an_unrecognized_sort_is_a_422(app, client, db_session) -> None:
    """Never a silent default: a typo'd `sort` that quietly returned newest-first looks to
    the caller like the control simply does not work."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")

    resp = await client.get(_MARKETPLACE, headers=headers, params={"sort": "sideways"})

    assert resp.status_code == 422


async def test_an_over_long_q_is_a_422(app, client, db_session) -> None:
    """The shared `clean_search` bound, so `q` cannot become an abusive scan."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")

    resp = await client.get(_MARKETPLACE, headers=headers, params={"q": "x" * 201})

    assert resp.status_code == 422
