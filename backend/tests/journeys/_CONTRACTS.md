# Journey Contracts — SPA-facing assertions each pytest journey MUST make

Every row is a promise the **current SPA already relies on**. A journey test drives the real
FastAPI app the way the portal does and asserts the row's contract. `BROKEN-captures-bug` rows
are written to **fail today** (red) and **pass after the fix** — do not weaken them to green.

Wire note: the portal calls `/api/*`; the Vite/proxy rewrite maps `/api/*` → `/v1/*`. Tests hit
the FastAPI routes at `/v1/*` directly. Response bodies are camelCase (`alias_generator=to_camel`)
or, for conversations, the Express `_id`/`{error:{message}}` envelope.

---

## Journey 1 — Deploy lifecycle: provision → status(appId) → submit(appId)

**Updated 2026-07-09 for one app per project (KD-4).** A project holds exactly ONE app (its
tool/code). The builder provisions that app — passing `{conversationId, projectId}` — and
addresses it **flat by the RETURNED appId** (`/v1/apps/{appId}/status`, `/submit`), never by a
conversation id. The app has its own fresh UUIDv7 PK; the acting builder conversation is recorded
as the app's head/last-builder pointer (`conversation_id`), and the parent project is resolved via
`project_id` for the breadcrumb. Provision is idempotent **per project**. The rebuilt frontends
address by the returned appId (there is no deployed SPA to preserve — the frontends are built after
the backend; see memory `app-identity-and-flat-url-model`).

| # | Assertion the test must make | Status | Evidence |
|---|------------------------------|--------|----------|
| 1.1 | `POST /v1/apps/provision {conversationId: C, projectId: P}` → **201**; `body.appId` is the app's OWN fresh id (`!= C`), and the app's `conversation_id` head == C. | OK — provision mints/reuses the project's one app (fresh uuid7 PK). | back: `backend/src/api/v1/apps/router.py` `provision`; PK: `backend/src/db/models/app_registry.py` (`UUIDv7PrimaryKeyMixin`), `uq_app_registry_project`, `conversation_id` head pointer |
| 1.2 | After provisioning, `GET /v1/apps/{appId}/status` → **200** with `status == "draft"`, `appId == <returned>`, and an `appKey`. | OK — resolves by the returned PK. | back: `apps/router.py` `read_status`, `_owned_app_or_404` |
| 1.3 | `POST /v1/apps/{appId}/submit {source,compiled,entry}` → **200** with `status == "pending"`, `appId == <returned>`. | OK — resolves by the returned PK. | back: `apps/router.py` `submit` |
| 1.4 | Repeat provision in the SAME project returns the **same** app (idempotent per project) — `body.appId` stable, same `appKey`, no second row; even from a different conversation (which just advances the head). | OK — `on_conflict_do_update` on `uq_app_registry_project`. | back: `apps/router.py` `provision` (upsert on `uq_app_registry_project`) |
| 1.5 | Cross-user: user B `GET /v1/apps/{appId}/status` for user A's app → **200 `{status: null}`** (owner-scoped, non-leaking "not provisioned"); provision with another user's `projectId` → **404**. | OK (fail-closed). | back: `apps/router.py` `read_status` (`app.user_id != user_id`), `resolve_project_for_write` (cross-user project → 404) |

---

## Journey 2 — Admin App Registry list: Owner column

The admin table renders the owner from **`app.ownerUsername`** (and the review modal reads the same).
`AdminAppOut` exposes only `ownerId` (a raw uuid) — no username/email — so the Owner cell always
renders the `—` fallback and no admin can tell whose app it is.

