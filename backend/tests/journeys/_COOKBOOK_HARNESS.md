# Journey-test harness cookbook

Copy-pasteable recipes for authoring **end-to-end journey tests** (whole flows across
domains) under `backend/tests/journeys/`. Every snippet below is lifted from a real,
passing test in `backend/tests/` — file citations inline. `asyncio_mode="auto"`, so
every test is a plain `async def test_...`, no `@pytest.mark.asyncio` decorator.

Run: `cd backend && uv run pytest tests/journeys`.

---

## 0. The base fixtures (from `tests/conftest.py`)

You get these for free, no imports — they are session/function fixtures in the root
`conftest.py`:

| fixture | scope | what it is |
|---|---|---|
| `db_session` | function | `AsyncSession` bound to a **connection-level transaction that is rolled back** after the test. Nothing you write persists across tests. |
| `app` | function | a fresh `create_app()` FastAPI instance with `get_db` **already overridden** to yield `db_session`. Add your own `app.dependency_overrides[...]` on top. |
| `client` | function | `httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")`. **Re-raises app exceptions** (a 500 in the endpoint bubbles up as a test error — see §9 for the non-raising variant). |

Key facts an author must internalise:
- The global engine is rebound to `NullPool` in `conftest.py` **before import**, and the
  test DB name must contain `"test"` or the suite refuses to boot (`tests/conftest.py:29-35`).
- `get_db` is overridden in the `app` fixture (`tests/conftest.py:76-79`) — your
  endpoint and your test share **one** `db_session`, so after a request you can assert
  directly against the DB with that same session (no commit needed; the request path
  flushes).
- The client origin host is `http://test` — this is the string that shows up in
  runner/frame CSP `connect-src` (`tests/api/v1/apps/test_runner.py:20`).

---

## 1. Auth cookie helper (mint a session, become a user)

Every authenticated request carries a `Cookie: session=<jwt>` header. The JWT is minted
directly (no OIDC round-trip) with `mint_session_jwt(user_id, token_version, ttl)`.
Pattern is identical across `test_lifecycle.py`, `test_conversations.py`,
`test_runner.py`, `test_chat_stream.py`:

```python
from src.config import settings
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth_user(db, **overrides):
    """Create a user + return (user, cookie-headers). Pass email=... to control identity."""
    user = await UserFactory.create(db, **overrides)
    return user, _cookie(mint_session_jwt(user.id, user.token_version, _TTL))
```

Usage:

```python
user, headers = await _auth_user(db_session)
resp = await client.get("/v1/conversations", headers=headers)
```

An unauthenticated request (no cookie) gets **401** on every gated route.

### Becoming a SUPERADMIN

There is **no role column**. Superadmin is decided by an email allowlist in config.
`.env.test` sets `SUPERADMIN_EMAILS=admin@bial.com,superadmin@bial.com`
(`.env.test:7`). So a user created with one of those emails **is** an admin — that is
the entire trick (`tests/api/v1/admin/test_apps_governance.py:64-72`):

```python
async def _admin(db):
    user = await UserFactory.create(db, email="admin@bial.com")   # → super-admin
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))

async def _citizen(db):
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")  # → plain citizen
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))
```

A citizen hitting an `/v1/admin/...` route gets **403**; unauthenticated gets **401**.

---

## 2. Factories (`tests/factories.py`)

All accept a `db` (the `AsyncSession`) as the first positional arg to `.create()`, add +
flush + refresh (server defaults like the UUIDv7 id and `token_version` are populated).
`.build()` returns an unpersisted instance.

```python
from tests.factories import (
    UserFactory, AppRegistryFactory, ConversationFactory, MessageFactory,
)
```

**`UserFactory.create(db, **overrides)`** — defaults `azure_oid=f"oid-{uuid4}"`,
`email="citizen@rvaiglobal.com"`. Override `email=` to steer identity (admin vs citizen,
or two distinct users for cross-user isolation).

