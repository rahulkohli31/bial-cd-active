"""Per-app records API (U5, R22/R24): CRUD/search/distinct under the X-App-Key
chain, app-scoped, quota-bounded, audited. Response envelopes match Express."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import APP_RECORD_COUNT_CAP, AppStatus
from src.db.models.audit import AuditLog
from tests.factories import AppRegistryFactory, UserFactory


async def _approved_app(db: AsyncSession, **overrides: object):
    user = await UserFactory.create(db)
    app = await AppRegistryFactory.create(
        db, user_id=user.id, status=AppStatus.APPROVED, login_required=False, **overrides
    )
    return app, {"X-App-Key": app.app_key}


async def test_create_then_get_and_list_app_scoped(client, db_session) -> None:
    app, headers = await _approved_app(db_session)
    created = await client.post(
        f"/v1/apps/{app.id}/records",
        json={"collection": "people", "data": {"name": "Alice", "age": 30}},
        headers=headers,
    )
    assert created.status_code == 201
    rec = created.json()
    # Bare record projection, camelCase, no leaked internals.
    assert set(rec) == {
        "id",
        "collection",
        "data",
        "createdBy",
        "createdInDraft",
        "createdAt",
        "updatedAt",
    }
    assert rec["data"] == {"name": "Alice", "age": 30}
    assert rec["createdBy"] is None  # anonymous (login not required)
    assert rec["createdInDraft"] is False  # approved app

    got = await client.get(f"/v1/apps/{app.id}/records/{rec['id']}", headers=headers)
    assert got.status_code == 200
    assert got.json()["record"]["id"] == rec["id"]

    listed = await client.get(f"/v1/apps/{app.id}/records", headers=headers)
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()["records"]] == [rec["id"]]


async def test_records_never_cross_app(client, db_session) -> None:
    app_a, headers_a = await _approved_app(db_session)
    app_b, headers_b = await _approved_app(db_session)
    await client.post(f"/v1/apps/{app_a.id}/records", json={"data": {"x": 1}}, headers=headers_a)
    # App B lists its own records — never A's.
    listed_b = await client.get(f"/v1/apps/{app_b.id}/records", headers=headers_b)
    assert listed_b.json()["records"] == []


async def test_search_by_q_and_filter_with_total(client, db_session) -> None:
    app, headers = await _approved_app(db_session)
    for data in (
        {"name": "Alice", "age": 30},
        {"name": "Bob", "age": 25},
        {"name": "Alice", "age": 40},
    ):
        await client.post(f"/v1/apps/{app.id}/records", json={"data": data}, headers=headers)

    # Free-text q matches across fields.
    by_q = await client.get(f"/v1/apps/{app.id}/records/search?q=bob", headers=headers)
    assert by_q.status_code == 200
    assert by_q.json()["total"] == 1
    assert by_q.json()["items"][0]["data"]["name"] == "Bob"

    # Filter JSON: equality on data.<field>.
    import urllib.parse

    flt = urllib.parse.quote('{"name":"Alice"}')
    by_filter = await client.get(f"/v1/apps/{app.id}/records/search?filter={flt}", headers=headers)
    body = by_filter.json()
    assert body["total"] == 2
    assert all(i["data"]["name"] == "Alice" for i in body["items"])


async def test_distinct_returns_unique_values(client, db_session) -> None:
    app, headers = await _approved_app(db_session)
    for name in ("Alice", "Bob", "Alice"):
        await client.post(
            f"/v1/apps/{app.id}/records", json={"data": {"name": name}}, headers=headers
        )
    resp = await client.get(f"/v1/apps/{app.id}/records/distinct?field=name", headers=headers)
    assert resp.status_code == 200
    assert set(resp.json()["values"]) == {"Alice", "Bob"}


async def test_patch_merges_and_delete_removes(client, db_session) -> None:
    app, headers = await _approved_app(db_session)
    created = await client.post(
        f"/v1/apps/{app.id}/records", json={"data": {"a": 1, "b": 2}}, headers=headers
    )
    rid = created.json()["id"]

    patched = await client.patch(
        f"/v1/apps/{app.id}/records/{rid}", json={"data": {"b": 99, "c": 3}}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["record"]["data"] == {"a": 1, "b": 99, "c": 3}  # shallow merge

    deleted = await client.delete(f"/v1/apps/{app.id}/records/{rid}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
    assert (
        await client.get(f"/v1/apps/{app.id}/records/{rid}", headers=headers)
    ).status_code == 404


async def test_record_quota_exceeded_is_rejected(client, db_session) -> None:
    # Seed the app at its record-count ceiling → the next create is refused.
    app, headers = await _approved_app(db_session, data_count=APP_RECORD_COUNT_CAP)
    resp = await client.post(
        f"/v1/apps/{app.id}/records", json={"data": {"x": 1}}, headers=headers
    )
    assert resp.status_code == 413


async def test_reserved_keys_stripped_and_bad_field_rejected(client, db_session) -> None:
    app, headers = await _approved_app(db_session)
    # Reserved keys are silently dropped; a `$`/`.` key is rejected.
    ok = await client.post(
        f"/v1/apps/{app.id}/records",
        json={"data": {"name": "x", "appId": "forged", "bytes": 999}},
        headers=headers,
    )
    assert ok.status_code == 201
    assert ok.json()["data"] == {"name": "x"}

    bad = await client.post(
        f"/v1/apps/{app.id}/records", json={"data": {"$bad": 1}}, headers=headers
    )
    assert bad.status_code == 400
    assert "invalid field name" in bad.json()["error"]["message"]


async def test_writes_are_audited(client, db_session) -> None:
    app, headers = await _approved_app(db_session)
    created = await client.post(
        f"/v1/apps/{app.id}/records", json={"data": {"x": 1}}, headers=headers
    )
    rid = created.json()["id"]
    await client.patch(
        f"/v1/apps/{app.id}/records/{rid}", json={"data": {"y": 2}}, headers=headers
    )
    await client.delete(f"/v1/apps/{app.id}/records/{rid}", headers=headers)

    actions = (
        (
            await db_session.execute(
                sa.select(AuditLog.action).where(
                    AuditLog.resource_type == "record", AuditLog.resource_id == rid
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(actions) == {"create", "update", "delete"}
