"""Journey: the generated-app DATA PLANE — the core function of every built app.

A deployed app never talks to the control-plane with a session cookie; it addresses its
OWN records/files API with the app's `X-App-Key` header (the U4 chain). This journey drives
that whole surface end-to-end the way a running generated app does:

  1. seed an APPROVED app + take its `app_key`
  2. records: create → list → search(q) → search(filter) → distinct → get → patch → delete
     (full CRUD round-trip, envelopes + projections must match Express)
  3. files: upload → list → metadata → content (byte proxy) → url (signed) → delete
     (storage swapped for an in-memory fake; blob written under `apps/{app_id}/`)
  4. cross-app ISOLATION: app B's key cannot reach app A's records or files — every
     cross-boundary read fails CLOSED with 404 (ADR-0004: a dropped app_id predicate is a
     cross-app leak).

This is a GREEN baseline: the data plane is a faithful Express port, so every assertion here
is the intended/correct behaviour and the whole journey must PASS. If any row goes red it is a
real regression in the app data plane, not an expected-bug capture.
"""

from __future__ import annotations

import base64
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.apps.files_router import storage_dependency
from src.db.models.app_registry import AppStatus
from src.services.storage.base import ListPage, ObjectMeta, ObjectStorage
from src.services.storage.errors import StorageError, StorageNotFoundError, StorageSignError
from tests.factories import AppRegistryFactory, UserFactory

_CSV = b"name,age\nAlice,30\n"


# --- in-memory object store (lifted verbatim from tests/api/v1/apps/test_files.py) ---


class _DictStorage(ObjectStorage):
    """A dict-backed store — enough of the ABC for the file routes. `can_sign` decides
    whether the signed-URL path works or falls back to the byte proxy."""

    def __init__(self, *, can_sign: bool = True, fail_put: bool = False) -> None:
        super().__init__(provider="fake")
        self.objects: dict[str, bytes] = {}
        self.put_keys: list[str] = []
        self.deleted: list[str] = []
        self._can_sign = can_sign
        self._fail_put = fail_put

    async def put(self, key, data, *, content_type=None, metadata=None):
        if self._fail_put:
            raise StorageError("put boom", provider="fake", key=key)
        self.objects[key] = data
        self.put_keys.append(key)
        return ObjectMeta(
            key=key, size=len(data), content_type=content_type, etag=None, last_modified=None
        )

    async def get(self, key):
        if key not in self.objects:
            raise StorageNotFoundError("missing", provider="fake", key=key)
        return self.objects[key]

    async def head(self, key):
        data = self.objects.get(key)
        return (
            None
            if data is None
            else ObjectMeta(
                key=key, size=len(data), content_type=None, etag=None, last_modified=None
            )
        )

    async def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)

    async def list(self, prefix, *, page_size=1000, token=None):
        return ListPage(
            keys=tuple(k for k in self.objects if k.startswith(prefix)), next_token=None
        )

    async def _signed_read_url_impl(self, key, *, expires_in: timedelta):
        if not self._can_sign:
            raise StorageSignError("no signing", provider="fake", key=key)
        return f"https://fake.local/{key}"

    async def aclose(self):
        return None


