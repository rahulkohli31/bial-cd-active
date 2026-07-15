"""Per-app files (U6, R25): two-store integrity under failure, both download paths,
per-app quota, app-scoping. Storage is an in-memory fake swapped in per test."""

from __future__ import annotations

import base64
from datetime import timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_dependency
from src.db.models.app_registry import APP_FILE_COUNT_CAP, AppStatus
from src.main import create_app
from src.services.storage.base import ListPage, ObjectMeta, ObjectStorage
from src.services.storage.errors import StorageError, StorageNotFoundError, StorageSignError
from tests.factories import AppRegistryFactory, UserFactory

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24  # valid PNG magic
_CSV = b"name,age\nAlice,30\n"


class _DictStorage(ObjectStorage):
    """A dict-backed store — enough of the ABC for the file routes. `can_sign`
    controls whether the signed-URL path works or falls back to the byte proxy."""

    def __init__(self, *, can_sign: bool = True, fail_put: bool = False) -> None:
        super().__init__(provider="fake")
        self.objects: dict[str, bytes] = {}
        self.put_keys: list[str] = []  # keys successfully written (before any compensation)
        self.deleted: list[str] = []  # keys the compensating path asked to delete
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


def _use_storage(app, store: _DictStorage) -> _DictStorage:
    app.dependency_overrides[storage_dependency] = lambda: store
    return store


async def _approved_app(db: AsyncSession, **overrides: object):
    user = await UserFactory.create(db)
    app_row = await AppRegistryFactory.create(
        db, user_id=user.id, status=AppStatus.APPROVED, login_required=False, **overrides
    )
    return app_row, {"X-App-Key": app_row.app_key}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def _upload(client, app_id, headers, *, filename, content_type, data: bytes):
    return await client.post(
        f"/v1/apps/{app_id}/files",
        json={"filename": filename, "contentType": content_type, "base64": _b64(data)},
        headers=headers,
    )


# --- happy path: upload → list → metadata → delete -----------------------------


async def test_upload_ready_then_list_and_delete(client, app, db_session) -> None:
    store = _use_storage(app, _DictStorage())
    app_row, headers = await _approved_app(db_session)

    up = await _upload(
        client, app_row.id, headers, filename="a.csv", content_type="text/csv", data=_CSV
    )
    assert up.status_code == 201
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
    assert meta["size"] == len(_CSV)
    fid = meta["fileId"]
    # The blob was written under the app-scoped key.
    assert any(k.startswith(f"apps/{app_row.id}/") for k in store.objects)

    listed = await client.get(f"/v1/apps/{app_row.id}/files", headers=headers)
    assert [f["fileId"] for f in listed.json()["files"]] == [fid]

    got = await client.get(f"/v1/apps/{app_row.id}/files/{fid}", headers=headers)
    assert got.json()["file"]["fileId"] == fid

    deleted = await client.delete(f"/v1/apps/{app_row.id}/files/{fid}", headers=headers)
    assert deleted.json() == {"ok": True}
    assert store.objects == {}  # blob removed


# --- two-store integrity: put failure → no orphan blob -------------------------


async def test_put_failure_leaves_no_orphan_blob(app, db_session) -> None:
    store = _use_storage(app, _DictStorage(fail_put=True))
    app_row, headers = await _approved_app(db_session)
    # A genuine store error surfaces as a 500; the conftest `client` re-raises app
    # exceptions, so use a non-raising transport to observe the response (in prod the
    # client still receives the 500 before the re-raise is logged).
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await _upload(
            c, app_row.id, headers, filename="a.csv", content_type="text/csv", data=_CSV
        )
    assert resp.status_code == 500
    # put never wrote anything, but the compensating path STILL runs (the app-scoped key it
    # tried to delete proves the except-branch executed — not a vacuous empty-store check).
    assert store.objects == {}
    assert len(store.deleted) == 1
    assert store.deleted[0].startswith(f"apps/{app_row.id}/")


