"""POST /v1/admin/apps/reconcile-storage — the operator-invoked reconciling sweep (U10).

Superadmin-only, audited, report-only on `submissions/` + `apps/`. The pins that MUST run through
the real upload path live here (`POST /attachments`): a hand-set `storage_key` hides the
PK-vs-key mismatch — `attachment_key(user_id, uuid.uuid7())` mints a fresh uuid7 unrelated to the
PK — so a test built on one goes green against the exact bug (a PK-derived owned-set that would
delete every attachment past grace while its row survives). The prefix mechanics + grace polarity
are pinned service-side in `tests/services/storage/test_reconcile.py`.
"""

from __future__ import annotations

import base64
import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_or_none_dependency
from src.api.v1.attachments.router import storage_dependency as attachment_storage_dependency
from src.config import settings
from src.db.models.attachment import Attachment
from src.db.models.audit import AuditLog
from src.services.auth.session_jwt import mint_session_jwt
from src.services.extract.deck import DeckResult
from src.services.extract.office import PPTX_MEDIA_TYPE
from src.services.storage import StorageError, attachment_key, snapshot_key, submission_key
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds
_RECONCILE = "/v1/admin/apps/reconcile-storage"

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    # The .env.test allowlist contains admin@bial.com → super-admin.
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession):
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


def _wire_shared_storage(app) -> FakeStorage:
    """One in-memory store shared by the attachments UPLOAD path and the admin RECONCILE path, so
    a real `POST /attachments` and the sweep read/write the same objects. The reconcile endpoint
    takes the shared None-tolerant `storage_or_none_dependency`; the upload path keeps its own
    `storage_dependency` — both are overridden to the same instance."""
    store = FakeStorage()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[attachment_storage_dependency] = lambda: store
    return store


def _past_grace() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=48)


async def _upload_image(client, headers: dict[str, str], attachment_id: str):
    return await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": attachment_id,
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )


async def _row(db: AsyncSession, user_id: uuid.UUID, attachment_id: str) -> Attachment:
    att = await db.scalar(
        select(Attachment).where(
            Attachment.user_id == user_id, Attachment.attachment_id == attachment_id
        )
    )
    assert att is not None
    return att


# --- owned attachments survive (real upload path) -------------------------------


async def test_owned_attachment_survives_any_age(client, app, db_session) -> None:
    store = _wire_shared_storage(app)
    citizen, user = await _citizen(db_session)
    admin = await _admin(db_session)

    assert (await _upload_image(client, citizen, "att_owned")).status_code == 201
    att = await _row(db_session, user.id, "att_owned")
    store.mtimes[att.storage_key] = _past_grace()  # age it well past the grace

    resp = await client.post(_RECONCILE, headers=admin)
    assert resp.status_code == 200
    # Owned → never even age-checked → survives, and still downloads.
    assert att.storage_key in store.objects
    assert (await client.get("/v1/attachments/att_owned", headers=citizen)).status_code == 200
    assert resp.json()["attachments"]["deleted"] == 0


async def test_derived_key_regression_pin(client, app, db_session) -> None:
    # The owned-set MUST come from the persisted storage_key, not attachment_key(user, PK). Prove
    # the two differ, age the blob past grace, sweep — and the object survives + still downloads.
    store = _wire_shared_storage(app)
    citizen, user = await _citizen(db_session)
    admin = await _admin(db_session)

    assert (await _upload_image(client, citizen, "att_derived")).status_code == 201
    att = await _row(db_session, user.id, "att_derived")
    # The trap this pins: a PK-derived key matches NO stored object.
    assert att.storage_key != attachment_key(user.id, att.id)
    store.mtimes[att.storage_key] = _past_grace()

    resp = await client.post(_RECONCILE, headers=admin)
    assert resp.status_code == 200
    assert att.storage_key in store.objects
    assert (await client.get("/v1/attachments/att_derived", headers=citizen)).status_code == 200


async def test_many_owned_attachments_yield_zero_eligible(client, app, db_session) -> None:
    # N attachments all via the real upload path, all past grace. A non-zero att eligible/deleted
    # count means the owned-set is being built from a derived (PK) key.
    store = _wire_shared_storage(app)
    citizen, user = await _citizen(db_session)
    admin = await _admin(db_session)

    for i in range(4):
        assert (await _upload_image(client, citizen, f"att_{i}")).status_code == 201
        att = await _row(db_session, user.id, f"att_{i}")
        store.mtimes[att.storage_key] = _past_grace()

    body = (await client.post(_RECONCILE, headers=admin)).json()
    assert body["attachments"]["eligible"] == 0
    assert body["attachments"]["deleted"] == 0


