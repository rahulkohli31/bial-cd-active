"""What "live" means on the projects list, and the three numbers above it (#158 §1, §10).

"Live" was settled on the #158 call as a DEPLOYMENT fact:

>   we have live = deployed / published — if the application is published and has url we
>   will show that status

The tempting shortcut is `AppStatus.APPROVED`, and it is wrong: approved means an
administrator said yes, not that anything is serving. `PublishStatusChip` already keeps
`Approved` and `Live` apart, and a list that conflated them would tell a citizen their app
is live when it may never have been deployed.

Two things these tests hold, and the second is the reason the predicate is shared rather
than written twice:

1.  **`isServing` follows the deployment, not the lifecycle.** An approved app that never
    deployed is not live; a draft app that did deploy is.
2.  **The count and the rows cannot disagree.** `in_production` and the per-row flag read
    the same `live_app_ids` collapse, so "3 in production" above a list showing two live
    apps is not expressible. A dashboard that contradicts the list beneath it is worse than
    a wrong number, because the reader cannot tell which half to believe.
"""

from __future__ import annotations

import datetime as dt

from src.db.models.app_registry import AppStatus
from src.db.models.deployment import Deployment, DeploymentStatus
from tests.api.v1.projects.test_projects_crud import _auth
from tests.factories import AppRegistryFactory, ProjectFactory

_PROJECTS = "/v1/projects"
_COUNTS = "/v1/projects/counts"
_URL = "https://app-example.azurecontainerapps.io/"


async def _project_with_app(db, user_id, *, name: str, status: AppStatus = AppStatus.DRAFT):
    project = await ProjectFactory.create(db, user_id, name=name)
    app = await AppRegistryFactory.create(
        db, user_id=user_id, project_id=project.id, status=status
    )
    return project, app


