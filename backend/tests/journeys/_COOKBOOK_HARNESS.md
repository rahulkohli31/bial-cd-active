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
- The client origin host is `http://test` — the origin string request-scoped CSP / CORS
  assertions compare against.

---

## 1. Auth cookie helper (mint a session, become a user)

Every authenticated request carries a `Cookie: session=<jwt>` header. The JWT is minted
directly (no OIDC round-trip) with `mint_session_jwt(user_id, token_version, ttl)`.
Pattern is identical across `test_lifecycle.py`, `test_conversations.py`,
and `test_chat_stream.py`:

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
    user = await UserFactory.create(db, email="admin@bial.com")  # → super-admin
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
    UserFactory,
    AppRegistryFactory,
    ConversationFactory,
    MessageFactory,
)
```

**`UserFactory.create(db, **overrides)`** — defaults `azure_oid=f"oid-{uuid4}"`,
`email="citizen@rvaiglobal.com"`. Override `email=` to steer identity (admin vs citizen,
or two distinct users for cross-user isolation).

**`AppRegistryFactory.create(db, *, user_id, **overrides)`** — `user_id` is **required**
(the ownership boundary). Defaults: a freshly-minted `app_key` (via `mint_app_key()`,
prefix `bial_`), a random `conversation_id`, `status=AppStatus.DRAFT`, `name=""`. Common
overrides for journeys: `status=AppStatus.APPROVED`, `login_required=False`, the typed
submission refs `source_submission_id=uuid4()`, `source_commit_sha="1f"*20`,
`submitted_at=datetime.now(UTC)`, the pin `approved_submission_id=` /
`approved_commit_sha=`. (The JSX-era
`source_snapshot=`/`approved_snapshot=` JSONB kwargs are GONE — migration 0018
dropped the columns; seed refs, not artifact bytes.)

**`ConversationFactory.create(db, user_id, **overrides)`** — `user_id` required (2nd
positional). Defaults: `id=uuid4()` (client-minted, v4), `kind=ConversationKind.PLANNING`.
Override `title=`, `kind=ConversationKind.BUILDER`, `updated_at=` (for ordering tests).

**`MessageFactory.create(db, user_id, conversation_id, **overrides)`** — both ids
required. Defaults: `id=uuid4()`, `role=MessageRole.USER`, `seq=0`,
`parts=[{"type": "text", "text": "hi"}]`. Override `seq=`, `role=MessageRole.ASSISTANT`,
`parts=[...]`.

Enums to import when overriding:

```python
from src.db.models.app_registry import AppStatus  # DRAFT/PENDING/APPROVED/DISABLED/REJECTED
from src.db.models.conversation import ConversationKind  # PLANNING/ASSISTANT/BUILDER
from src.db.models.message import MessageRole  # USER/ASSISTANT
```

---

## 3. The app lifecycle chain: mint → submit → approve

This is the spine of most journeys. Verbatim request/response shapes below.

### 3a. mint the app row — `resolve_app_for_project` (NOT an endpoint)

`POST /v1/apps/provision` was **removed in U6** — it had zero production callers. The app row
is minted by the build session, and a journey mints it the same way, in-process:

```python
from src.services.build_sessions.appdata import resolve_app_for_project

project = await ProjectFactory.create(db_session, user.id)
app_id = await resolve_app_for_project(db_session, user.id, project.id)
await db_session.commit()  # the endpoints under test read through their own session
```

Returns just the `uuid.UUID`. The upsert still mints `app_key` on insert — read it off the row
(`(await db_session.get(AppRegistry, app_id)).app_key`) if a journey needs it; `GET
/v1/apps/{id}/status` is the only surface that returns it over the wire.

Idempotent **per project** (`uq_app_registry_project`): a second call for the same project
returns the same id and the same key (`tests/services/build_sessions/test_appdata.py`). A
project owned by another user is a non-leaking **404**; a project whose app belongs to another
user is a **409**.

### 3b. submit — `submit_app_for_review` (NOT an endpoint; ASM18/U8)

The citizen HTTP route (`POST /v1/apps/{app_id}/submit`) is **retired** — R15a allows
exactly one route into the review queue, and it runs through the publish request
(`POST /v1/projects/{project_id}/deploy`), which attaches both declaration answer sets.
`tests/api/v1/apps/test_submit_retired.py` guards the old route 404/405 forever, even
for the owner with a valid staged bundle. The behaviour it used to carry lives on as
`services/approvals/submit.py`'s `submit_app_for_review`, called in-process exactly as
the publish gate calls it — a journey drives it the same way, not through `client`:

```python
from src.api.deps import storage_dependency
from src.db.models.app_registry import AppRegistry, ApprovalRoute
from src.services.approvals.submit import submit_app_for_review
from src.services.storage import snapshot_key, submission_key
from tests.fakes import FakeStorage

