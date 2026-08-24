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
from src.db.models.app_registry import AppStatus
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
    app_status: AppStatus | None = None,
) -> Deployment:
    """A project + its one app + a deployment row in the shape a finished deploy leaves.

    Defaults describe a LIVE app — succeeded, has a URL, not taken down — so each test
    overrides only the one axis it is about, and a reader can see which. `app_status` is
    `None` by default (the factory's own `AppStatus.DRAFT`), overridden only by the
    disabled-app test — the catalog otherwise never reads `AppRegistry.status`.
    """
    owner = await UserFactory.create(db, email=owner_email, display_name=display_name)
    project = await ProjectFactory.create(db, owner.id, name=name, description=description)
    registry_overrides = {} if app_status is None else {"status": app_status}
    app = await AppRegistryFactory.create(
        db, user_id=owner.id, project_id=project.id, **registry_overrides
    )
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


async def _redeploy(
    db: AsyncSession,
    prior: Deployment,
    *,
    status: DeploymentStatus = DeploymentStatus.SUCCEEDED,
    url: str | None = "https://pub-example.azurecontainerapps.io/",
    unpublished_at: datetime | None = None,
) -> Deployment:
    """A second (or later) deployment attempt for the SAME app — what an ordinary redeploy
    actually leaves in the append-only `deployments` table.

    `_published_app` mints exactly one row per app, and every one of the original 16 tests
    used it — so no test represented an app that had been redeployed, which is an ordinary
    thing the append-only table explicitly supports. That gap is exactly what let both the
    duplicate-listing and unpublish-bypass bugs ship green (#147 review).
    """
    row = Deployment(
        app_id=prior.app_id,
        user_id=prior.user_id,
        status=status,
        image_digest="sha256:" + "cd" * 32,
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

    Mutation receipt: add `Deployment.user_id == user.id` to `_live_catalog`'s
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


async def test_a_redeployed_app_is_listed_once(app, client, db_session) -> None:
    """The duplicate-listing blocker (#147 review). `deployments` is APPEND-ONLY — a
    redeploy adds a SECOND succeeded row for the same app, it does not replace the first —
    and the URL is identical across both because `published_app_name` derives the
    container name from the immutable `app_id`. Before the collapse in `_live_catalog`,
    this returned two byte-identical cards and a `total` that counted attempts, not apps.

    Mutation receipt: replace the `last_success` DISTINCT ON collapse with a flat filter
    (drop straight to `sa.select(Deployment).where(status==SUCCEEDED, url.is_not(None))`,
    no `.distinct(...)`) and this goes red with two items and `total == 2`."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    first = await _published_app(
        db_session,
        owner_email="builder@rvaiglobal.com",
        name="Redeployed Tool",
        description="Deployed more than once.",
    )
    await _redeploy(db_session, first)

    resp = await client.get(_MARKETPLACE, headers=headers)
    body = resp.json()

    assert [item["name"] for item in body["items"]] == ["Redeployed Tool"]
    assert body["total"] == 1


async def test_unpublish_survives_a_redeploy(app, client, db_session) -> None:
    """The unpublish-bypass blocker (#147 review). Admin unpublish stamps `unpublished_at`
    on the NEWEST deployment row for the app (`deploy/router.py`'s `unpublish`, via
    `store.latest_for_app`) — so on a redeployed app, only the second row carries the
    stamp. A catalog that filtered a flat `unpublished_at IS NULL` across every row would
    resurrect the app through its first, unstamped row.

    Mutation receipt: read `unpublished_at` off the `last_success` collapse's own row
    instead of the separate `newest` collapse, and this goes red with the app still
    listed."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    first = await _published_app(
        db_session,
        owner_email="builder@rvaiglobal.com",
        name="Unpublished After Redeploy",
        description="Taken down on its second deploy.",
    )
    await _redeploy(db_session, first, unpublished_at=datetime.now(UTC))

    resp = await client.get(_MARKETPLACE, headers=headers)

    assert resp.json()["items"] == []


async def test_a_failed_redeploy_does_not_unlist_the_still_serving_app(
    app, client, db_session
) -> None:
    """The design decision the review left explicitly open: a redeploy attempt that
    settles FAILED does not mean the app went dark. The pipeline creates the container app
    before it awaits the new revision, so a failed attempt commonly leaves the PREVIOUS,
    succeeded revision still running and serving — `deploy/service.py`'s own citizen-facing
    copy states this outright: "Your previous version is still running." Collapsing to the
    newest row REGARDLESS of status (the reviewer's literal suggested SQL) would drop this
    app from the catalog on every failed redeploy; collapsing to the newest SUCCEEDED row
    does not.

    Mutation receipt: collapse `last_success` to the newest row overall instead of the
    newest row where `status == SUCCEEDED` (i.e. drop the `.where(status==SUCCEEDED,
    url.is_not(None))` filter before the `.distinct(...)`), and this goes red with an
    empty catalog."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    first = await _published_app(
        db_session,
        owner_email="builder@rvaiglobal.com",
        name="Still Running After A Failed Redeploy",
        description="The old revision is still serving.",
    )
    await _redeploy(db_session, first, status=DeploymentStatus.FAILED, url=None)

    resp = await client.get(_MARKETPLACE, headers=headers)
    body = resp.json()

    assert [item["name"] for item in body["items"]] == ["Still Running After A Failed Redeploy"]
    assert body["total"] == 1


async def test_unpublish_after_a_failed_redeploy_still_removes_the_app(
    app, client, db_session
) -> None:
    """Pins the two-collapse design specifically, not just its two halves separately. A
    naive fix that reads `unpublished_at` off the SUCCEEDED row's own value (rather than
    off the absolute newest row) would pass the previous two tests but fail this one: here
    the admin's unpublish lands on the FAILED redeploy — because `unpublish` always
    targets the newest row, whatever its status — and the succeeded row's own
    `unpublished_at` stays NULL throughout.

    Mutation receipt: read `unpublished_at` from `last_success`'s own row instead of the
    separate `newest` (absolute-newest-row) collapse, and this goes red with the app still
    listed despite the unpublish."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    first = await _published_app(
        db_session,
        owner_email="builder@rvaiglobal.com",
        name="Unpublished Via A Failed Redeploy",
        description="The kill-switch landed on the failed attempt.",
    )
    await _redeploy(
        db_session,
        first,
        status=DeploymentStatus.FAILED,
        url=None,
        unpublished_at=datetime.now(UTC),
    )

    resp = await client.get(_MARKETPLACE, headers=headers)

    assert resp.json()["items"] == []