async def _deploy(
    db,
    app,
    user_id,
    *,
    status: DeploymentStatus = DeploymentStatus.SUCCEEDED,
    url: str | None = _URL,
    unpublished_at: dt.datetime | None = None,
) -> Deployment:
    """One deploy ATTEMPT. `deployments` is append-only, so several of these stack up per
    app and liveness is a collapse over them, never a flat read of the newest row."""
    row = Deployment(
        app_id=app.id,
        user_id=user_id,
        status=status,
        image_digest="sha256:" + "cd" * 32,
        url=url,
        unpublished_at=unpublished_at,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def _rows(client, headers) -> dict[str, bool]:
    resp = await client.get(_PROJECTS, headers=headers)
    assert resp.status_code == 200, resp.text
    return {item["name"]: item["isServing"] for item in resp.json()["items"]}


# --- what the flag actually tracks --------------------------------------------


async def test_a_deployed_app_with_a_url_is_live(client, db_session) -> None:
    headers, user = await _auth(db_session)
    _, app = await _project_with_app(db_session, user.id, name="Visitor Log")
    await _deploy(db_session, app, user.id)

    assert (await _rows(client, headers))["Visitor Log"] is True


async def test_an_approved_app_that_never_deployed_is_not_live(client, db_session) -> None:
    """The shortcut this whole module exists to refuse.

    APPROVED means an administrator said yes. Nothing is serving, there is no URL, and a
    status column that showed "Live" here would be claiming something no row supports.
    """
    headers, user = await _auth(db_session)
    await _project_with_app(db_session, user.id, name="Approved Only", status=AppStatus.APPROVED)

    assert (await _rows(client, headers))["Approved Only"] is False


async def test_a_draft_app_that_deployed_is_live_anyway(client, db_session) -> None:
    """The mirror image: one-click deploy never writes `status`, so the ordinary live app
    is still `draft`. Keying off the lifecycle would miss exactly the common case."""
    headers, user = await _auth(db_session)
    _, app = await _project_with_app(db_session, user.id, name="Self Published")
    await _deploy(db_session, app, user.id)

    assert (await _rows(client, headers))["Self Published"] is True


async def test_a_succeeded_deploy_with_no_url_is_not_live(client, db_session) -> None:
    """ "published AND has url" — both halves. A succeeded row with no address is not
    something a citizen can open."""
    headers, user = await _auth(db_session)
    _, app = await _project_with_app(db_session, user.id, name="No Address")
    await _deploy(db_session, app, user.id, url=None)

    assert (await _rows(client, headers))["No Address"] is False


async def test_a_failed_deploy_is_not_live(client, db_session) -> None:
    headers, user = await _auth(db_session)
    _, app = await _project_with_app(db_session, user.id, name="Failed Build")
    await _deploy(db_session, app, user.id, status=DeploymentStatus.FAILED, url=None)

    assert (await _rows(client, headers))["Failed Build"] is False


async def test_a_project_with_no_app_at_all_is_not_live(client, db_session) -> None:
    """And is still LISTED — the liveness join is OUTER for exactly this."""
    headers, user = await _auth(db_session)
    await ProjectFactory.create(db_session, user.id, name="Nothing Built")

    rows = await _rows(client, headers)
    assert rows["Nothing Built"] is False


# --- the collapse: append-only history, not the newest row ---------------------


async def test_an_unpublished_app_stops_being_live(client, db_session) -> None:
    headers, user = await _auth(db_session)
    _, app = await _project_with_app(db_session, user.id, name="Taken Down")
    await _deploy(db_session, app, user.id, unpublished_at=dt.datetime.now(dt.UTC))

    assert (await _rows(client, headers))["Taken Down"] is False


async def test_a_redeploy_after_an_unpublish_is_live_again(client, db_session) -> None:
    """Unpublish then redeploy is the documented recovery path.

    Excluding any app with a takedown anywhere in its history would strand it forever; the
    comparison is "did a takedown land AFTER the row we are calling live", not "is there
    one at all".
    """
    headers, user = await _auth(db_session)
    _, app = await _project_with_app(db_session, user.id, name="Republished")
    await _deploy(db_session, app, user.id, unpublished_at=dt.datetime.now(dt.UTC))
    await _deploy(db_session, app, user.id)  # newer, succeeded, addressable

    assert (await _rows(client, headers))["Republished"] is True


async def test_a_second_takedown_after_a_republish_is_not_live(client, db_session) -> None:
    """The other direction, and the one a naive "newest row wins" read gets wrong."""
    headers, user = await _auth(db_session)
    _, app = await _project_with_app(db_session, user.id, name="Down Again")
    await _deploy(db_session, app, user.id, unpublished_at=dt.datetime.now(dt.UTC))
    await _deploy(db_session, app, user.id)
    await _deploy(db_session, app, user.id, unpublished_at=dt.datetime.now(dt.UTC))

    assert (await _rows(client, headers))["Down Again"] is False


# --- the three numbers ---------------------------------------------------------


async def test_counts_are_owner_scoped(client, db_session) -> None:
    headers, user = await _auth(db_session)
    other_headers, other = await _auth(db_session)
    _, mine = await _project_with_app(db_session, user.id, name="Mine")
    await _deploy(db_session, mine, user.id)
    _, theirs = await _project_with_app(db_session, other.id, name="Theirs")
    await _deploy(db_session, theirs, other.id)

    resp = await client.get(_COUNTS, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"inProduction": 1, "totalApplications": 1, "inPipeline": 0}
    assert (await client.get(_COUNTS, headers=other_headers)).json()["totalApplications"] == 1


async def test_in_production_agrees_with_the_rows_beneath_it(client, db_session) -> None:
    """THE INVARIANT THE SHARED PREDICATE EXISTS FOR.

    A count computed from a different definition than the rows would let the dashboard say
    three while the list shows two, and a reader cannot tell which half to believe. The
    mix below is deliberately awkward: live, approved-but-never-deployed, taken down,
    republished, and nothing built.
    """
    headers, user = await _auth(db_session)

    _, live_one = await _project_with_app(db_session, user.id, name="Live One")
    await _deploy(db_session, live_one, user.id)

    _, republished = await _project_with_app(db_session, user.id, name="Republished")
    await _deploy(db_session, republished, user.id, unpublished_at=dt.datetime.now(dt.UTC))
    await _deploy(db_session, republished, user.id)

    _, approved = await _project_with_app(
        db_session, user.id, name="Approved Only", status=AppStatus.APPROVED
    )
    _, taken_down = await _project_with_app(db_session, user.id, name="Taken Down")
    await _deploy(db_session, taken_down, user.id, unpublished_at=dt.datetime.now(dt.UTC))
    await ProjectFactory.create(db_session, user.id, name="Nothing Built")

    counts = (await client.get(_COUNTS, headers=headers)).json()
    rows = await _rows(client, headers)

    assert counts["inProduction"] == sum(1 for is_live in rows.values() if is_live)
    assert counts["inProduction"] == 2  # Live One + Republished
    # PROJECTS, including the one with nothing built in it — a project IS an application
    # in the product's language, and it exists before anything is built.
    assert counts["totalApplications"] == 5
    # `approved` is counted as in-pipeline ONLY because it is not live; counting it in both
    # would make the three numbers sum to more than the citizen has.
    assert counts["inPipeline"] == 1
    assert approved is not None and taken_down is not None


async def test_counts_is_not_shadowed_by_the_project_id_route(client, db_session) -> None:
    """`/counts` must not be parsed as a project id.

    FastAPI matches in declaration order, so this passes only while `/counts` is declared
    before `/{project_id}`. A reorder would turn it into a 422 on a UUID parse, which is a
    silent break of the dashboard rather than an obvious one.
    """
    headers, _ = await _auth(db_session)

    resp = await client.get(_COUNTS, headers=headers)

    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"inProduction", "totalApplications", "inPipeline"}


async def test_a_disabled_app_is_not_live_even_with_a_standing_deployment(
    client, db_session
) -> None:
    """THE KILL SWITCH MUST WIN OVER THE DEPLOYMENT ROW.

    `disable` transitions the status and SEVERS the app's database — it does not stamp
    `unpublished_at` on the deployment. So an app that was serving keeps a newest-succeeded
    row with a URL and no takedown, and a purely deployment-side liveness predicate calls it
    live: the row renders the green Live badge, `Switched off` becomes unreachable because
    `statusFor` checks serving first, and "In production" counts an app an administrator
    has already killed.

    Liveness is "published AND not withdrawn", and a disable IS a withdrawal — it just
    records it on the registry rather than on the deployment.
    """
    headers, user = await _auth(db_session)
    _, app = await _project_with_app(db_session, user.id, name="Killed", status=AppStatus.DISABLED)
    await _deploy(db_session, app, user.id)  # succeeded, has a URL, never unpublished

    assert (await _rows(client, headers))["Killed"] is False
    assert (await client.get(_COUNTS, headers=headers)).json()["inProduction"] == 0


async def test_a_rejected_app_is_not_live_either(client, db_session) -> None:
    """Same shape, the other lever. `reject` writes `status` and `rejection_standing`
    together; neither touches the deployment row."""
    headers, user = await _auth(db_session)
    _, app = await _project_with_app(
        db_session, user.id, name="Refused", status=AppStatus.REJECTED
    )
    await _deploy(db_session, app, user.id)

    assert (await _rows(client, headers))["Refused"] is False