_SHA = "ab" * 20  # 40 lowercase hex
_BUNDLE = b"# v2 git bundle\n" + _SHA.encode() + b" HEAD\n\nPACK-fake"

store = FakeStorage()
app.dependency_overrides[storage_dependency] = lambda: store  # the `app` FIXTURE
store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE

app_row = await db_session.get(AppRegistry, uuid.UUID(app_id))
receipt = await submit_app_for_review(
    db_session,
    store,
    user_id=owner.id,
    app=app_row,
    declaration={"citizen": {}, "review": {}, "differences": [], "explanation": ""},
    route=ApprovalRoute.SELF_PUBLISH,
)
await db_session.commit()
# receipt == SubmissionReceipt(submission_id, commit_sha, submitted_at)
```

After submit, the row carries the typed refs (`source_submission_id`,
`source_commit_sha`, `submitted_at`) and the immutable copy exists at
`submission_key(app_id, receipt.submission_id)` — byte-identical to the snapshot. Read
the pending state back over the wire at `GET /v1/apps/{app_id}/status`.

Rejections (raised as `AppApiError` from the call, not a response you assert against a
`client` request): no snapshot blob → **409** `"Nothing to submit — generate an app
first."`; corrupt (non-bundle) snapshot → **409**; a live build-session lock (D8) →
**409**; transient storage error → **503**; cross-user `app.user_id` → **404**.

### 3c. approve (ADMIN cookie) — `POST /v1/admin/apps/{app_id}/approve`

Approve requires the app be **PENDING**, takes the **reviewed submission id** in the
body (the D5 guard), and verifies the blob exists (R11) — so the wired store must hold
`submission_key(app_id, submission_id)` (`test_apps_governance.py`):

```python
admin_headers = await _admin(db_session)
resp = await client.post(
    f"/v1/admin/apps/{app_id}/approve",
    json={"submissionId": str(submission_id)},
    headers=admin_headers,
)
assert resp.status_code == 200
assert resp.json() == {"appId": app_id, "status": "approved"}
# fresh.approved_submission_id == the reviewed id; approved_commit_sha/by/at are set
```

Guards: non-pending → **409** (including DISABLED — approve never bypasses `enable`);
re-submitted since review (id mismatch) → **409**; blob missing → **409**; storage
error → **503**.

### 3d. the other governance transitions (ADMIN cookie)

All take the admin cookie; all return `{"appId": ..., "status": ...}` (or `{"ok": True}`
for delete) (`test_apps_governance.py:139-175`):