async def test_compensating_blob_delete_when_post_put_step_fails(
    app, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two-store compensation the put-fails test can't reach: the blob IS written, then a
    post-put step (the audit insert) fails, so the compensating delete must remove the now-orphaned
    blob. Proves 'no orphan blob' is real integrity, not a vacuum where nothing was ever stored."""

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("audit exploded after the blob was written")

    monkeypatch.setattr("src.api.v1.apps.files_router.append_audit", _boom)
    store = _use_storage(app, _DictStorage())  # put SUCCEEDS
    app_row, headers = await _approved_app(db_session)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await _upload(
            c, app_row.id, headers, filename="a.csv", content_type="text/csv", data=_CSV
        )
    assert resp.status_code == 500
    # The blob was written by put(), then the exact same key was removed by the compensating
    # delete — no orphan left behind.
    assert store.put_keys, "put must have written the blob before the post-put failure"
    assert store.deleted == store.put_keys
    assert store.objects == {}


# --- download paths ------------------------------------------------------------


async def test_signed_url_path(client, app, db_session) -> None:
    _use_storage(app, _DictStorage(can_sign=True))
    app_row, headers = await _approved_app(db_session)
    up = await _upload(
        client, app_row.id, headers, filename="a.csv", content_type="text/csv", data=_CSV
    )
    fid = up.json()["fileId"]
    resp = await client.get(f"/v1/apps/{app_row.id}/files/{fid}/url", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"].startswith("https://fake.local/")
    assert "expiresAt" in body


async def test_signed_url_falls_back_to_proxy_when_store_cannot_sign(
    client, app, db_session
) -> None:
    _use_storage(app, _DictStorage(can_sign=False))
    app_row, headers = await _approved_app(db_session)
    up = await _upload(
        client, app_row.id, headers, filename="a.csv", content_type="text/csv", data=_CSV
    )
    fid = up.json()["fileId"]
    resp = await client.get(f"/v1/apps/{app_row.id}/files/{fid}/url", headers=headers)
    assert resp.status_code == 501
    assert "content endpoint" in resp.json()["error"]["message"]


async def test_byte_proxy_headers_and_image_inline(client, app, db_session) -> None:
    _use_storage(app, _DictStorage())
    app_row, headers = await _approved_app(db_session)
    up = await _upload(
        client, app_row.id, headers, filename="p.png", content_type="image/png", data=_PNG
    )
    fid = up.json()["fileId"]
    resp = await client.get(f"/v1/apps/{app_row.id}/files/{fid}/content", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"] == "default-src 'none'; sandbox"
    # An image is served as its sniffed type, inline.
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.headers["content-disposition"].startswith("inline")


async def test_byte_proxy_non_image_is_attachment(client, app, db_session) -> None:
    _use_storage(app, _DictStorage())
    app_row, headers = await _approved_app(db_session)
    up = await _upload(
        client, app_row.id, headers, filename="a.csv", content_type="text/csv", data=_CSV
    )
    fid = up.json()["fileId"]
    resp = await client.get(f"/v1/apps/{app_row.id}/files/{fid}/content", headers=headers)
    assert resp.headers["content-type"].startswith("application/octet-stream")
    assert resp.headers["content-disposition"].startswith("attachment")


# --- gateway validations + quota + scoping -------------------------------------


async def test_disallowed_type_rejected(client, app, db_session) -> None:
    _use_storage(app, _DictStorage())
    app_row, headers = await _approved_app(db_session)
    resp = await _upload(
        client, app_row.id, headers, filename="x.svg", content_type="image/svg+xml", data=b"<svg/>"
    )
    assert resp.status_code == 400


async def test_magic_byte_mismatch_rejected(client, app, db_session) -> None:
    _use_storage(app, _DictStorage())
    app_row, headers = await _approved_app(db_session)
    # Declared png but the bytes aren't png.
    resp = await _upload(
        client, app_row.id, headers, filename="x.png", content_type="image/png", data=b"not a png"
    )
    assert resp.status_code == 400


async def test_over_quota_rejected(client, app, db_session) -> None:
    _use_storage(app, _DictStorage())
    app_row, headers = await _approved_app(db_session, file_count=APP_FILE_COUNT_CAP)
    resp = await _upload(
        client, app_row.id, headers, filename="a.csv", content_type="text/csv", data=_CSV
    )
    assert resp.status_code == 413
    # Byte-identical structured envelope + machine code (R10).
    assert resp.json() == {
        "error": {
            "message": "This app has reached its file storage limit.",
            "code": "FILE_QUOTA_EXCEEDED",
        }
    }


async def test_disabled_app_key_is_403_envelope(client, db_session) -> None:
    # A disabled app's key is refused through this router's own wiring.
    owner = await UserFactory.create(db_session)
    disabled = await AppRegistryFactory.create(
        db_session, user_id=owner.id, status=AppStatus.DISABLED, login_required=False
    )
    resp = await client.get(
        f"/v1/apps/{disabled.id}/files", headers={"X-App-Key": disabled.app_key}
    )
    assert resp.status_code == 403
    assert resp.json() == {"error": {"message": "This app is not available."}}


async def test_over_limit_is_429_envelope(client, db_session) -> None:
    # Prove the per-app limiter (429) is reachable through this router (fresh bucket).
    app_row, headers = await _approved_app(db_session)
    last = None
    for _ in range(121):
        last = await client.get(f"/v1/apps/{app_row.id}/files", headers=headers)
    assert last is not None
    assert last.status_code == 429
    assert set(last.json()) == {"error"}


def test_files_document_error_codes_in_openapi() -> None:
    paths = create_app().openapi()["paths"]
    upload = set(paths["/v1/apps/{app_id}/files"]["post"]["responses"])
    assert {"400", "401", "403", "404", "413", "429", "500"} <= upload
    url = set(paths["/v1/apps/{app_id}/files/{file_id}/url"]["get"]["responses"])
    assert "501" in url


async def test_files_are_app_scoped(client, app, db_session) -> None:
    _use_storage(app, _DictStorage())
    app_a, headers_a = await _approved_app(db_session)
    app_b, headers_b = await _approved_app(db_session)
    up = await _upload(
        client, app_a.id, headers_a, filename="a.csv", content_type="text/csv", data=_CSV
    )
    fid = up.json()["fileId"]
    # App B cannot read app A's file id.
    cross = await client.get(f"/v1/apps/{app_b.id}/files/{fid}", headers=headers_b)
    assert cross.status_code == 404


@pytest.mark.parametrize("filename", ["../etc/passwd", "a b.csv", "a;b.csv"])
async def test_bad_filename_rejected(client, app, db_session, filename: str) -> None:
    _use_storage(app, _DictStorage())
    app_row, headers = await _approved_app(db_session)
    resp = await _upload(
        client, app_row.id, headers, filename=filename, content_type="text/csv", data=_CSV
    )
    assert resp.status_code == 400