async def test_deck_sibling_survives_via_pptx_path(client, app, db_session, monkeypatch) -> None:
    # A deck upload writes BOTH `{storage_key}` and a derived `{storage_key}.pdf` sibling no column
    # points at. Both must be in the owned-set (via `_blob_keys_for`'s deck branch) or the sweep
    # permanently deletes the only rendered form the Azure-hosted model can read. Deck is
    # Gotenberg-gated (off by default), so the sidecar is mocked to still exercise the REAL upload
    # path that produces the two objects.
    import src.api.v1.attachments.router as att_router

    async def _fake_convert(data, *, name):
        return DeckResult(pdf=b"%PDF-1.4 rendered deck", page_count=1)

    monkeypatch.setattr(att_router, "deck_attachments_enabled", lambda: True)
    monkeypatch.setattr(att_router, "convert_deck_to_pdf", _fake_convert)

    store = _wire_shared_storage(app)
    citizen, user = await _citizen(db_session)
    admin = await _admin(db_session)

    resp = await client.post(
        "/v1/attachments",
        headers=citizen,
        json={"attachmentId": "att_deck", "mediaType": PPTX_MEDIA_TYPE, "base64": _b64(b"pptx")},
    )
    assert resp.status_code == 201
    att = await _row(db_session, user.id, "att_deck")
    pdf_key = att.storage_key + ".pdf"
    assert att.storage_key in store.objects and pdf_key in store.objects
    store.mtimes[att.storage_key] = _past_grace()
    store.mtimes[pdf_key] = _past_grace()

    assert (await client.post(_RECONCILE, headers=admin)).status_code == 200
    assert att.storage_key in store.objects
    # The .pdf sibling SPECIFICALLY is still there and readable.
    assert pdf_key in store.objects
    assert await store.get(pdf_key) == b"%PDF-1.4 rendered deck"


# --- U9: never-sent-upload reclaim folded into the sweep ------------------------


async def test_never_sent_orphan_is_reclaimed_by_the_sweep(client, app, db_session) -> None:
    # U9/U10: a never-sent upload (row intact, referenced by NO sent message, past the 48h window)
    # is reclaimed by the operator sweep. The blob-vs-row pass alone cannot close this — it treats
    # any still-rowed upload as OWNED — so this pins the reclaim fold that now runs in prod.
    store = _wire_shared_storage(app)
    citizen, user = await _citizen(db_session)
    admin = await _admin(db_session)

    assert (await _upload_image(client, citizen, "att_never_sent")).status_code == 201
    att = await _row(db_session, user.id, "att_never_sent")
    storage_key = att.storage_key  # capture before the aging commit (avoid a post-commit reload)
    # Age the ROW past the 48h reclaim window — the endpoint takes no injectable `now`, and the
    # window is checked on `Attachment.created_at`, not the blob mtime.
    att.created_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=72)
    await db_session.commit()

    body = (await client.post(_RECONCILE, headers=admin)).json()
    assert body["attachmentReclaim"]["reclaimed"] == 1
    assert body["attachmentReclaim"]["sweptKeys"] == 1
    assert body["attachmentReclaim"]["freedBytes"] > 0
    # The diff sweep left the still-rowed blob alone; the reclaim pass took the row AND its blob.
    assert body["attachments"]["deleted"] == 0
    assert storage_key not in store.objects
    assert (
        await db_session.scalar(
            select(Attachment).where(Attachment.attachment_id == "att_never_sent")
        )
        is None
    )
    # Tallies land in the audit trail (counts only, security.md).
    row = await db_session.scalar(select(AuditLog).where(AuditLog.action == "storage:reconcile"))
    assert row is not None and row.detail is not None
    assert row.detail["reclaimedAttachments"] == 1


# --- snapshots report + counts (typed body) -------------------------------------


async def test_snapshots_report_body(client, app, db_session) -> None:
    store = _wire_shared_storage(app)
    admin = await _admin(db_session)
    owner = await UserFactory.create(db_session)
    live_app = await AppRegistryFactory.create(db_session, user_id=owner.id)

    now = datetime.datetime.now(datetime.UTC)
    store.objects[snapshot_key(live_app.id)] = b"x"  # owned
    store.mtimes[snapshot_key(live_app.id)] = now - datetime.timedelta(hours=48)
    fresh = snapshot_key(uuid.uuid7())
    store.objects[fresh] = b"x"
    store.mtimes[fresh] = now - datetime.timedelta(hours=1)  # within grace
    stale = snapshot_key(uuid.uuid7())
    store.objects[stale] = b"x"
    store.mtimes[stale] = now - datetime.timedelta(hours=48)  # eligible

    body = (await client.post(_RECONCILE, headers=admin)).json()
    assert body["snapshots"] == {
        "scanned": 3,
        "owned": 1,
        "withinGrace": 1,
        "eligible": 1,
        "deleted": 1,
    }
    assert stale not in store.objects  # the genuine orphan reclaimed