async def test_a_disabled_app_is_not_advertised(app, client, db_session) -> None:
    """The second blocker (#147 review): `disable` (the admin kill-switch for a
    compromised or data-leaking app) severs the per-app database but writes nothing to
    `deployments` — so a disabled app's deployment row can still read `succeeded`, and the
    old flat filter kept advertising it platform-wide.

    Mutation receipt: drop `AppRegistry.status != AppStatus.DISABLED` from `_live_catalog`
    and this goes red with the app still listed."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")
    await _published_app(
        db_session,
        owner_email="builder@rvaiglobal.com",
        name="Disabled For Cause",
        description="Pulled by an admin for a data-classification hit.",
        app_status=AppStatus.DISABLED,
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


async def test_an_absurdly_large_page_is_a_422_not_a_500(app, client, db_session) -> None:
    """`clean_page` bounds BOTH ends, like `clean_limit`. Without an upper bound,
    `(page - 1) * limit` overflows int64 on its way into asyncpg's OFFSET parameter
    (`DataError: value out of int64 range`), which falls through to the catch-all as a
    500 — contradicting this route's own documented "a page past the end is empty, not an
    error" contract.

    Distinct from `test_a_page_past_the_end_is_empty_not_an_error`, which uses `page=9`:
    that is a legitimate past-the-end read and must stay a 200. This is the failure
    region, nowhere near it.

    Mutation receipt: drop the `<= MAX_PAGE` half of the bound and this goes red with a
    500 instead of a 422."""
    headers = await _signed_in(db_session, "viewer@rvaiglobal.com")

    resp = await client.get(
        _MARKETPLACE, headers=headers, params={"page": 99999999999999999999, "limit": 100}
    )

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