| call | body | success |
|---|---|---|
| `POST /v1/admin/apps/{id}/reject` | `{"note": "no good"}` | `status": "rejected"`, stores `rejection_note` |
| `POST /v1/admin/apps/{id}/disable` | — | `status": "disabled"` (requires APPROVED, else **409**) |
| `POST /v1/admin/apps/{id}/enable` | — | `status": "approved"` (requires DISABLED, else **409**) |
| `PATCH /v1/admin/apps/{id}` | `{"loginRequired": true}` | loginRequired flip is audited (`config:loginRequired`); the app name is project-sourced (#48) and no longer settable — a stray `{"name": ...}` key is ignored |
| `GET /v1/admin/apps?status=approved` | — | `{"apps": [{"appId","status","hasApprovedSnapshot","submissionId","commitSha","redeployNeeded",...}]}` — never leaks `appKey` or a signed URL; `?status=pending` orders by `submittedAt` (review queue) |
| `GET /v1/admin/apps/{id}/bundle-url` | — | `{"url","submissionId","commitSha","expiresInSeconds"}` — short-TTL signed download, audited `bundle:download` (needs a storage override, §6) |
| `POST /v1/admin/apps/{id}/mark-deployed` | — | `{"appId","deployedSubmissionId","deployedAt"}` (requires APPROVED, else **409**), audited `mark-deployed` |
| `DELETE /v1/admin/apps/{id}` | — | `{"ok": True}` — sweeps the app's blobs, drops the registry row, and post-commit salts the project's database, audited `app:delete` + `db:drop` (needs a storage override, §6) |

### 3e. shortcut: seed an already-approved app (skip the chain)

When a journey only needs an approved app, seed it directly through the factory instead of
driving mint→submit→approve:

```python
_SHA = "9d" * 20


async def _approved_app(db, **overrides):
    user = await UserFactory.create(db)
    sid = uuid.uuid4()
    app = await AppRegistryFactory.create(
        db,
        user_id=user.id,
        status=AppStatus.APPROVED,
        login_required=False,
        source_submission_id=sid,
        source_commit_sha=_SHA,
        approved_submission_id=sid,
        approved_commit_sha=_SHA,
        **overrides,
    )
    return app
```

---

## 4. Data-plane calls — RETIRED

There is no control-plane data API and no `X-App-Key` auth chain. Both were deleted in U6
together with the `data_records` / `clear_data_tokens` tables, the `app_registry` counter
columns, and the admin data-summary / clear-data endpoints (migration
`0023_drop_data_records`).

A generated app's data lives in **its project's own PostgreSQL database** (ADR-0028), reached
with Drizzle from the app's own server code over the injected `BIAL_DATABASE_URL`. Nothing about
that path passes through the control plane, so there is nothing to drive from a journey test:
the platform-side surfaces are provisioning (`services/appdb/`), the kill-switch sever on admin
disable, the audited DSN reveal, and teardown — all covered by their own tests.

`app.app_key` still exists as a publishable label returned by `GET /v1/apps/{id}/status`. It
authorizes nothing. Do not build a request header out of it.

The per-app FILE surface (`/v1/apps/{app_id}/files`, the `APP_FILE_*_CAP` quotas) was retired
earlier, with the open-sandbox pivot — a built app stores files in its OWN per-app Blob
container via the injected `BIAL_BLOB_*` env.

---

## 5. Runner / frame render assertion — RETIRED

The old-JSX runner serving surface — `/apps/{id}` (shell) + `/apps/{id}/frame`, the shell/frame
CSP builders in `src.services.appserving.csp`, `runner.py`, and `test_runner.py` — was removed
with the open-sandbox pivot. A deployed app is served from the sandbox's own Caddy, NOT this
control plane, so there is no in-process render assertion: the build→submit→approve pipeline now
ends at `approved` (see `test_journey_build_deploy_render.py::test_build_submit_approve_pipeline`).
`verify_runner_token` went with the app-key chain it guarded (U6); `decode_session_jwt` — the real
cookie-session primitive — stays. Tokens are minted inline via `mint_session_jwt`.

---

## 6. Swapping in a fake object store (bundles, hard-delete, attachment sweep)

Any route that touches blob storage must have its storage dependency overridden with an
in-memory fake, or it will reach for real Azure. There are **two different dependency
symbols** depending on the domain — override the right one:

- app files / admin hard-delete / clear-data →
  `from src.api.deps import storage_dependency`
- conversation attachment sweep →
  `from src.api.v1.attachments.router import storage_dependency`

### 6a. The dict-backed `ObjectStorage` fake (see `test_apps_governance.py`)

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
```

Wire it onto the `app` fixture (note: this needs the `app` fixture in your test signature,
because you mutate `app.dependency_overrides`):

```python
from src.api.deps import storage_dependency


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
from tests.fakes import FakeStorage  # has .objects: dict[str, bytes]

fake = FakeStorage()
app.dependency_overrides[
    __import__("src.api.v1.attachments.router", fromlist=["storage_dependency"]).storage_dependency
] = lambda: fake
```

---

## 7. Injecting a `TestModel` for the chat endpoint (no network)

`POST /v1/conversations/{id}/turns` starts a turn on the turn engine; the reply streams from a
pydantic-ai model over `GET /v1/conversations/{id}/events`. In tests the Foundry model is
replaced with a `pydantic_ai.models.test.TestModel` (or a `FunctionModel` when the test needs to
see the prompt), and billing is bound to the rolled-back test session.

> **The relay this section used to document is gone.** `POST /v1/claude` and
> `tests/api/v1/claude/` were retired — the turn engine is the only send path
> (guard: `tests/api/v1/claude_retired/test_relay_retired.py`). The two fixtures now live in
> `tests/api/v1/conversations/conftest.py`, and the dependencies they override come from
> `src/api/v1/conversations/_shared.py`, not the deleted `claude/router.py`.

**To reuse them in a journey test, copy the two fixtures into a local `conftest.py`** (they are
scoped to `tests/api/v1/conversations/`, and a journey module lives outside it):

```python
# conftest.py next to your journey test
import contextlib
import pytest


@pytest.fixture(autouse=True)
def _override_billing(app, db_session) -> None:
    from src.api.v1.conversations._shared import billing_session_factory

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session  # do NOT close/rollback — the db_session fixture owns teardown

    app.dependency_overrides[billing_session_factory] = lambda: lambda: _session()


@pytest.fixture
def set_chat_model(app):
    def _set(model) -> None:
        from src.api.v1.conversations._shared import chat_model

        app.dependency_overrides[chat_model] = lambda: model

    return _set
```

Driving a turn is TWO steps, unlike the relay's single call: the POST returns `202` and the text
arrives on the event stream. See `tests/journeys/test_journey_multiturn_generate.py` for the
worked version, including how to await the detached task before asserting on the DB.

```python
from pydantic_ai.models.test import TestModel


async def test_chat_turn(client, db_session, app, set_chat_model):
    user, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="hello world"))
    resp = await client.post(
        f"/v1/conversations/{conversation.id}/turns",
        headers=_headers(user),
        json={"message": {"text": "hello", "attachmentTexts": [], "attachmentIds": []}},
    )
    assert resp.status_code == 202  # the turn is ACCEPTED, not yet answered
    await _settle(engine, conversation.id)  # await the detached task
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
    return {
        "_id": str(uuid.uuid4()),
        "role": "user",
        "seq": seq,
        "parts": parts or [{"type": "text", "text": "hi"}],
    }


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
assert [m["seq"] for m in body["messages"]] == [0, 1, 2]  # seq-ordered
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