| # | Assertion the test must make | Status | Evidence |
|---|------------------------------|--------|----------|
| 2.1 | `GET /v1/admin/apps?status=pending` → each `apps[]` carries a human owner identifier the SPA reads as `ownerUsername` (owner's email/display name), non-null for a real owner. | **BROKEN-captures-bug** — `AdminAppOut` projects `owner_id` only (`ownerId`); no `ownerUsername`/`ownerEmail`. | SPA read: `portal/src/components/admin/AppRegistryPanel.jsx:296`, `:54`; back schema: `backend/src/api/v1/admin/router.py:55-73`, projection `:141-159` |
| 2.2 | The list is admin-gated: a non-superadmin caller → **403** (RBAC at the API). | OK — keep as the gate guard. | back: `admin/router.py:187-189` (`CurrentSuperadmin`) |

---

## Journey 3 — Admin Audit drawer for one app

`AuditDrawer` renders each event via `ev._id` (React key), `ev.at` (timestamp), `ev.username`
(actor), and `ev.recordId` / `ev.count` (detail). `AuditEventOut` emits `id`, `createdAt`,
`actorId` (raw uuid), `resourceId`, and a nested `detail` object — **none** of the four SPA keys.
Every audit row therefore renders with an undefined React key, a `—` time, and `anonymous`.

| # | Assertion the test must make | Status | Evidence |
|---|------------------------------|--------|----------|
| 3.1 | `GET /v1/admin/apps/{C}/audit` → each `events[]` has an `_id` the SPA keys on (not only `id`). | **BROKEN-captures-bug** — schema field is `id`; SPA reads `ev._id`. | SPA: `AppRegistryPanel.jsx:173`; back: `admin/router.py:124-132`, emit `:445-457` |
| 3.2 | Each event carries a timestamp the SPA reads as `ev.at` (renderable by `fmtWhen`). | **BROKEN-captures-bug** — schema field is `createdAt`; `fmtWhen(ev.at)` → `—`. | SPA: `AppRegistryPanel.jsx:176`, `:22-25`; back: `admin/router.py:131` |
| 3.3 | Each event carries a human actor as `ev.username` (resolved from the actor, not a raw uuid). | **BROKEN-captures-bug** — schema exposes `actorId` (uuid) only; SPA falls back to `anonymous`. | SPA: `AppRegistryPanel.jsx:179`; back: `admin/router.py:126` |
| 3.4 | A record/count-bearing event (e.g. `clear-data`, `config:loginRequired`) surfaces `ev.recordId` and/or `ev.count` where the SPA reads them (top-level), for at least the count. | **BROKEN-captures-bug** — count lives in `detail.count`, no `recordId`; SPA reads `ev.recordId`/`ev.count` top-level. | SPA: `AppRegistryPanel.jsx:179`; back detail nesting: `admin/router.py:128`, `:277-279`, `:367-374` |
| 3.5 | Audit is admin-gated: non-superadmin → **403**. | OK — keep as the gate guard. | back: `admin/router.py:427-429` (`CurrentSuperadmin`) |

---

## Journey 4 — Conversation / build persistence (chat + builder history)

The SPA writes turns and reads them back through `conversationApi.js`, normalizing the server's
`_id`→`id` and expecting `{_id, role, parts, seq, createdAt}` messages and a
`{_id, kind, title, createdAt, updatedAt, context, code}` header. The builder additionally patches
`code` and re-reads it at `header.code.current.source`. FastAPI already ports these shapes verbatim,
so this journey is the **green baseline** that proves the harness drives the real persistence path.

| # | Assertion the test must make | Status | Evidence |
|---|------------------------------|--------|----------|
| 4.1 | `POST /v1/conversations/{id}/messages {message:{_id,role,parts,seq,schemaVersion,createdAt}, header:{kind,title}}` → **201 `{ok:true, message:{_id,seq}}`**; the header is upserted so the conversation exists after one call. | OK | SPA: `portal/src/utils/conversationApi.js:62-73`, `:125-131`; back: `backend/src/api/v1/conversations/router.py:295-381` |
| 4.2 | `GET /v1/conversations/{id}` → `messages[]` each shaped `{_id, role, parts, seq, createdAt}`, ordered by `seq` ascending; header shaped `{_id, kind, title, createdAt, updatedAt}`. | OK | SPA normalize: `conversationApi.js:14-30`, `:45-54`; back: `conversations/router.py:78-86`, `:60-75`, `:130-147` |
| 4.3 | Builder: `PATCH /v1/conversations/{id} {code:{source,entry,createdAt}}` then `GET` → `header.code.current.source == source` (server wraps the snapshot under `current`). | OK | SPA: `portal/src/utils/builderHistory.js:29-31`, read `portal/src/pages/BuilderPage.jsx:219`; back wrap: `conversations/router.py:187-188` |
| 4.4 | `parts[]` round-trips unchanged (text + file parts), and a re-`POST` of the same `message._id` is idempotent (no duplicate, still 201). | OK | back: `conversations/router.py:225-254` (parts validation), `:356-372` (idempotent on dup `_id`) |
| 4.5 | Cross-user: user B `GET`/`DELETE` of user A's conversation id → **404** (owner-scoped). | OK — keep as the isolation guard. | back: `conversations/router.py:104-122` (`_load_owned` scopes by `user_id`) |

---

## Journey 5 — Plan → Builder handoff (summarize relay)

`ChatPage.handleBuildApp` flattens the planning transcript and calls the **stateless** Claude relay
with `{messages:[{role:'user', content: transcript}], system: SUMMARIZE_SYSTEM_PROMPT}`, streams the
builder prompt into the modal, then `handleLaunchBuilder` navigates to `/workspace/builder` with
`state.prompt` — which BuilderPage's first-prompt effect consumes to provision/generate (feeding
Journey 1). The backend-observable contract is the relay accepting a one-shot `messages+system`
request and streaming assistant text.

| # | Assertion the test must make | Status | Evidence |
|---|------------------------------|--------|----------|
| 5.1 | `POST /v1/claude {messages:[{role:'user',content}], system}` → **200** streaming body; concatenated deltas are non-empty assistant text (the builder prompt). | OK | SPA: `portal/src/pages/ChatPage.jsx:357-364`; back: `backend/src/api/v1/claude/router.py:198-230`, stream `:133-196` |
| 5.2 | `POST /v1/claude` with empty/absent `messages` → **400 `{error:{message}}`** (SPA error-banner envelope). | OK | back: `claude/router.py:224-226`, `:85-86` |
| 5.3 | The relay persists nothing — a summarize call creates no conversation/message rows (SPA owns persistence via Journey 4). | OK | back: `claude/router.py:1-9` docstring ("server persists NO messages") |

---

### Summary of BROKEN rows (must be red now, green after fix)
- **Journey 1** — updated 2026-07-09 to one-app-per-project flat-id addressing (KD-4); all rows now GREEN (the app is addressed by its returned appId, not the conversation id).
- **2.1** — admin apps list has no owner username/email (`ownerId` uuid only).
- **3.1, 3.2, 3.3, 3.4** — audit rows miss `_id`, `at`, `username`, and top-level `recordId`/`count`.