async def test_submissions_reported_never_deleted_body(client, app, db_session) -> None:
    store = _wire_shared_storage(app)
    admin = await _admin(db_session)
    now = datetime.datetime.now(datetime.UTC)

    ownerless = submission_key(uuid.uuid7(), uuid.uuid7())  # app_id resolves to no row
    store.objects[ownerless] = b"x"
    store.mtimes[ownerless] = now - datetime.timedelta(hours=48)

    body = (await client.post(_RECONCILE, headers=admin)).json()
    assert body["submissions"]["deleted"] == 0
    assert body["ownerlessSubmissions"] == 1
    assert ownerless in store.objects  # immutable record — surfaced, never deleted (D7)


async def test_clean_system_body_is_all_zero(client, app, db_session) -> None:
    _wire_shared_storage(app)
    admin = await _admin(db_session)

    body = (await client.post(_RECONCILE, headers=admin)).json()
    # Asserted on the parsed JSON, never on captured log output.
    assert set(body) == {
        "attachments",
        "snapshots",
        "submissions",
        "apps",
        "ownerlessSubmissions",
        "attachmentReclaim",
    }
    for prefix in ("attachments", "snapshots", "submissions", "apps"):
        assert body[prefix] == {
            "scanned": 0,
            "owned": 0,
            "withinGrace": 0,
            "eligible": 0,
            "deleted": 0,
        }
    assert body["ownerlessSubmissions"] == 0
    assert body["attachmentReclaim"] == {"reclaimed": 0, "freedBytes": 0, "sweptKeys": 0}


async def test_response_body_carries_no_key_list(client, app, db_session) -> None:
    # R13 posture: counts only, no storage keys — dumping keys leaks the internal layout.
    store = _wire_shared_storage(app)
    admin = await _admin(db_session)
    stale = snapshot_key(uuid.uuid7())
    store.objects[stale] = b"x"
    store.mtimes[stale] = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=48)

    body = (await client.post(_RECONCILE, headers=admin)).json()
    # Every value in the body is a count (int), never a key string.
    for prefix in ("attachments", "snapshots", "submissions", "apps"):
        assert all(isinstance(v, int) for v in body[prefix].values())
    assert isinstance(body["ownerlessSubmissions"], int)
    # The reclaim summary is counts only too — never a key or a user id.
    assert all(isinstance(v, int) for v in body["attachmentReclaim"].values())


# --- gating + audit + error surface ---------------------------------------------


async def test_citizen_is_forbidden(client, app, db_session) -> None:
    _wire_shared_storage(app)
    citizen, _ = await _citizen(db_session)
    assert (await client.post(_RECONCILE, headers=citizen)).status_code == 403


async def test_unauthenticated_is_401(client, app, db_session) -> None:
    _wire_shared_storage(app)
    assert (await client.post(_RECONCILE)).status_code == 401


async def test_sweep_is_audited(client, app, db_session) -> None:
    store = _wire_shared_storage(app)
    admin = await _admin(db_session)
    stale = snapshot_key(uuid.uuid7())
    store.objects[stale] = b"x"
    store.mtimes[stale] = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=48)

    assert (await client.post(_RECONCILE, headers=admin)).status_code == 200
    row = await db_session.scalar(select(AuditLog).where(AuditLog.action == "storage:reconcile"))
    assert row is not None
    assert row.resource_type == "storage"
    assert row.detail is not None and row.detail["snapshotsDeleted"] == 1


class _ExplodingListStorage(FakeStorage):
    async def list(self, prefix, *, page_size=1000, token=None):
        raise StorageError("list blip", provider="fake", key=prefix)


async def test_storage_error_returns_retryable_503(client, app, db_session) -> None:
    store = _ExplodingListStorage()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    admin = await _admin(db_session)

    resp = await client.post(_RECONCILE, headers=admin)
    assert resp.status_code == 503
    assert "try again" in resp.json()["error"]["message"].lower()


async def test_unconfigured_store_is_503_not_500(client, app, db_session) -> None:
    # FIX 8 regression + the fixture-free store-off baseline (`.claude/rules/testing.md`): with NO
    # store wired, `storage_or_none_dependency` resolves `get_storage()` →
    # StorageUnconfiguredError → None, and the body maps None to the DOCUMENTED 503. An eager
    # `Storage` dependency raised at solve time → an undocumented 500. Deliberately does not touch
    # the accessor singleton.
    from src.services.storage import accessor as _storage_accessor

    _storage_accessor._backend_singleton = None  # store off: no backend configured in .env.test
    admin = await _admin(db_session)

    resp = await client.post(_RECONCILE, headers=admin)
    assert resp.status_code == 503
    assert resp.status_code != 500
    assert "try again" in resp.json()["error"]["message"].lower()


def test_openapi_documents_the_route() -> None:
    from src.main import create_app

    responses = set(create_app().openapi()["paths"][_RECONCILE]["post"]["responses"])
    assert {"200", "503", "401", "403", "500"} <= responses