row = (
    await db_session.execute(
        sa.select(AuditLog).where(AuditLog.resource_type == "app", AuditLog.resource_id == app_id)
    )
).scalar_one()
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
before you can assert the status. To assert a **500** (e.g. a storage failure that must leave no
orphan blob), build a non-raising transport:

```python
import httpx

transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
    resp = await _upload(
        c, app_row.id, headers, filename="a.csv", content_type="text/csv", data=b"..."
    )
assert resp.status_code == 500
```

---

## 11. A full journey skeleton (mint → submit → approve → audit)

```python
import uuid
import sqlalchemy as sa
from src.config import settings
from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.audit import AuditLog
from src.services.approvals.submit import submit_app_for_review
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt):
    return {"Cookie": f"session={jwt}"}


_SHA = "ab" * 20
_BUNDLE = b"# v2 git bundle\n" + _SHA.encode() + b" HEAD\n\nPACK-journey"


async def test_owner_builds_admin_approves(client, app, db_session):
    store = FakeStorage()  # §6 — submit reads the snapshot bundle
    app.dependency_overrides[storage_or_none_dependency] = lambda: store

    # 1. owner's build session mints the app row; the build finalized a snapshot bundle
    owner = await UserFactory.create(db_session, email="owner@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, owner.id)
    app_id = await resolve_app_for_project(db_session, owner.id, project.id)
    await db_session.commit()
    store.objects[snapshot_key(app_id)] = _BUNDLE

    # 2. owner submits: draft -> pending + an immutable per-submission copy. There is no
    #    citizen HTTP route for this (§3b, ASM18) — call the service directly, the way the
    #    publish gate does.
    app_row = await db_session.get(AppRegistry, app_id)
    receipt = await submit_app_for_review(
        db_session,
        store,
        user_id=owner.id,
        app=app_row,
        declaration={"citizen": {}, "review": {}, "differences": [], "explanation": ""},
        route=ApprovalRoute.SELF_PUBLISH,
    )
    await db_session.commit()
    assert receipt.commit_sha == _SHA

    # 3. admin approves
    admin = await UserFactory.create(db_session, email="admin@bial.com")
    ah = _cookie(mint_session_jwt(admin.id, admin.token_version, _TTL))
    assert (await client.post(f"/v1/admin/apps/{app_id}/approve", headers=ah)).json()[
        "status"
    ] == "approved"

    # 4. the trail recorded submit + approve
    actions = (
        (
            await db_session.execute(
                sa.select(AuditLog.action).where(AuditLog.resource_id == str(app_id))
            )
        )
        .scalars()
        .all()
    )
    assert {"submit", "approve"} <= set(actions)
```

---

## Gotchas checklist

- **No commit needed** to assert against the DB — endpoint + test share one `db_session`;
  the request path flushes. Everything rolls back after the test.
- **Mutating `app.dependency_overrides`** requires the `app` fixture in your test signature,
  not just `client`.
- **Two `storage_dependency` symbols** — `src.api.deps` (admin/governance) vs `attachments.router`.
  Override the one your route uses.
- **`set_chat_model` / billing override are directory-scoped** to
  `tests/api/v1/conversations/`. A journey lives outside it, so copy the fixtures locally
  (§7). The four files in that directory that drive turns opt in with a module-level
  `pytestmark = pytest.mark.usefixtures("_fresh_engine", "_override_billing")`.
- **Superadmin = email allowlist**, not a role. `admin@bial.com` / `superadmin@bial.com`
  (`.env.test`).
- **One auth model: the session Cookie**, for owner and admin alike. The `X-App-Key` header
  chain and the unauthenticated runner shell/frame are both GONE (U6 / the open-sandbox pivot);
  `app.app_key` is a label that authorizes nothing.
- **There is no in-process render assertion.** A deployed app is served by the sandbox's own
  Caddy, not this control plane — the pipeline a journey can drive ends at `approved`.
- Default `client` **re-raises app errors**; use a `raise_app_exceptions=False` transport
  to observe a 500.
