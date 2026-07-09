"""POST/GET/DELETE /v1/attachments — validation, magic bytes, quota, owner-scoped
keys, rate limit (U10). Byte-stable with the Express `/api/attachments` contract.
"""

from __future__ import annotations

import base64
import io

from openpyxl import Workbook
from sqlalchemy import select

from src.config import settings
from src.db.models.attachment import Attachment
from src.services.auth.session_jwt import mint_session_jwt
from src.services.extract.office import EXCEL_MEDIA_TYPE, PPTX_MEDIA_TYPE
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds

# Minimal magic-valid bytes for the allowlisted types.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session):
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


# --- upload happy path + download ---------------------------------------------


async def test_upload_image_then_download(client, db_session, fake_storage) -> None:
    headers, user = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": "att_1",
            "name": "shot.png",
            "mediaType": "image/png",
            "base64": _b64(_PNG),
        },
    )
    assert resp.status_code == 201
    att = resp.json()["attachment"]
    assert att["attachmentId"] == "att_1"
    assert att["mediaType"] == "image/png"
    assert att["size"] == len(_PNG)
    assert att["name"] == "shot.png"
    assert att["kind"] == "image"
    assert att["key"].startswith(f"att/{user.id}/")

    # Stored under the owner-scoped key; download returns the exact bytes + sniffed type.
    dl = await client.get("/v1/attachments/att_1", headers=headers)
    assert dl.status_code == 200
    assert dl.content == _PNG
    assert dl.headers["content-type"] == "image/png"
    assert dl.headers["cache-control"] == "private, max-age=3600"


async def test_upload_pdf_is_document_kind(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={"attachmentId": "att_pdf", "mediaType": "application/pdf", "base64": _b64(_PDF)},
    )
    assert resp.status_code == 201
    assert resp.json()["attachment"]["kind"] == "document"


# --- upload validation --------------------------------------------------------


async def test_wrong_magic_rejected(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={"attachmentId": "att_x", "mediaType": "image/png", "base64": _b64(_PDF)},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "error": {"message": "Attachment bytes do not match the declared type image/png."}
    }