async def _approved_app(db: AsyncSession, **overrides: object):
    """Seed an APPROVED app + return (row, {'X-App-Key': key}) — the data-plane auth chain
    is keyed on the app's own publishable key, not a user session cookie."""
    user = await UserFactory.create(db)
    app_row = await AppRegistryFactory.create(
        db, user_id=user.id, status=AppStatus.APPROVED, login_required=False, **overrides
    )
    return app_row, {"X-App-Key": app_row.app_key}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def test_generated_app_data_plane_end_to_end(client, app, db_session) -> None:
    # Files touch blob storage → swap the real Azure store for an in-memory fake up front.
    store = _DictStorage()
    app.dependency_overrides[storage_dependency] = lambda: store

    app_a, key_a = await _approved_app(db_session)

    # ================================================================
    # PHASE 1 — records CRUD round-trip under the X-App-Key
    # ================================================================

    # 1a. create — bare record projection, camelCase, no leaked internals.
    created = await client.post(
        f"/v1/apps/{app_a.id}/records",
        json={"collection": "people", "data": {"name": "Alice", "age": 30}},
        headers=key_a,
    )
    assert created.status_code == 201, created.text
    r1 = created.json()
    assert set(r1) == {
        "id",
        "collection",
        "data",
        "createdBy",
        "createdInDraft",
        "createdAt",
        "updatedAt",
    }
    assert r1["collection"] == "people"
    assert r1["data"] == {"name": "Alice", "age": 30}
    assert r1["createdBy"] is None  # anonymous — login not required
    assert r1["createdInDraft"] is False  # approved app, not a draft write
    r1_id = r1["id"]

    # Seed two more so search/distinct/filter have something to bite on.
    r2_id = (
        await client.post(
            f"/v1/apps/{app_a.id}/records",
            json={"collection": "people", "data": {"name": "Bob", "age": 25}},
            headers=key_a,
        )
    ).json()["id"]
    await client.post(
        f"/v1/apps/{app_a.id}/records",
        json={"collection": "people", "data": {"name": "Alice", "age": 40}},
        headers=key_a,
    )

    # 1b. list — {"records": [...]}, app-scoped; all three present.
    listed = await client.get(f"/v1/apps/{app_a.id}/records", headers=key_a)
    assert listed.status_code == 200
    listed_ids = {r["id"] for r in listed.json()["records"]}
    assert {r1_id, r2_id} <= listed_ids
    assert len(listed.json()["records"]) == 3

    # 1c. search by free-text q — matches across fields, returns {total, items}.
    by_q = await client.get(f"/v1/apps/{app_a.id}/records/search?q=bob", headers=key_a)
    assert by_q.status_code == 200
    assert by_q.json()["total"] == 1
    assert by_q.json()["items"][0]["data"]["name"] == "Bob"

    # 1d. search by filter JSON — equality on data.<field>.
    import urllib.parse

    flt = urllib.parse.quote('{"name":"Alice"}')
    by_filter = await client.get(f"/v1/apps/{app_a.id}/records/search?filter={flt}", headers=key_a)
    assert by_filter.status_code == 200
    body = by_filter.json()
    assert body["total"] == 2
    assert all(i["data"]["name"] == "Alice" for i in body["items"])

    # 1e. distinct — unique scalar values for a field.
    distinct = await client.get(f"/v1/apps/{app_a.id}/records/distinct?field=name", headers=key_a)
    assert distinct.status_code == 200
    assert set(distinct.json()["values"]) == {"Alice", "Bob"}

    # 1f. get one — {"record": {...}}.
    got = await client.get(f"/v1/apps/{app_a.id}/records/{r1_id}", headers=key_a)
    assert got.status_code == 200
    assert got.json()["record"]["id"] == r1_id

    # 1g. patch — SHALLOW merge (last-write-wins on overlapping keys, new keys added).
    patched = await client.patch(
        f"/v1/apps/{app_a.id}/records/{r1_id}",
        json={"data": {"age": 31, "city": "BLR"}},
        headers=key_a,
    )
    assert patched.status_code == 200
    assert patched.json()["record"]["data"] == {"name": "Alice", "age": 31, "city": "BLR"}

    # 1h. delete — {"ok": true}; the row is then gone (404).
    deleted = await client.delete(f"/v1/apps/{app_a.id}/records/{r2_id}", headers=key_a)
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
    assert (
        await client.get(f"/v1/apps/{app_a.id}/records/{r2_id}", headers=key_a)
    ).status_code == 404
    # r1 survives the delete; list is now two.
    after_del = await client.get(f"/v1/apps/{app_a.id}/records", headers=key_a)
    assert len(after_del.json()["records"]) == 2

    # ================================================================
    # PHASE 2 — files upload/list/metadata/content/url/delete
    # ================================================================

    # 2a. upload (base64 JSON) — file projection, blob written under apps/{app_id}/.
    up = await client.post(
        f"/v1/apps/{app_a.id}/files",
        json={"filename": "roster.csv", "contentType": "text/csv", "base64": _b64(_CSV)},
        headers=key_a,
    )
    assert up.status_code == 201, up.text
    meta = up.json()
    assert set(meta) == {
        "fileId",
        "collection",
        "filename",
        "contentType",
        "size",
        "createdBy",
        "createdInDraft",
        "createdAt",
        "updatedAt",
    }
    assert meta["filename"] == "roster.csv"
    assert meta["contentType"] == "text/csv"
    assert meta["size"] == len(_CSV)
    fid = meta["fileId"]
    assert any(k.startswith(f"apps/{app_a.id}/") for k in store.objects)

    # 2b. list — {"files": [...]}.
    files = await client.get(f"/v1/apps/{app_a.id}/files", headers=key_a)
    assert files.status_code == 200
    assert [f["fileId"] for f in files.json()["files"]] == [fid]

    # 2c. metadata — {"file": {...}}.
    fmeta = await client.get(f"/v1/apps/{app_a.id}/files/{fid}", headers=key_a)
    assert fmeta.status_code == 200
    assert fmeta.json()["file"]["fileId"] == fid

    # 2d. content — hardened same-origin byte proxy: raw bytes + nosniff + locked CSP.
    content = await client.get(f"/v1/apps/{app_a.id}/files/{fid}/content", headers=key_a)
    assert content.status_code == 200
    assert content.content == _CSV
    assert content.headers["x-content-type-options"] == "nosniff"
    assert content.headers["content-security-policy"] == "default-src 'none'; sandbox"
    # A non-image is served as an attachment octet-stream (never inline-executed).
    assert content.headers["content-type"].startswith("application/octet-stream")
    assert content.headers["content-disposition"].startswith("attachment")

    # 2e. url — the fake store CAN sign, so the signed-URL path returns a url + expiry.
    signed = await client.get(f"/v1/apps/{app_a.id}/files/{fid}/url", headers=key_a)
    assert signed.status_code == 200
    assert signed.json()["url"].startswith("https://fake.local/")
    assert "expiresAt" in signed.json()

    # ================================================================
    # PHASE 3 — cross-app ISOLATION (fail-closed 404, no cross-app leak)
    # ================================================================

    app_b, key_b = await _approved_app(db_session)

    # 3a. App B's key on App A's records URL → 404 (the appkey-chain URL/key cross-check).
    cross_url = await client.get(f"/v1/apps/{app_a.id}/records/{r1_id}", headers=key_b)
    assert cross_url.status_code == 404
    assert cross_url.json()["error"]["message"]  # non-empty, non-leaking message

    # 3b. App B addressing A's record id under B's OWN app URL → 404 (app-scoped read).
    cross_scoped = await client.get(f"/v1/apps/{app_b.id}/records/{r1_id}", headers=key_b)
    assert cross_scoped.status_code == 404

    # 3c. App B never sees A's rows: its own list is empty.
    b_list = await client.get(f"/v1/apps/{app_b.id}/records", headers=key_b)
    assert b_list.status_code == 200
    assert b_list.json()["records"] == []

    # 3d. Files are app-scoped too: App B cannot read App A's file id.
    cross_file = await client.get(f"/v1/apps/{app_b.id}/files/{fid}", headers=key_b)
    assert cross_file.status_code == 404

    # ================================================================
    # PHASE 4 — close the file lifecycle: delete removes row + blob
    # ================================================================

    fdel = await client.delete(f"/v1/apps/{app_a.id}/files/{fid}", headers=key_a)
    assert fdel.status_code == 200
    assert fdel.json() == {"ok": True}
    assert store.objects == {}  # blob purged, no orphan
    assert (await client.get(f"/v1/apps/{app_a.id}/files/{fid}", headers=key_a)).status_code == 404