**`AppRegistryFactory.create(db, *, user_id, **overrides)`** — `user_id` is **required**
(the ownership boundary). Defaults: a freshly-minted `app_key` (via `mint_app_key()`,
prefix `bial_`), a random `conversation_id`, `status=AppStatus.DRAFT`, `name=""`. Common
overrides for journeys: `status=AppStatus.APPROVED`, `login_required=False`,
`approved_snapshot={...}`, `source_snapshot={...}`, and quota seeds `data_count=`,
`file_count=`.

**`ConversationFactory.create(db, user_id, **overrides)`** — `user_id` required (2nd
positional). Defaults: `id=uuid4()` (client-minted, v4), `kind=ConversationKind.PLANNING`.
Override `title=`, `kind=ConversationKind.BUILDER`, `updated_at=` (for ordering tests).

**`MessageFactory.create(db, user_id, conversation_id, **overrides)`** — both ids
required. Defaults: `id=uuid4()`, `role=MessageRole.USER`, `seq=0`,
`parts=[{"type": "text", "text": "hi"}]`. Override `seq=`, `role=MessageRole.ASSISTANT`,
`parts=[...]`.

Enums to import when overriding:

```python
from src.db.models.app_registry import AppStatus       # DRAFT/PENDING/APPROVED/DISABLED/REJECTED
from src.db.models.conversation import ConversationKind # PLANNING/ASSISTANT/BUILDER
from src.db.models.message import MessageRole           # USER/ASSISTANT
```

---

## 3. The app lifecycle chain: provision → submit → approve → serve

This is the spine of most journeys. Verbatim request/response shapes below.

### 3a. provision (owner cookie) — `POST /v1/apps/provision`

Request body: `{"conversationId": "<uuid-str>"}`. Response **201**
(`tests/api/v1/apps/test_lifecycle.py:38-51`):

```python
resp = await client.post(
    "/v1/apps/provision", json={"conversationId": str(uuid.uuid4())}, headers=headers
)
assert resp.status_code == 201
body = resp.json()
# body == {"appId": "<uuid>", "appKey": "bial_...", "status": "draft", "loginRequired": False}
app_id = body["appId"]
app_key = body["appKey"]   # a secure token, prefix "bial_" — NOT a raw UUID
```

Idempotent per conversation: a second provision with the **same** `conversationId`
returns the same `appId` + same `appKey` (`test_lifecycle.py:53-64`).

### 3b. submit (owner cookie) — `POST /v1/apps/{app_id}/submit`

`SubmitRequest` needs **`source`, `compiled`, `entry`**. A **valid `compiled`** is any
non-empty, ≤2 MiB **string** — the server never runs Babel, it only checks presence/shape
(`src/services/appserving/artifact.py:validate_artifact`). The canonical fixture
(`test_lifecycle.py:31-35`):

```python
_VALID_SUBMIT = {
    "source": "export default function PreviewApp(){ return <div>hi</div>; }",
    "entry": "PreviewApp",
    "compiled": "var PreviewApp = () => React.createElement('div', null, 'hi');",
}

resp = await client.post(f"/v1/apps/{app_id}/submit", json=_VALID_SUBMIT, headers=headers)
assert resp.status_code == 200
assert resp.json() == {"appId": app_id, "status": "pending"}
```

After submit, the row stores `source_snapshot` with the `source`→`src` rename, an `entry`
default, and the client `compiled` verbatim (`test_lifecycle.py:80-84`):
`app.source_snapshot == {"src": <source>, "entry": "PreviewApp", "compiled": <compiled>}`.

Rejections (both **400**): blank source → message
`"Nothing to submit — generate an app first."`; empty `compiled` → an `{"error": {...}}`
body. Unknown app → **404**. No cookie → **401**.

### 3c. approve (ADMIN cookie) — `POST /v1/admin/apps/{app_id}/approve`

Approve requires the app be **PENDING** with a submitted snapshot. It copies the client
artifact into `approved_snapshot` — **no server compile** (`test_apps_governance.py:107-120`):

```python
admin_headers = await _admin(db_session)
resp = await client.post(f"/v1/admin/apps/{app_id}/approve", headers=admin_headers)
assert resp.status_code == 200
assert resp.json() == {"appId": app_id, "status": "approved"}
# fresh.approved_snapshot["compiled"] == the submitted compiled; fresh.approved_by is set
```