async def test_unsupported_type_rejected(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={"attachmentId": "att_x", "mediaType": "image/tiff", "base64": _b64(_PNG)},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message"].startswith("Unsupported attachment type: image/tiff.")


async def test_text_type_rejected(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={"attachmentId": "att_x", "mediaType": "text/plain", "base64": _b64(b"hi")},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Text attachments are sent inline, not uploaded."}}


async def test_invalid_id_rejected(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={"attachmentId": "bad id!", "mediaType": "image/png", "base64": _b64(_PNG)},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Invalid attachment id."}}


async def test_missing_media_type_rejected(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments", headers=headers, json={"attachmentId": "att_x", "base64": _b64(_PNG)}
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "mediaType is required."}}


async def test_over_size_cap_rejected(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (4 * 1024 * 1024)  # just over 4 MB decoded
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={"attachmentId": "att_big", "mediaType": "image/png", "base64": _b64(oversized)},
    )
    assert resp.status_code == 413
    assert resp.json() == {"error": {"message": "Attachment is too large (max 4 MB)."}}


async def test_over_quota_rejected(client, db_session) -> None:
    headers, user = await _auth(db_session)
    # Pre-fill the user's quota to within a few bytes of the 50 MB cap.
    near_cap = 50 * 1024 * 1024 - 4
    db_session.add(
        Attachment(
            user_id=user.id,
            attachment_id="att_existing",
            media_type="image/png",
            name="",
            size=near_cap,
            storage_key=f"att/{user.id}/existing",
        )
    )
    await db_session.flush()
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={"attachmentId": "att_new", "mediaType": "image/png", "base64": _b64(_PNG)},
    )
    assert resp.status_code == 413
    body = resp.json()
    assert body["error"]["code"] == "ATTACHMENT_STORE_FULL"


# --- download / delete ownership ----------------------------------------------


async def test_download_missing_404(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.get("/v1/attachments/att_nope", headers=headers)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Attachment not found."}}


async def test_download_cross_user_denied(client, db_session) -> None:
    headers_a, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers_a,
        json={"attachmentId": "att_shared", "mediaType": "image/png", "base64": _b64(_PNG)},
    )
    assert resp.status_code == 201
    # User B has no attachment with that id — owner-scoped lookup returns 404.
    headers_b, _ = await _auth(db_session)
    resp_b = await client.get("/v1/attachments/att_shared", headers=headers_b)
    assert resp_b.status_code == 404


async def test_delete_removes_object_and_row(client, db_session, fake_storage) -> None:
    headers, user = await _auth(db_session)
    await client.post(
        "/v1/attachments",
        headers=headers,
        json={"attachmentId": "att_del", "mediaType": "image/png", "base64": _b64(_PNG)},
    )
    assert len(fake_storage.objects) == 1

    resp = await client.delete("/v1/attachments/att_del", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert fake_storage.objects == {}
    row = await db_session.scalar(
        select(Attachment).where(
            Attachment.user_id == user.id, Attachment.attachment_id == "att_del"
        )
    )
    assert row is None


async def test_delete_missing_is_idempotent(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.delete("/v1/attachments/att_ghost", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_delete_cross_user_is_noop_and_preserves_owner_data(
    client, db_session, fake_storage
) -> None:
    # Destructive-leak guard: B DELETEing A's attachmentId is a 200 no-op AND must NOT
    # touch A's row or blob — the `_load_owned(db, user.id, …)` scope predicate must hold.
    a_headers, user_a = await _auth(db_session)
    await client.post(
        "/v1/attachments",
        headers=a_headers,
        json={"attachmentId": "att_shared", "mediaType": "image/png", "base64": _b64(_PNG)},
    )
    assert len(fake_storage.objects) == 1

    b_headers, _ = await _auth(db_session)
    resp = await client.delete("/v1/attachments/att_shared", headers=b_headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}  # idempotent no-op — B owns nothing

    # A's blob + row survive untouched (no cross-user destruction).
    assert len(fake_storage.objects) == 1
    row = await db_session.scalar(
        select(Attachment).where(
            Attachment.user_id == user_a.id, Attachment.attachment_id == "att_shared"
        )
    )
    assert row is not None


async def test_deck_upload_disabled_is_501(client, db_session) -> None:
    # Deck is gated off (GOTENBERG_URL unset in test) → 501 envelope (migrated _error).
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": "att_deck",
            "mediaType": PPTX_MEDIA_TYPE,
            "base64": _b64(b"PK\x03\x04"),
        },
    )
    assert resp.status_code == 501
    assert resp.json() == {"error": {"message": "PowerPoint attachments aren't enabled."}}


async def test_malformed_base64_rejected(client, db_session) -> None:
    # Exercises the fixed tuple-except branch in _validate_attachment_bytes (U1).
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={"attachmentId": "att_b64", "mediaType": "image/png", "base64": "a"},
    )
    assert resp.status_code == 400
    assert "do not match the declared type" in resp.json()["error"]["message"]


def test_attachments_openapi_documents_codes() -> None:
    from src.main import create_app

    paths = create_app().openapi()["paths"]
    upload = set(paths["/v1/attachments"]["post"]["responses"])
    assert {"400", "401", "413", "429", "501", "500"} <= upload
    dl = set(paths["/v1/attachments/{attachment_id}"]["get"]["responses"])
    assert {"400", "404", "401", "500"} <= dl
    delete = set(paths["/v1/attachments/{attachment_id}"]["delete"]["responses"])
    assert {"400", "429", "401", "500"} <= delete


# --- rate limit + auth --------------------------------------------------------


async def test_rate_limit_enforced(client, db_session) -> None:
    from src.api.v1.attachments.router import ATTACHMENT_RATE_LIMIT

    headers, _ = await _auth(db_session)
    payload = {"attachmentId": "att_rl", "mediaType": "image/png", "base64": _b64(_PNG)}
    for _ in range(ATTACHMENT_RATE_LIMIT):
        resp = await client.post("/v1/attachments", headers=headers, json=payload)
        assert resp.status_code == 201  # idempotent re-upload of the same id
    blocked = await client.post("/v1/attachments", headers=headers, json=payload)
    assert blocked.status_code == 429
    assert blocked.json() == {
        "error": {"message": "Too many attachment requests. Please slow down."}
    }


# --- office / deck branches (U11) ---------------------------------------------


async def test_office_upload_extracts_markdown(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Numbers"
    sheet.append(["A", "B"])
    sheet.append([1, 2])
    buffer = io.BytesIO()
    workbook.save(buffer)

    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": "att_office",
            "mediaType": EXCEL_MEDIA_TYPE,
            "base64": _b64(buffer.getvalue()),
            "name": "sheet.xlsx",
        },
    )
    assert resp.status_code == 201
    att = resp.json()["attachment"]
    assert att["kind"] == "office"
    assert att["format"] == "excel"
    assert "## Sheet: Numbers" in att["text"]
    assert att["truncated"] is False


