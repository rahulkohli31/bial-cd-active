"""POST /v1/admin/apps/backfill-sandbox-tags — C10 identity for containers that predate it (U8).

Everything provisioned from U8 onward is stamped at create. This endpoint is the other half:
the fleet that already exists, which today carries nothing at all and is therefore un-judgeable
the moment Redis is lost. Running it is a release prerequisite — the destroy flag stays off until
the fleet reports zero untagged sandboxes.

THE LOAD-BEARING TEST IN THIS FILE is `test_a_container_matching_no_app_row_gets_no_owner`, with
its mutation-check sibling `test_a_guessed_owner_would_wrongly_become_destroy_eligible`. A sandbox
name keeps only 28 of its app_id's 32 hex characters, so it is not invertible; recovering an owner
means matching FORWARD against the app table, and failing to match means stamping no owner at all.
Filling in a plausible one is the single change that turns "report this to a human" into "delete
somebody's unsaved work", and it is the thing the escalate-never-destroy architecture exists to
prevent.

Shaped after its three siblings (`test_sandbox_reconcile.py`, `test_storage_reconcile.py`,
`test_database_reconcile.py`): the gate, the wire body, the audit row, the failure surface.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import sandbox_or_none_dependency
from src.config import settings
from src.db.models.audit import AuditLog
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.manager import app_name_for
from src.services.sandbox import SandboxError
from src.services.sandbox.base import (
    KIND_BUILD_SANDBOX,
    TAG_APP_ID,
    TAG_BACKFILLED_AT,
    TAG_CONTROL_PLANE,
    TAG_CREATED_AT,
    TAG_KIND,
    TAG_USER_ID,
    identity_from_tags,
)
from tests.factories import AppRegistryFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_BACKFILL = "/v1/admin/apps/backfill-sandbox-tags"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    # The .env.test allowlist contains admin@bial.com → super-admin.
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


class _Fleet:
    """A sandbox client that can list AND stamp — `FleetTagger` by shape, which is all the route
    checks. Deliberately not a `SandboxClient` subclass: the route's runtime `isinstance` check is
    exactly what a future substrate must satisfy, and inheriting the real ABC would stop
    exercising it.

    `stamp_tags` MERGES into the recorded state, like the ARM PATCH it stands for, so the tests
    below observe the tags a container would actually end up carrying rather than the argument
    they were called with."""

    def __init__(
        self,
        fleet: dict[str, dict[str, str]],
        *,
        list_error: Exception | None = None,
        stamp_errors: set[str] | None = None,
    ) -> None:
        self.fleet = fleet
        self.list_error = list_error
        self.stamp_errors = stamp_errors or set()

    async def list_sandbox_app_names(self) -> list[str]:
        return list(self.fleet)

    async def list_sandbox_app_tags(self) -> dict[str, dict[str, str]]:
        if self.list_error is not None:
            raise self.list_error
        return {name: dict(tags) for name, tags in self.fleet.items()}

    async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None:
        if name in self.stamp_errors:
            raise SandboxError("ARM refused the tag PATCH")
        self.fleet.setdefault(name, {}).update(tags)


class _ListerOnly:
    """A deployment whose client can enumerate but cannot stamp — the exact reason `FleetTagger`
    is a separate Protocol from `FleetLister`."""

    async def list_sandbox_app_names(self) -> list[str]:
        return []


def _wire(app, sandbox: object) -> None:
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: sandbox


# --- the gate ---------------------------------------------------------------------


async def test_citizen_is_forbidden(client, app, db_session) -> None:
    _wire(app, _Fleet({}))
    assert (await client.post(_BACKFILL, headers=await _citizen(db_session))).status_code == 403


async def test_unauthenticated_is_401(client, app, db_session) -> None:
    _wire(app, _Fleet({}))
    assert (await client.post(_BACKFILL)).status_code == 401


# --- ownership recovered ----------------------------------------------------------


async def test_an_owned_container_gets_the_whole_identity(client, app, db_session) -> None:
    """The happy path: a container whose name matches an app row comes out judgeable without
    Redis — owner, app, control plane and an age."""
    admin = await _admin(db_session)
    owner = await UserFactory.create(db_session, email="builder@rvaiglobal.com")
    row = await AppRegistryFactory.create(db_session, user_id=owner.id)
    await db_session.commit()
    name = app_name_for(row.id)
    fleet = _Fleet({name: {}})
    _wire(app, fleet)

    body = (await client.post(_BACKFILL, headers=admin)).json()

    assert body == {
        "scanned": 1,
        "alreadyTagged": 0,
        "stamped": 1,
        "skippedNoRow": 0,
        "failed": 0,
        "unowned": 0,
    }
    tags = fleet.fleet[name]
    assert tags[TAG_KIND] == KIND_BUILD_SANDBOX
    assert tags[TAG_USER_ID] == str(owner.id)
    assert tags[TAG_APP_ID] == str(row.id)
    assert tags[TAG_CONTROL_PLANE] == str(settings.ENVIRONMENT)
    assert identity_from_tags(tags).escalate_only is False


async def test_a_backfilled_age_is_marked_synthetic_and_starts_now(
    client, app, db_session
) -> None:
    """R2, at its sharpest. The stamped age is `now` — NOT Azure's `systemData.createdAt`, which
    is the field R2 exists to distrust — so a backfilled container reads as brand new and must
    serve its full tier clock. Erring toward waiting is the whole point: believing a nineteen-day
    Azure timestamp would hand that ghost an instant death sentence on untrustworthy evidence."""
    admin = await _admin(db_session)
    owner = await UserFactory.create(db_session, email="builder@rvaiglobal.com")
    row = await AppRegistryFactory.create(db_session, user_id=owner.id)
    await db_session.commit()
    name = app_name_for(row.id)
    # The fake reports what ARM would: a container created long ago, with no tags. The stamped
    # age must ignore it entirely.
    fleet = _Fleet({name: {}})
    _wire(app, fleet)

    assert (await client.post(_BACKFILL, headers=admin)).status_code == 200

    identity = identity_from_tags(fleet.fleet[name])
    assert identity.was_backfilled is True
    assert identity.created_at is not None
    assert identity.backfilled_at == identity.created_at


# --- ownership NOT recovered: the escalate-forever bucket -------------------------


async def test_a_container_matching_no_app_row_gets_no_owner(client, app, db_session) -> None:
    """THE BINDING RULE. `app_name_for` keeps 28 of 32 hex characters, so a sandbox name does not
    identify its app. When nothing matches, the container is stamped `kind` + `backfilled_at` and
    NOTHING ELSE — no owner, no app, no control plane — which leaves it escalate-forever:
    reported on every pass, destroyed by none of them."""
    admin = await _admin(db_session)
    ghost = app_name_for(uuid.uuid7())  # a well-formed name belonging to no app row
    fleet = _Fleet({ghost: {}})
    _wire(app, fleet)

    body = (await client.post(_BACKFILL, headers=admin)).json()

    assert body["skippedNoRow"] == 1
    assert body["stamped"] == 0
    tags = fleet.fleet[ghost]
    assert set(tags) == {TAG_KIND, TAG_BACKFILLED_AT}
    assert TAG_USER_ID not in tags
    assert TAG_APP_ID not in tags
    assert TAG_CREATED_AT not in tags
    assert identity_from_tags(tags).escalate_only is True


async def test_a_guessed_owner_would_wrongly_become_destroy_eligible(
    client, app, db_session
) -> None:
    """MUTATION CHECK, written as an executable statement of the failure rather than a comment.

    This is what the code above would produce if `_backfill_tags` guessed an owner for an
    unmatched container — the "closest match" temptation. The identity comes out complete, so
    `escalate_only` flips to False and the container becomes destroy-eligible on evidence that is
    a coincidence of 28 truncated hex characters. The assertion below is the alarm: if the real
    backfill ever produces this shape for an unmatched name, the test above goes red."""
    guessed = {
        TAG_KIND: KIND_BUILD_SANDBOX,
        TAG_USER_ID: str(uuid.uuid7()),
        TAG_APP_ID: str(uuid.uuid7()),
        TAG_CONTROL_PLANE: str(settings.ENVIRONMENT),
        TAG_CREATED_AT: "2026-08-11T00:00:00+00:00",
        TAG_BACKFILLED_AT: "2026-08-11T00:00:00+00:00",
    }

    assert identity_from_tags(guessed).escalate_only is False, (
        "a guessed owner makes an unprovable container destroy-eligible — this is exactly what "
        "the backfill must never write for a name that matches no app row"
    )


# --- idempotence ------------------------------------------------------------------


async def test_an_already_tagged_container_is_left_alone(client, app, db_session) -> None:
    """Re-stamping would overwrite a real `bial-created-at` with `now` on every run, resetting the
    age clock of the whole fleet each time an operator pressed the button — reclamation that
    reclaims nothing, forever, with every test still green."""
    admin = await _admin(db_session)
    owner = await UserFactory.create(db_session, email="builder@rvaiglobal.com")
    row = await AppRegistryFactory.create(db_session, user_id=owner.id)
    await db_session.commit()
    name = app_name_for(row.id)
    original = {
        TAG_KIND: KIND_BUILD_SANDBOX,
        TAG_USER_ID: str(owner.id),
        TAG_APP_ID: str(row.id),
        TAG_CONTROL_PLANE: str(settings.ENVIRONMENT),
        TAG_CREATED_AT: "2026-07-01T00:00:00+00:00",
    }
    fleet = _Fleet({name: dict(original)})
    _wire(app, fleet)

    body = (await client.post(_BACKFILL, headers=admin)).json()

    assert body == {
        "scanned": 1,
        "alreadyTagged": 1,
        "stamped": 0,
        "skippedNoRow": 0,
        "failed": 0,
        "unowned": 0,
    }
    assert fleet.fleet[name] == original  # the real age survives, untouched


async def test_a_second_pass_changes_nothing(client, app, db_session) -> None:
    admin = await _admin(db_session)
    owner = await UserFactory.create(db_session, email="builder@rvaiglobal.com")
    row = await AppRegistryFactory.create(db_session, user_id=owner.id)
    await db_session.commit()
    fleet = _Fleet({app_name_for(row.id): {}})
    _wire(app, fleet)

    assert (await client.post(_BACKFILL, headers=admin)).status_code == 200
    after_first = dict(fleet.fleet[app_name_for(row.id)])
    second = (await client.post(_BACKFILL, headers=admin)).json()

    assert second["alreadyTagged"] == 1
    assert second["stamped"] == 0
    assert fleet.fleet[app_name_for(row.id)] == after_first


async def test_the_unowned_population_is_still_reported_on_the_second_pass(
    client, app, db_session
) -> None:
    """THE NUMBER AN OPERATOR NEEDS MUST NOT GO QUIET. A container matching no app row is stamped
    `kind` + `backfilled_at` on pass 1 and counted in `skippedNoRow`. On pass 2 it carries
    `bial-kind`, so it lands in `alreadyTagged` — and `skippedNoRow` drops to zero.

    That is the whole failure: `alreadyTagged == scanned` with every other bucket empty is exactly
    what "the fleet is clean, flip the destroy flag" looks like, and it reads identically for a
    fully-identified fleet and for one made entirely of containers nobody has adjudicated. The
    consequence is safe (they are escalate-only, nothing destroys them) but the operator has lost
    the count C10 §3 says matters most, at the moment they are deciding.

    Mutation-check: count `unowned` only where this pass stamped it, and this goes red on the
    second pass while every other assertion in the file stays green."""
    admin = await _admin(db_session)
    ghost = app_name_for(uuid.uuid7())
    fleet = _Fleet({ghost: {}})
    _wire(app, fleet)

    first = (await client.post(_BACKFILL, headers=admin)).json()
    assert first == {
        "scanned": 1, "alreadyTagged": 0, "stamped": 0,
        "skippedNoRow": 1, "failed": 0, "unowned": 1,
    }  # fmt: skip

    second = (await client.post(_BACKFILL, headers=admin)).json()
    # The four action buckets now say "nothing to do" — correctly, and uselessly.
    assert second["alreadyTagged"] == second["scanned"] == 1
    assert second["skippedNoRow"] == second["stamped"] == second["failed"] == 0
    # ...and this is the one that keeps telling the truth.
    assert second["unowned"] == 1


# --- the buckets sum --------------------------------------------------------------


async def test_the_buckets_account_for_every_container(client, app, db_session) -> None:
    """`scanned == alreadyTagged + stamped + skippedNoRow + failed`. An operator reads this to
    decide whether the fleet is ready for a destructive flag; a report whose numbers do not add
    up cannot support that decision."""
    admin = await _admin(db_session)
    owner = await UserFactory.create(db_session, email="builder@rvaiglobal.com")
    row = await AppRegistryFactory.create(db_session, user_id=owner.id)
    await db_session.commit()
    owned, ghost = app_name_for(row.id), app_name_for(uuid.uuid7())
    tagged, doomed = app_name_for(uuid.uuid7()), app_name_for(uuid.uuid7())
    fleet = _Fleet(
        {owned: {}, ghost: {}, tagged: {TAG_KIND: KIND_BUILD_SANDBOX}, doomed: {}},
        stamp_errors={doomed},
    )
    _wire(app, fleet)

    body = (await client.post(_BACKFILL, headers=admin)).json()

    assert body == {
        "scanned": 4,
        "alreadyTagged": 1,
        "stamped": 1,
        "skippedNoRow": 1,
        "failed": 1,
        # NOT part of the sum: a fleet census, not a record of this pass. Two here — the one
        # this pass stamped kind-only, plus one an EARLIER pass did, which `alreadyTagged`
        # would otherwise have quietly absorbed.
        "unowned": 2,
    }
    assert body["scanned"] == sum(
        body[k] for k in ("alreadyTagged", "stamped", "skippedNoRow", "failed")
    )


async def test_one_refused_patch_does_not_abort_the_pass(client, app, db_session) -> None:
    # The operation is idempotent, so the next run retries the failure. Aborting on the first
    # would leave the fleet part-stamped with no report of what remains.
    admin = await _admin(db_session)
    owner = await UserFactory.create(db_session, email="builder@rvaiglobal.com")
    row = await AppRegistryFactory.create(db_session, user_id=owner.id)
    await db_session.commit()
    ok, broken = app_name_for(row.id), app_name_for(uuid.uuid7())
    fleet = _Fleet({broken: {}, ok: {}}, stamp_errors={broken})
    _wire(app, fleet)

    body = (await client.post(_BACKFILL, headers=admin)).json()

    assert body["failed"] == 1
    assert body["stamped"] == 1
    assert fleet.fleet[ok][TAG_USER_ID] == str(owner.id)  # the pass carried on past the failure


# --- the audit trail --------------------------------------------------------------


async def test_the_audit_row_carries_counts_but_no_names(client, app, db_session) -> None:
    """A sandbox name embeds 28 hex characters of its app's uuid, so a name list in the audit
    trail is a durable inventory of who was running what (`.claude/rules/security.md`). Unlike
    `reconcile-sandboxes` there is no operator action left to take on the names, so they do not
    travel in the response either — failures go to the logs."""
    admin = await _admin(db_session)
    ghost = app_name_for(uuid.uuid7())
    _wire(app, _Fleet({ghost: {}}))

    resp = await client.post(_BACKFILL, headers=admin)

    assert resp.status_code == 200
    assert ghost not in resp.text
    row = await db_session.scalar(
        select(AuditLog).where(AuditLog.action == "sandbox:backfill-tags")
    )
    assert row is not None
    assert row.resource_type == "sandbox"
    assert row.detail == {
        "scanned": 1,
        "alreadyTagged": 0,
        "stamped": 0,
        "skippedNoRow": 1,
        "failed": 0,
        "unowned": 1,
    }
    assert ghost not in str(row.detail)


# --- the failure surface ----------------------------------------------------------


async def test_a_failed_enumeration_is_503_not_a_partial_answer(client, app, db_session) -> None:
    # "Nothing left to stamp" from a half-listed fleet is the exact false green the destroy flag
    # is gated on. Refuse rather than under-report.
    admin = await _admin(db_session)
    _wire(app, _Fleet({}, list_error=SandboxError("arm blip")))

    resp = await client.post(_BACKFILL, headers=admin)

    assert resp.status_code == 503
    assert "try again" in resp.json()["error"]["message"].lower()


async def test_an_unconfigured_sandbox_is_503(client, app, db_session) -> None:
    admin = await _admin(db_session)
    _wire(app, None)
    assert (await client.post(_BACKFILL, headers=admin)).status_code == 503


async def test_a_client_that_cannot_stamp_is_503_not_500(client, app, db_session) -> None:
    # Nothing is wrong with the request — this deployment simply cannot answer it. A client that
    # can list but not stamp lands here, which is why the tagger is its own Protocol.
    admin = await _admin(db_session)
    _wire(app, _ListerOnly())

    resp = await client.post(_BACKFILL, headers=admin)

    assert resp.status_code == 503
    assert resp.status_code != 500