Guards: approve a non-pending app → **409**; approve a pending app with
`source_snapshot=None` → **400**.

### 3d. the other governance transitions (ADMIN cookie)

All take the admin cookie; all return `{"appId": ..., "status": ...}` (or `{"ok": True}`
for delete) (`test_apps_governance.py:139-175`):

| call | body | success |
|---|---|---|
| `POST /v1/admin/apps/{id}/reject` | `{"note": "no good"}` | `status": "rejected"`, stores `rejection_note` |
| `POST /v1/admin/apps/{id}/disable` | — | `status": "disabled"` (requires APPROVED, else **409**) |
| `POST /v1/admin/apps/{id}/enable` | — | `status": "approved"` (requires DISABLED, else **409**) |
| `PATCH /v1/admin/apps/{id}` | `{"name": ...}` / `{"loginRequired": true}` | name-only is **not** audited; loginRequired flip **is** (`config:loginRequired`) |
| `GET /v1/admin/apps?status=approved` | — | `{"apps": [{"appId","status","hasApprovedSnapshot",...}]}` — never leaks `appKey`/`approvedSnapshot` |
| `DELETE /v1/admin/apps/{id}` | — | `{"ok": True}` — CASCADE purges records+files, audited `app:delete` (needs a storage override, §6) |

### 3e. shortcut: seed an already-approved app (skip the chain)

When a journey only needs an approved app to exercise the data-plane or runner, seed it
directly through the factory instead of driving provision→submit→approve
(`test_runner.py:32-38`, `test_records.py:14-20`):

```python
_COMPILED = "var PreviewApp=()=>React.createElement('div',null,'live');"

async def _approved_app(db, **overrides):
    user = await UserFactory.create(db)
    app = await AppRegistryFactory.create(
        db, user_id=user.id, status=AppStatus.APPROVED, login_required=False,
        approved_snapshot={"compiled": _COMPILED, "src": "x", "entry": "PreviewApp"},
        **overrides,
    )
    return app, {"X-App-Key": app.app_key}
```

---

## 4. Data-plane calls with `X-App-Key`

The per-app records/files API is authed by the app's own key in an **`X-App-Key`** header
(not a session cookie). Get the key from `app.app_key` (factory) or the provision response.

```python
app, headers = await _approved_app(db_session)   # headers == {"X-App-Key": app.app_key}
```

### 4a. records — `/v1/apps/{app_id}/records`  (`test_records.py`)

| verb + path | request | response |
|---|---|---|
| `POST /records` | `{"collection": "people", "data": {"name": "Alice", "age": 30}}` (collection optional) | **201**, bare record: keys `{id, collection, data, createdBy, createdInDraft, createdAt, updatedAt}`. `createdBy` is `None` when login not required. |
| `GET /records` | — | `{"records": [ {record}, ... ]}` |
| `GET /records/{id}` | — | `{"record": {record}}` (missing → **404**) |
| `GET /records/search?q=bob` | free-text across fields | `{"total": N, "items": [{record}]}` |
| `GET /records/search?filter=<url-encoded JSON>` | e.g. `urllib.parse.quote('{"name":"Alice"}')` — equality on `data.<field>` | `{"total": N, "items": [...]}` |
| `GET /records/distinct?field=name` | — | `{"values": [...]}` (unique) |
| `PATCH /records/{id}` | `{"data": {"b": 99, "c": 3}}` — **shallow merge** | **200** `{"record": {...merged data...}}` |
| `DELETE /records/{id}` | — | **200** `{"ok": True}` (then GET → 404) |