async def test_upload_non_string_name_400(client, db_session, fake_storage) -> None:
    # A present wrong-type `name` used to be coerced to "" — the attachment stored nameless
    # and the SPA lost the filename it renders, with no error.
    headers, _ = await _auth(db_session)
    for bad in (123, ["shot.png"], {"n": "x"}):
        resp = await client.post(
            "/v1/attachments",
            headers=headers,
            json={
                "attachmentId": "att_badname",
                "name": bad,
                "mediaType": "image/png",
                "base64": _b64(_PNG),
            },
        )
        assert resp.status_code == 400, bad
        assert resp.json() == {"error": {"message": "name must be a string."}}, bad
    assert fake_storage.objects == {}  # nothing stored


async def test_upload_absent_name_defaults_to_empty(client, db_session) -> None:
    # Absent (and its `null` spelling) keeps the column's defined "" default — name is optional.
    headers, user = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={"attachmentId": "att_noname", "mediaType": "image/png", "base64": _b64(_PNG)},
    )
    assert resp.status_code == 201
    assert resp.json()["attachment"]["name"] == ""
    row = await db_session.scalar(
        select(Attachment).where(
            Attachment.user_id == user.id, Attachment.attachment_id == "att_noname"
        )
    )
    assert row is not None and row.name == ""


async def test_office_upload_non_string_name_400(client, db_session) -> None:
    # The name is parsed ONCE at the boundary, so the office branch is covered by the same check.
    headers, _ = await _auth(db_session)
    workbook = Workbook()
    buffer = io.BytesIO()
    workbook.save(buffer)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": "att_office_badname",
            "mediaType": EXCEL_MEDIA_TYPE,
            "base64": _b64(buffer.getvalue()),
            "name": 42,
        },
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "name must be a string."}}


async def test_office_upload_rejects_corrupt_file(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": "att_bad",
            "mediaType": EXCEL_MEDIA_TYPE,
            "base64": _b64(b"not a real xlsx"),
        },
    )
    assert resp.status_code == 400
    assert "ZIP signature" in resp.json()["error"]["message"]


async def test_deck_upload_disabled_returns_501(client, db_session) -> None:
    # GOTENBERG_URL is unset in the test env → deck attachments are disabled.
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/attachments",
        headers=headers,
        json={"attachmentId": "att_deck", "mediaType": PPTX_MEDIA_TYPE, "base64": _b64(b"x")},
    )
    assert resp.status_code == 501
    assert resp.json() == {"error": {"message": "PowerPoint attachments aren't enabled."}}


async def test_requires_auth(client) -> None:
    assert (await client.get("/v1/attachments/att_1")).status_code == 401
    assert (await client.post("/v1/attachments", json={})).status_code == 401
