# Journey Contracts — SPA-facing assertions each pytest journey MUST make

Every row is a promise the **current SPA already relies on**. A journey test drives the real
FastAPI app the way the portal does and asserts the row's contract. `BROKEN-captures-bug` rows
are written to **fail today** (red) and **pass after the fix** — do not weaken them to green.

Wire note: the portal calls `/api/*`; the Vite/proxy rewrite maps `/api/*` → `/v1/*`. Tests hit
the FastAPI routes at `/v1/*` directly. Response bodies are camelCase (`alias_generator=to_camel`)
or, for conversations, the Express `_id`/`{error:{message}}` envelope.

---

## Journey 1 — Deploy lifecycle: provision(C) → status(C) → submit(C)  ⛔ CRITICAL

The builder provisions an app **for a conversation C** (`provisionApp` sends `{conversationId: C}`),
then addresses that same app at `/apps/C/status` and `/apps/C/submit` using the **conversation/build
id C** — never the id returned by provision. So the whole builder deploy UX is contractually bound to
"an app provisioned for conversation C is addressable at `/v1/apps/C/*`". FastAPI breaks this: provision
mints a **fresh uuid7 PK** (`AppRegistry.id`) and stores C only as a soft, non-PK `conversation_id`
column; `status`/`submit` resolve by `db.get(AppRegistry, app_id)` on the **PK**, so `/apps/C/*` 404s.

| # | Assertion the test must make | Status | Evidence |
|---|------------------------------|--------|----------|
| 1.1 | `POST /v1/apps/provision {conversationId: C}` → **201**, and `body.appId == C` (the SPA re-addresses the app at `/apps/C/*`, so the returned appId must equal C). | **BROKEN-captures-bug** — provision leaves `id` to the uuid7 default; `app_id=row.id != C`. | send: `portal/src/utils/appRegistryApi.js:98-100`; back: `backend/src/api/v1/apps/router.py:99-125`; PK: `backend/src/db/models/app_registry.py:101` (`UUIDv7PrimaryKeyMixin`), `:121` (`conversation_id` non-PK soft link) |
| 1.2 | After provisioning C, `GET /v1/apps/C/status` → **200** with `status == "draft"`, `appId == C`, and an `appKey`. | **BROKEN-captures-bug** — `db.get(AppRegistry, C)` misses the uuid7 PK → `_owned_app_or_404` → 404. | call site: `portal/src/pages/BuilderPage.jsx:230` `getAppStatus(saved.id)`; api: `appRegistryApi.js:116-118`; back: `apps/router.py:190-200`, `:128-134` |
| 1.3 | After provisioning C, `POST /v1/apps/C/submit {source,compiled,entry}` → **200** with `status == "pending"`, `appId == C`. | **BROKEN-captures-bug** — same PK-vs-conversation miss → 404 before the transition runs. | call site: `BuilderPage.jsx:540` `submitApp(id,…)`; api: `appRegistryApi.js:109-113`; back: `apps/router.py:137-187`, `:146` |
| 1.4 | Repeat `provision(C)` for the same owner returns the **same** app (idempotent) — `body.appId` stable, same `appKey`. Once 1.1 is fixed this must stay true. | OK (idempotent upsert already) — but only observable/meaningful after the 1.1 fix; assert alongside. | back: `apps/router.py:107-118` (`on_conflict_do_update` on `uq_app_registry_owner_conversation`) |
| 1.5 | Cross-user: user B `GET /v1/apps/C/status` for user A's app → **404** (owner-scoped, non-leaking). | OK (fail-closed) — keep as a guard so the 1.1 fix doesn't open a cross-user read. | back: `apps/router.py:128-134` (`app.user_id != user_id → 404`) |

> Likely fix under test: provision must make **C the primary key** (`id=body.conversation_id`) — the
> model docstring already states "the row's `id` IS the appId" (`app_registry.py:4-10`), so status/submit
> resolving by PK is correct once provision stops minting a divergent uuid7. Do **not** "fix" by pointing
> the SPA at `body.appId`; the SPA is the spec here.

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
- **1.1, 1.2, 1.3** — provision/status/submit keyed on a fresh uuid7 PK instead of the conversation id C (CRITICAL: the entire builder deploy flow 404s).
- **2.1** — admin apps list has no owner username/email (`ownerId` uuid only).
- **3.1, 3.2, 3.3, 3.4** — audit rows miss `_id`, `at`, `username`, and top-level `recordId`/`count`.