Guards: reserved keys (`appId`, `bytes`, ...) are silently stripped; a `$`/`.` field key →
**400** `"invalid field name..."`; over `APP_RECORD_COUNT_CAP` → **413**; records never
cross apps (B lists only B's). Writes audit `create`/`update`/`delete` under
`resource_type="record"`, `resource_id=<record id>` (`test_records.py:147-169`).

### 4b. files — `/v1/apps/{app_id}/files`  (`test_files.py`)

Files need a **storage override** (§6). Upload is base64 JSON:

```python
import base64

async def _upload(client, app_id, headers, *, filename, content_type, data: bytes):
    return await client.post(
        f"/v1/apps/{app_id}/files",
        json={"filename": filename, "contentType": content_type,
              "base64": base64.b64encode(data).decode()},
        headers=headers,
    )
```

| verb + path | response |
|---|---|
| `POST /files` | **201**, keys `{fileId, collection, filename, contentType, size, createdBy, createdInDraft, createdAt, updatedAt}`; blob written under `apps/{app_id}/...` |
| `GET /files` | `{"files": [{fileId, ...}]}` |
| `GET /files/{id}` | `{"file": {fileId, ...}}` (cross-app → **404**) |
| `GET /files/{id}/url` | **200** `{"url": "https://...", "expiresAt": ...}` when the store can sign; **501** (`"content endpoint"` msg) when it can't |
| `GET /files/{id}/content` | raw bytes + `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`; images `inline`, others `attachment application/octet-stream` |
| `DELETE /files/{id}` | `{"ok": True}`, blob removed |

Guards: disallowed type (svg) → **400**; magic-byte mismatch (declared png, bytes aren't)
→ **400**; over `APP_FILE_COUNT_CAP` → **413** `{"error": {"code": "FILE_QUOTA_EXCEEDED"}}`;
bad filenames (`../etc/passwd`, spaces, `;`) → **400**.

---

## 5. Runner / frame render assertion (proving the compiled artifact is SERVED)

The runner routes are mounted **outside** `/v1`, at `/apps` and `/preview`, and are gated
purely by app **status** (no cookie on shell/frame). Serveable = status ∈
{APPROVED, PENDING} **and** `approved_snapshot.compiled` is a string
(`src/api/v1/apps/runner.py:55-63`).

- `GET /apps/{id}` → the **shell** HTML (same-origin host page). Embeds a JSON `config`
  with `appId` + `appKey`; injects them into an iframe sandboxed
  `allow-scripts allow-forms allow-downloads` (**no** `allow-same-origin`). CSP header ==
  `build_shell_csp()`, `X-Frame-Options: SAMEORIGIN`.
- `GET /apps/{id}/frame` → the **frame** HTML (opaque-origin sandbox) that **embeds the
  approved `compiled` artifact verbatim** inside a React IIFE. This is where you prove the
  artifact is rendered.

**The load-bearing assertion — the compiled string appears in the frame body**
(`test_runner.py:97-111`):

```python
_COMPILED = "var PreviewApp=()=>React.createElement('div',null,'live');"

app = await _approved_app(db_session)          # approved_snapshot.compiled == _COMPILED
resp = await client.get(f"/apps/{app.id}/frame")
assert resp.status_code == 200
assert _COMPILED in resp.text                  # ← the artifact is SERVED into the frame
assert resp.headers["content-security-policy"] == build_frame_csp("http://test")
```

Shell-level proof the config (appKey/appId) is injected (`test_runner.py:44-56`):

```python
resp = await client.get(f"/apps/{app.id}")
assert resp.status_code == 200
body = resp.text
assert app.app_key in body
assert str(app.id) in body
assert "allow-scripts allow-forms allow-downloads" in body
assert "allow-same-origin" not in body
```

Not-served cases → **404** with body containing `"not available"`: a DISABLED app, or a
PENDING/anything with `approved_snapshot=None` (`test_runner.py:58-72`). A PENDING app that
still carries a **prior** approved snapshot **is** served (200) — re-submit keeps the old
app live (`test_runner.py:75-83`).

Runner-token mint (`POST /apps/{id}/runner-token`, cookie-authed) returns
`{"accessToken", "user"}`, never a refresh cookie; no cookie → **401**; unknown app →
**404** (`test_runner.py:151-188`).

CSP builders to import for byte-exact header assertions:

```python
from src.services.appserving.csp import build_frame_csp, build_preview_csp, build_shell_csp
# build_frame_csp(origin) / build_preview_csp(origin) take the request origin "http://test"
```

---

## 6. Swapping in a fake object store (files, hard-delete, attachment sweep)

Any route that touches blob storage must have its storage dependency overridden with an
in-memory fake, or it will reach for real Azure. There are **two different dependency
symbols** depending on the domain — override the right one:

- app files / admin hard-delete / clear-data →
  `from src.api.v1.apps.files_router import storage_dependency`
- conversation attachment sweep →
  `from src.api.v1.attachments.router import storage_dependency`

### 6a. The dict-backed `ObjectStorage` fake (from `test_files.py:23-75`)

```python
from datetime import timedelta
from src.services.storage.base import ListPage, ObjectMeta, ObjectStorage
from src.services.storage.errors import StorageError, StorageNotFoundError, StorageSignError


class _DictStorage(ObjectStorage):
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
        return ObjectMeta(key=key, size=len(data), content_type=content_type,
                          etag=None, last_modified=None)

    async def get(self, key):
        if key not in self.objects:
            raise StorageNotFoundError("missing", provider="fake", key=key)
        return self.objects[key]

    async def head(self, key):
        data = self.objects.get(key)
        return None if data is None else ObjectMeta(
            key=key, size=len(data), content_type=None, etag=None, last_modified=None)

    async def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)

    async def list(self, prefix, *, page_size=1000, token=None):
        return ListPage(keys=tuple(k for k in self.objects if k.startswith(prefix)),
                        next_token=None)

    async def _signed_read_url_impl(self, key, *, expires_in: timedelta):
        if not self._can_sign:
            raise StorageSignError("no signing", provider="fake", key=key)
        return f"https://fake.local/{key}"

    async def aclose(self):
        return None
```

Wire it onto the `app` fixture (note: this needs the `app` fixture in your test signature,
because you mutate `app.dependency_overrides`):

```python
from src.api.v1.apps.files_router import storage_dependency

async def test_journey(client, app, db_session):
    store = _DictStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
    app_row, headers = await _approved_app(db_session)
    # ... upload/list/delete; assert against store.objects
```

### 6b. The simpler `FakeStorage` for attachment sweeps (from `tests/fakes.py`)

The conversations conftest already overrides the attachments store autouse
(`tests/api/v1/conversations/conftest.py`). If you need it directly:

```python
from tests.fakes import FakeStorage   # has .objects: dict[str, bytes]

fake = FakeStorage()
app.dependency_overrides[
    __import__("src.api.v1.attachments.router", fromlist=["storage_dependency"]).storage_dependency
] = lambda: fake
```

---

## 7. Injecting a `TestModel` for the chat endpoint (no network)

`POST /v1/claude` streams SSE from a pydantic-ai model. In tests the Foundry model is
replaced with a `pydantic_ai.models.test.TestModel`, and billing is bound to the
rolled-back test session — both live in `tests/api/v1/claude/conftest.py`.

**To reuse them in a journey test, place your journey module under
`tests/api/v1/claude/` OR copy the two fixtures into a local `conftest.py`** (the
`set_chat_model` + autouse `_override_billing` fixtures are scoped to that directory):

```python
# conftest.py next to your journey test — verbatim from tests/api/v1/claude/conftest.py
import contextlib
import pytest


@pytest.fixture(autouse=True)
def _override_billing(app, db_session) -> None:
    from src.api.v1.claude.router import billing_session_factory

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session   # do NOT close/rollback — the db_session fixture owns teardown

    app.dependency_overrides[billing_session_factory] = lambda: lambda: _session()


@pytest.fixture
def set_chat_model(app):
    def _set(model) -> None:
        from src.api.v1.claude.router import chat_model
        app.dependency_overrides[chat_model] = lambda: model
    return _set
```

Drive it (`test_chat_stream.py:56-64`):

```python
from pydantic_ai.models.test import TestModel

async def test_chat_turn(client, db_session, set_chat_model):
    headers, _ = await _auth_user(db_session)
    set_chat_model(TestModel(custom_output_text="hello world"))
    resp = await client.post("/v1/claude", headers=headers, json={"messages": [
        {"role": "user", "content": "hello"}
    ]})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert 'data: {"delta":{"text":"hello world"}}\n\n' in resp.text
    assert resp.text.endswith("data: [DONE]\n\n")
```

`resp.text` fully drains the stream — **billing lands before `[DONE]`**, so read the body
before asserting a `TokenUsage` row exists. Without a model override the endpoint returns
**503** `{"error": {"message": "Claude client not configured."}}`.

---

## 8. Conversations (append / list / get / patch / delete)

Cookie-authed under `/v1/conversations` (`test_conversations.py`, `test_append_delete.py`).
The wire shape is Express-stable: message id is **`_id`**, envelope errors are
`{"error": {"message": ...}}`.

**Message doc shape** (append body, `test_append_delete.py:29-35`):

```python
def _message(seq=0, parts=None):
    return {"_id": str(uuid.uuid4()), "role": "user", "seq": seq,
            "parts": parts or [{"type": "text", "text": "hi"}]}

def _header(kind="planning"):
    return {"kind": kind, "title": "My chat"}
```

| verb + path | request | response |
|---|---|---|
| `POST /v1/conversations/{cid}/messages` | `{"message": {_id,role,seq,parts}, "header": {kind,title}}` | **201** `{"ok": True, "message": {"_id": ..., "seq": 0}}`; header upserted once; duplicate `_id` is idempotent (no 2nd row); cross-user same `cid` → **409** `"Conversation id already in use."` |
| `GET /v1/conversations` | `?kind=builder` optional | **200** `{"conversations": [{_id, kind, title, createdAt (…Z)}]}`, newest-first, scoped to caller; unknown kind → **400** `"Unknown kind."` |
| `GET /v1/conversations/{cid}` | — | **200** `{"conversation": {_id,...}, "messages": [{_id,role,seq,parts}, ...]}` **sorted by seq asc**; cross-user → **404** `"Conversation not found."`; bad id → **400** `"Invalid conversation id."` |
| `PATCH /v1/conversations/{cid}` | `{"title": ..., "context": {...}}` or `{"code": {"source","entry"}}` | **200** `{"ok": True}`; code snapshot wraps as `{"current": snapshot}`; cross-user → **404** |
| `DELETE /v1/conversations/{cid}` | — | **200** `{"ok": True}`; CASCADE deletes messages + sweeps attachment rows **and** blobs (needs attachments-store fake, §6b); cross-user → **404** |

Get-with-messages, the shape a journey asserts (`test_conversations.py:82-96`):

```python
resp = await client.get(f"/v1/conversations/{conv.id}", headers=headers)
body = resp.json()
assert body["conversation"]["_id"] == str(conv.id)
assert [m["seq"] for m in body["messages"]] == [0, 1, 2]     # seq-ordered
assert body["messages"][0]["parts"] == [{"type": "text", "text": "hi"}]
```

---

## 9. Reading the audit trail

Two ways, both used in real tests.

**A) Direct DB query on the shared `db_session`** — fastest, no admin cookie
(`test_lifecycle.py:95-104`, `test_records.py:158-169`). Columns on `AuditLog`
(`src/db/models/audit.py`): `id, actor_id (uuid|None), action (str), resource_type (str),
resource_id (str|None), detail (jsonb|None), created_at`. `append_audit` flushes within the
caller's transaction — no commit — so the row is visible on `db_session` immediately
(`src/services/audit/log.py`).

```python
import sqlalchemy as sa
from src.db.models.audit import AuditLog

row = (await db_session.execute(
    sa.select(AuditLog).where(
        AuditLog.resource_type == "app", AuditLog.resource_id == app_id
    )
)).scalar_one()
assert row.action == "submit"
assert row.actor_id == user.id
```

**B) Through the admin API** — `GET /v1/admin/apps/{id}/audit` (admin cookie),
returns `{"events": [AuditEventOut, ...]}` newest-first, limit 200
(`src/api/v1/admin/router.py:427-458`, `test_apps_governance.py:214-220`). Each event:
`{id, actorId, action, resourceType, resourceId, detail, createdAt}` (camelCase). The query
matches both `resource_id == app_id` **and** `detail.appId == app_id`.

```python
events = await client.get(f"/v1/admin/apps/{app_id}/audit", headers=admin_headers)
actions = [e["action"] for e in events.json()["events"]]
assert "approve" in actions
```

---

## 10. Observing a 500 (the non-raising client)

The default `client` re-raises app exceptions, so a genuine endpoint error fails the test
before you can assert the status. To assert a **500** (e.g. storage put failure leaving no
orphan blob), build a non-raising transport (`test_files.py:144-160`):

```python
import httpx

transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
    resp = await _upload(c, app_row.id, headers, filename="a.csv",
                         content_type="text/csv", data=b"...")
assert resp.status_code == 500
```

---

## 11. A full journey skeleton (provision → submit → approve → serve → data → audit)

```python
import uuid
import sqlalchemy as sa
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds

def _cookie(jwt): return {"Cookie": f"session={jwt}"}

_SUBMIT = {
    "source": "export default function PreviewApp(){ return <div>hi</div>; }",
    "entry": "PreviewApp",
    "compiled": "var PreviewApp = () => React.createElement('div', null, 'JOURNEY');",
}


async def test_owner_builds_admin_approves_public_serves(client, db_session):
    # 1. owner provisions + submits
    owner = await UserFactory.create(db_session, email="owner@rvaiglobal.com")
    oh = _cookie(mint_session_jwt(owner.id, owner.token_version, _TTL))
    app_id = (await client.post("/v1/apps/provision",
              json={"conversationId": str(uuid.uuid4())}, headers=oh)).json()["appId"]
    assert (await client.post(f"/v1/apps/{app_id}/submit",
            json=_SUBMIT, headers=oh)).json()["status"] == "pending"

    # 2. admin approves
    admin = await UserFactory.create(db_session, email="admin@bial.com")
    ah = _cookie(mint_session_jwt(admin.id, admin.token_version, _TTL))
    assert (await client.post(f"/v1/admin/apps/{app_id}/approve",
            headers=ah)).json() == {"appId": app_id, "status": "approved"}

    # 3. the public runner frame now serves the compiled artifact
    frame = await client.get(f"/apps/{app_id}/frame")
    assert frame.status_code == 200
    assert "JOURNEY" in frame.text          # the artifact is rendered into the frame

    # 4. the data-plane accepts writes under the app key
    key = (await db_session.get(AppRegistry, uuid.UUID(app_id))).app_key
    dh = {"X-App-Key": key}
    rec = await client.post(f"/v1/apps/{app_id}/records",
          json={"data": {"n": 1}}, headers=dh)
    assert rec.status_code == 201

    # 5. the trail recorded submit + approve
    actions = (await db_session.execute(
        sa.select(AuditLog.action).where(AuditLog.resource_id == app_id)
    )).scalars().all()
    assert {"submit", "approve"} <= set(actions)
```

---

## Gotchas checklist

- **No commit needed** to assert against the DB — endpoint + test share one `db_session`;
  the request path flushes. Everything rolls back after the test.
- **Mutating `app.dependency_overrides`** requires the `app` fixture in your test signature,
  not just `client`.
- **Two `storage_dependency` symbols** — `apps.files_router` vs `attachments.router`.
  Override the one your route uses.
- **`set_chat_model` / billing override are directory-scoped** to `tests/api/v1/claude/`.
  Put a chat journey there, or copy the conftest fixtures locally.
- **Superadmin = email allowlist**, not a role. `admin@bial.com` / `superadmin@bial.com`
  (`.env.test`).
- **`X-App-Key` for data-plane, session Cookie for owner/admin, no auth for runner
  shell/frame** (status-gated). Three different auth models — don't mix them up.
- **The compiled artifact is served verbatim into `/apps/{id}/frame`** — assert a unique
  substring of your `compiled` string appears in `resp.text` to prove render.
- Default `client` **re-raises app errors**; use a `raise_app_exceptions=False` transport
  to observe a 500.
