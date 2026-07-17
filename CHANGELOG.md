# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.6.0-phase2.1] - 2026-07-17

**Pilot closure.** Every in-pilot gap from the 2026-07-16 release audit, closed against
existing seams. Two of these were silent-data-loss paths: a build could quietly overwrite
your saved app with a blank template, and a finished build could report that your work was
saved when it wasn't. Attachments you added to a build now actually reach the agent — before
this, the build ran as if the file wasn't there.

### Added
- **Files attached in the composer reach the build.** Images and PDFs arrive as vision
  content, spreadsheets and documents as their extracted text, CSV/TXT inline — so "build me
  an app from this spreadsheet" now works. Attachments are collected from every turn since the
  last build (not just the newest message), scoped to the owner and the project, and a file
  that can't be read fails the start with a clear message naming it rather than silently
  building the wrong app.
- **Deployed apps get their own long-lived storage credential.** A superadmin mints a
  365-day, container-scoped Blob credential (`POST /v1/admin/apps/{id}/deploy-credential`), so
  a live app reaches its own storage directly with no platform in the data path. It is minted
  against a per-app stored access policy, which is what makes revoking it real: delete the
  policy and the credential dies. Supersedes the go-live runbook's KNOWN GAP.
- **"Your app is live."** The superadmin records the deployed URL at mark-deployed and the
  app's owner sees a Live link instead of "deployed by the platform team". https-only.
- **Prompt caching on the build loop**, at the 1-hour tier — a build's steps can sit minutes
  apart while npm installs, and a 5-minute cache would expire between them and cost more than
  it saved.

### Changed
- **The build agent never seeds fake data.** No dummy, sample, or placeholder records; it
  builds honest empty, loading, and error states, and real data arrives by upload or entry.
  Restores a promise the POC kept and the open-sandbox rewrite lost.
- **The end-of-build event is now emitted by the session manager, not the agent** — after the
  snapshot commits, so `snapshot_committed` finally tells the truth. The agent can no longer
  emit a terminal event at all; the capability was removed rather than merely discouraged.
- Portal lint runs again: a v9 flat config plus the plugins it always needed. `npm run lint`
  has been in `package.json` for months with no config on disk, so it could not execute at all.

### Fixed
- **A transient storage error can no longer put a blank template over your work.** The restore
  path retries, then fails the build with "Sandbox unavailable. Please try again later or
  contact the admin" — leaving your saved version intact. Provisioning a fresh app now happens
  only when storage positively confirms there is nothing saved. A failing restore used to fall
  back to a blank sandbox, which the next snapshot then wrote over the user's real work.
- Builds no longer fail on deployments that run without object storage configured.
- A build that finished while a stop was landing could be torn down without its snapshot while
  still reporting success.
- An escalated build reported itself as a graceful end rather than a failure.
- Attachments now precede the instruction in the prompt, matching Anthropic's vision ordering
  and the portal's own assembly.
- Office attachment `format` is sanitized before it reaches the prompt fence.
- A 2083-character URL at mark-deployed returned a server error instead of a validation error.

## [1.6.0-phase2.0] - 2026-07-13

Phase-2 **Stage 0 — the agentic-build foundation.** This is the sequential,
one-branch foundation the four parallel Wave-1 tracks fork from; it freezes the
cross-track seams and stubs the shared skeletons so every Wave-1 worktree only
*adds* files. It does **not** implement the build loop. Shipped on the non-prod
`release/phase2` integration branch (forked from `release/1.5.0`).

### Added
- **Nine field-level cross-track contracts (C1–C9)** frozen as durable docs under
  `docs/engineering/contracts/` — the supervisor HTTP API, sandbox-client ABC,
  build-session control API, snapshot/sync ordering, Redis key namespace, golden-template
  shape, brain interface + progress envelope, preview transport/framing, and interim
  app-data access — each specified to request/response/enum/signature level so the four
  Wave-1 tracks build against faithful mocks without reading each other's code.
- **Frozen backend shared-file stubs.** Optional `redis` + `sandbox` sub-configs on
  `Settings` (prod-gated, so the existing suite boots with no new env); a `services/redis/`
  async pool + frozen C5 key namespace; a `services/sandbox/` complete `SandboxClient` ABC +
  `SandboxHandle`; and a `build_sessions/` API package with real C3/C7 schemas (status enum +
  tagged-union progress envelope) behind a mounted stub router.
- **Golden Next.js CRUD template + pre-baked sandbox base image** under a new top-level
  `sandbox/` tree (Next.js 16 / React 19 / Tailwind v4 / shadcn/ui / TypeScript 5.x on Node 24
  LTS, latest-stable-then-pinned), with the single swappable data module wired to the existing
  platform data-service (HTTP client, not an ORM), cross-origin `frame-ancestors` framing in
  Caddy, and cross-platform (LF) guards for the Windows image build.
- **Walking skeleton** (`scripts/skeleton/`) that proves the two genuinely-hard facts once for
  real — cross-origin `frame-ancestors` framing with origin-validated `postMessage`, and a real
  golden-template `next dev` render — rather than mocking them away.

### Changed
- **Retired the old single-file `/preview` backend.** The in-browser Babel `/preview` shell is
  removed (route, shell, CSP builder, middleware branch, reserved root); the deployed `/apps`
  runner is unchanged. The builder live-preview is knowingly dark on `release/phase2` until the
  Wave-1 PORTAL-PREVIEW track lands the per-session cross-origin preview.
- **Decision record updated** — ADR-0014 storage clause (local disk + git-snapshot to Blob,
  public-ingress POC posture, still Proposed) and ADR-0018 (latest-stable-then-pinned stack +
  interim data-service client).

## [1.5.0] - 2026-07-03

### Added
- **Sign in with Microsoft (Entra ID).** The portal now authenticates against the
  organization's Microsoft Entra ID tenant. Signing in is a single "Sign in with Microsoft"
  click — the FastAPI control-plane runs the OpenID Connect flow itself (Authorization Code +
  PKCE), validates the Microsoft token fail-closed against the one configured tenant
  (outside-tenant and personal accounts are rejected), provisions you by your stable Entra
  Object ID, and issues its own secure session. Sessions are cookie-based (nothing is kept in
  the browser's storage), silently refresh in the background across tabs, carry an 8-hour
  absolute cap, and can be revoked server-side instantly.
- **The FastAPI control-plane backend lands (Phase 1 foundation).** A new `backend/`
  Python service (FastAPI, async SQLAlchemy 2.0 + asyncpg, Alembic, PostgreSQL) begins the
  incremental, strangler-fig replacement of the Express portal backend (ADR-0001). This first
  cut is the foundation scaffold: a typed fail-first `Settings`, the app factory with
  security headers + credentialed CORS, boundary exception handlers, the v1 router with a
  `/health` database-liveness probe, UUIDv7 / timestamp / user-scope DB mixins, the initial
  Alembic migration (pgcrypto + pgvector extensions), and an Azure Blob object-storage service
  (owner-scoped keys, no-SAS server-proxy default, managed-identity user-delegation SAS).
- **The citizen-developer app + data plane is live (control-plane completion).** You can now
  describe an app in chat and get a real, running app: the FastAPI backend provisions it,
  persists its data as per-app records in PostgreSQL (create / list / search / edit / delete),
  stores and parses per-app file uploads, and serves the generated app in a sandboxed frame
  with a built-in data client — so a freshly generated app shows its empty state instead of a
  "failed to fetch" flash. Admin governance (submit / approve / disable), conversations and
  message history, the Claude chat relay (via Azure Foundry), attachments, per-user usage and
  daily-token limits, and feedback all now run on the FastAPI control-plane, RBAC-gated and
  audited.
- **The backend can sign in to Postgres with Microsoft Entra (managed identity).** In
  production, the FastAPI control-plane can now connect to an Azure Database for PostgreSQL
  Flexible Server using a short-lived Microsoft Entra token in place of a stored database
  password — set `DB_AUTH_MODE=entra` and the app fetches the token via its managed identity on
  every connection, over verify-full TLS. Local development and tests are unchanged (the default
  `password` mode keeps using the Docker Postgres), so there is no database secret to store or
  rotate in production.

### Changed
- **The portal login is now Microsoft-only.** The username/password form is replaced by a
  single "Sign in with Microsoft" button, and the app shell reads the signed-in profile from
  the backend rather than from a stored token. The legacy Express password login is retired
  behind a `PASSWORD_LOGIN_ENABLED` gate (default enabled) so it can be switched off the moment
  Entra is verified live in production, then removed in a later step — no lockout risk during
  the cutover.
- **The changelog and product version moved to the repo root.** As the platform grows past the
  single `portal/` app to include the `backend/` control-plane, this changelog moved from
  `portal/CHANGELOG.md` to `CHANGELOG.md`, and the product version of record now lives in a
  root `VERSION` file. This is the first release tracked at the root, continuing the line from
  `1.4.9` → `1.5.0`. (The backend service keeps its own component version, `0.1.0`, in
  `backend/pyproject.toml` — the product version and the backend's API-maturity version are
  deliberately separate axes.)
- **The portal is now a static SPA behind nginx.** With the Express backend gone, the
  React/Vite portal ships as static files served by nginx, which proxies the API surface to
  the FastAPI control-plane. One less moving part, and no Node server in the portal image.
- **The backend API layer now follows one consistent shape (internal refactor).** Every domain
  keeps its routes and its request/response models side by side on one shared camelCase base,
  every route documents the errors it can return in the OpenAPI contract, and error responses
  are raised through a single path. No response body or status code changed — the wire contract
  the portal and generated apps depend on is byte-for-byte identical, locked by characterization
  tests.

### Removed
- **The Express / Node / Cosmos POC backend is retired.** `portal/server.js`, all of
  `portal/server/`, the Vercel Claude proxy, the Cosmos/Mongo operational scripts, and the
  Express-era single-container Docker setup are gone. The FastAPI control-plane + PostgreSQL
  fully replace them — the portal no longer runs any Node backend.

### Security
- Backend-owned OIDC as the relying party (no trusted proxy-asserted identity): a pinned HS256
  session-JWT algorithm that rejects `alg=none`, SHA-256-hashed refresh tokens with strict
  single-use rotation and family-based reuse detection, a `token_version` instant-revocation
  lever, environment-aware `__Host-`/`__Secure-` cookies, and signed double-submit CSRF
  protection on state-changing requests (ADR-0007).

## [1.4.9] - 2026-06-26

### Added
- **You can now attach PowerPoint decks (`.pptx`) to a chat.** Plan, App Builder, and
  the general assistant accept a `.pptx` alongside images, PDF, Word, and Excel, and the
  assistant reads the deck as a visual document — slides, layout, and charts and all — so
  you can ask it to summarize, critique, or build an app from a presentation. You can also
  attach a deck at the "Generate App" step so the very first build turn can reason over it.
  The original `.pptx` is what's stored and re-downloaded, and decks count toward the same
  per-conversation attachment cap as other files. (Available when the deck feature is
  enabled on the server with a reachable conversion sidecar; when it's off, `.pptx` simply
  isn't offered, with a clear message.)
- **The deck renderer now ships in one container for single-slot hosts.** The portal API/SPA
  and the Gotenberg/LibreOffice renderer build into a single image (`Dockerfile.appservice`)
  that run together on loopback with only the portal port exposed, so the deck feature can
  deploy to one-container platforms (Azure App Service for Containers) without a separate
  sidecar. The packaging is proven by the repo's first committed Playwright e2e suite, which
  drives a real browser through attach `.pptx` → assistant reads it → download the original,
  against both the dev stack and the built container (`npm run e2e` / `npm run e2e:container`).

### Changed
- **The "Generate App" file picker now accepts the same files as chat.** It previously took
  only spreadsheets (`.xlsx`/`.xls`/`.csv`/`.tsv`); it now accepts images, PDF, Word, Excel,
  and (when enabled) PowerPoint, and the files you pick feed the first generation turn
  directly instead of being flattened into pasted text.

### Fixed
- **App Builder's live preview no longer fails its data calls in local development.** The dev
  server was answering the preview's cross-origin preflight itself and blocking the request;
  it now lets the app server handle it, matching how production already behaves.

## [1.4.8] - 2026-06-25

### Fixed
- **Generated apps no longer advertise Word support their file picker won't accept.** A spreadsheet
  dashboard could show "Word (.docx) supported" in its upload error while its picker only took
  Excel/CSV — confusing when you tried to add a Word file. App-generation guidance now keeps each
  app's file-picker `accept` and its on-screen "supported types"/rejection message in sync with
  what the app actually handles, and documents that Word files can be stored (`docx` was already
  accepted server-side). Word parsing and storage themselves were never broken.

### Removed
- The "Empowering airport staff…" tagline on the login page.
- The "Featured Demo / RideLink BLR" sample card on the Sandbox start screen.

## [1.4.7] - 2026-06-25

### Fixed
- **App Builder now shows the AI's reply right away — no page refresh needed.** When a build
  prompt was answered with clarifying questions instead of an app (no code generated), the
  reply was saved but never rendered, so the chat looked empty until you reloaded the page.
  The Builder now appends the assistant's reply to the conversation as soon as generation
  finishes (the live preview still renders any generated app; the code block is stripped from
  the chat bubble as before). It also no longer claims "Your app is ready" over an empty
  preview when the model only asked questions.

## [1.4.6] - 2026-06-25

### Added
- **Generated apps can now store Word (`.docx`) files, not just Excel/CSV.** Word documents
  are accepted by the per-app file store (`BIALData.uploadFile`), so an app can keep an
  uploaded `.docx` in object storage alongside its record data and re-open it later to read
  its text — the same store-and-reparse flow that already worked for spreadsheets. Word is
  parsed by mammoth inside the sandboxed parse worker, served back with `nosniff` + a
  locked content CSP, exactly like the existing `.xlsx` path.

## [1.4.5] - 2026-06-24

### Added
- **Generated apps can turn an uploaded spreadsheet into a dashboard.** A deployed or
  preview app can now hand an uploaded Excel (`.xlsx`/`.xls`), CSV, or Word (`.docx`) file
  to the platform to be parsed — spreadsheets come back as structured rows, with the list
  of worksheet names so the app can offer a sheet picker; Word comes back as text — and
  render KPI cards, charts, and sortable tables from it. A view-only app parses for the
  session and keeps nothing; nothing is stored unless the app explicitly saves it. Reached
  through the injected `BIALData.parseFile(...)` client (a fresh file, or a previously
  uploaded one by id). PDF parsing is a planned fast-follow.
- **Real charts in generated apps.** The sanctioned Recharts charting library is now
  available inside every app sandbox, so dashboards render proper bar / line / grouped /
  stacked charts instead of hand-drawn SVG.

### Changed
- **Builder guidance for parsing and charts.** The app builder now knows to parse files via
  `BIALData.parseFile` (never a hand-rolled or CDN parser, and never assuming a global like
  `XLSX`), to offer worksheet and column selection where useful, and to draw charts with
  the Recharts global.

### Security
- **Untrusted uploaded files are parsed under strict server-side limits.** Parsing runs in
  an isolated worker thread with a hard wall-clock time budget and a memory ceiling, behind
  file-size, decompressed-size (zip-bomb), and row/column caps — an oversized or malicious
  file is rejected or truncated cleanly rather than exhausting the server, and a bomb can't
  slip through by being relabelled. The chart library is served through the sandbox's
  existing script allowlist with no change to the network/image rules that keep an app's
  session token from leaking.

## [1.4.4] - 2026-06-24

### Added
- **Attach Word and Excel files in chat.** You can now drop a `.docx` or `.xlsx` into any of
  the three chat surfaces (App Plan, Build, and BIAL Chat) alongside images, PDFs, and
  CSV/TXT. The document's text and the spreadsheet's sheets are read so the AI can answer
  questions about them, build from them, or summarise them. The original file stays attached
  as a chip you can click to download, byte-for-byte. Up to 4 MB per file. Legacy `.doc`
  files are politely declined with a "save as .docx" message.

### Changed
- **Large spreadsheets are handled gracefully.** Each sheet now sends up to 1,000 rows to the
  AI (raised from 200), so real rosters and schedules come through whole. If a sheet is still
  larger, the attachment is marked "truncated" and hovering the chip tells you exactly what
  was shortened — for example "first 1,000 of 2,300 rows" — while the file you download stays
  complete.

## [1.4.3] - 2026-06-24

### Fixed
- **No more surprise sign-outs on a brief hiccup.** When the app refreshed your session
  in the background, a momentary network blip, a rate-limit, or a transient server error
  could wrongly sign you out and bounce you to the login screen with "session expired" —
  even though your session was still valid. The app now signs you out only on a real
  authentication failure; transient errors keep you signed in and retry quietly.

### Changed
- **Steadier background session refresh.** After a transient refresh failure the app now
  waits briefly before trying again instead of retrying on every click — which, when many
  pilot users share one network, was making the rate-limiting worse. Each fail-open event
  is now logged to the browser console so session issues are easier to diagnose.

## [1.4.2] - 2026-06-24

### Added
- **Apps can now keep files, not just records.** A generated app can store an uploaded
  file or a file it produces (for example a reconciliation report), then list it,
  download it to your device, or re-open it inside the app later. Files survive a page
  refresh and are scoped to the app. Supported types: CSV, Excel (xlsx/xls), JSON, text,
  PDF, and common images (PNG/JPEG/GIF/WebP), up to roughly 18 MB per file.
- **Admin file visibility and cleanup.** Admins can see each app's file count and storage
  use, clear an app's files, and recompute the usage counters if they ever drift. Deleting
  an app also removes its stored files.

### Changed
- **Builder guidance for files.** The app builder now knows when to keep a file versus keep
  records, shows the worked reconciliation-report pattern, and warns that an app holding
  sensitive files must require sign-in and IT security review before go-live.
- **Runtime download support.** Deployed apps and the live preview can trigger a file
  download and render stored images inline, without widening what the sandbox can reach.

### Fixed
- Hardened the two-store file writes so a failed upload or delete no longer leaves an
  orphaned file or a wrong usage counter; cleanup and counter-recompute are race-safe.
- File lists now query against a matching database index, avoiding a slow or failing path
  on the production database.
- A generated file download over a non-secure URL now safely falls back to the in-app proxy.

## [1.4.1] - 2026-06-23

### Added
- **Pilot (POC) notice on the home screen.** A short banner now states this is an
  early proof-of-concept and that apps and data are for demonstration only and may
  change or reset, so first-time users know what to expect.

### Changed
- **The daily AI token counter is now easy to see.** It moved from tiny grey text to
  a clear status chip showing `used / limit` that turns amber as you near the limit
  and red when it's used up. It still reads your live usage and resets at midnight IST.
- **Clearer "Plan with AI" vs "Build an App".** The App Builder now explains that
  Plan with AI scopes your requirements in a guided chat first (no code yet), while
  Build an App jumps straight to a working draft.
- **Honest global search.** The search box no longer advertises apps it can't find —
  it now reads "Search pages or actions…" to match what it actually searches.

### Removed
- **Removed the non-functional "Data Source" dropdown and "Backend Schema" toggle**
  from the build sandbox. They connected to no real system, so they are gone, along
  with the misleading help text that claimed the portal connects to AODB, FIDS, and
  other airport systems. File upload, the Theme picker, and saved app data are
  unchanged.
- **Removed the meaningless role label** ("User") shown under the home-screen greeting.

## [1.4.0] - 2026-06-23

### Added
- **Build real, data-backed apps and deploy them to a shareable link.** The App
  Builder now generates working tools (like a Gate Inspection Log) that save records
  to a shared, per-app data store instead of holding everything in the browser.
  Start from a prompt or seed from an uploaded CSV, then **Submit for deployment**;
  once an admin approves, the app is served at its own `/apps/:id` URL. Apps can
  require your BIAL portal sign-in, and what you save persists and is shared with
  other signed-in users.
- **Search, filter, and page through your records.** Generated apps now include a
  search box that matches across every field, per-field filters (e.g. show only
  Status = Fail), and page-number pagination with a live total count. These are
  powered by a shared data API and the App Builder wires them in automatically, so
  apps stay fast even as the record count grows — no more loading every row into the
  browser to search or sort.
- **Admin App Registry.** Admins can review and approve or reject submitted apps,
  turn each app's sign-in requirement on or off, disable or delete an app, clear its
  data, and read a full audit trail of who created, changed, or deleted records.

### Security
- **Strict per-app data isolation.** Every record read and write is scoped to its
  own app, so one app can never see or change another app's data — even if someone
  guesses a record ID. Per-app storage quotas and request rate limits are enforced.
- **Hardened app sandbox.** Deployed apps and the live preview run in an
  opaque-origin sandboxed frame that cannot read your portal session. A scoped
  content-security-policy blocks any off-origin leak of the short-lived access token,
  native form submissions can't smuggle it out, and the long-lived refresh token is
  never handed to an app.

### Fixed
- **Record search and lists work on the deployed (Azure Cosmos DB) database, not
  just locally.** Record search now sorts on a single field, and the per-app
  list/search reads ship the tenant-scoped composite indexes Cosmos requires — it
  rejects a multi-field sort, or a filtered-and-sorted read with no matching index,
  with a 400 (the same constraint that broke chat history in 1.3.1–1.3.3). The
  indexes are created automatically on server start and can be applied to a running
  deployment with `node scripts/ensure-indexes.js`.
- **Sign-in works in deployed data-backed apps.** The app page now signs you in with
  the shared BIAL login and hands the running app a ready session (your identity is
  available to the app, never your password), so apps no longer try — and fail — to
  log in from inside their sandbox. The App Builder also stops generating a redundant
  in-app login form, and any older app that still has one now skips it automatically.

## [1.3.3] - 2026-06-23

### Fixed
- **Opening a conversation now actually loads its messages on the deployed app.** A
  live probe against the Cosmos account showed it serves only single-field ORDER BY
  — any multi-field sort (`{seq, createdAt}`, `{seq, createdAt, _id}`) returns the
  same 400, even with a matching compound index, which is why 1.3.1/1.3.2 did not
  fully resolve it. Messages now sort by `seq` alone; `seq` is a unique, monotonic
  per-conversation counter (user = N, assistant = N+1) so it fully orders messages
  with no tiebreak, and the matching index drops to `{conversationId, username, seq}`.

## [1.3.2] - 2026-06-23

### Fixed
- **Opening a chat or App Builder conversation works on the deployed app.** The
  1.3.1 indexes fixed the conversation list, but loading a single conversation's
  messages still failed with the same Cosmos 400 because the message read sorted by
  `_id` as a final tiebreak — and Azure Cosmos DB for MongoDB will not serve an
  ORDER BY that includes `_id`, even with the index present. Messages now sort by
  `{seq, createdAt}` and the matching index drops `_id`, so the read is served.

## [1.3.1] - 2026-06-23

### Fixed
- **Chat and App Builder history loads again on the deployed app.** On Azure Cosmos
  DB, listing your conversations and opening a chat were failing with a 400 error
  because the database had no composite index to serve those sorted, filtered
  reads (it worked locally, where the database does not require one). The required
  indexes are now created automatically on server start, so a fresh deployment
  fixes itself. To unblock a running deployment without redeploying, run
  `node scripts/ensure-indexes.js`.

## [1.3.0] - 2026-06-22

### Added
- **Your chats, generated apps, and uploaded files now follow you across browsers
  and devices.** Planning chats, App Builder sessions, the generated app code, and
  attachments are saved to your account on the server instead of only in this
  browser. Sign in on another machine and your recent work is already there;
  clearing your browser no longer loses anything.
- **Image and PDF attachments are kept in cloud object storage.** Attachment files
  live in a dedicated object store (Azure Blob Storage in production, or any
  S3-compatible store) and are served back through an authenticated, per-user link,
  so your files are only ever readable by you. Small text files (CSV/TXT) travel
  inline with the message. Supported uploads: PNG, JPEG, GIF, WebP, and PDF, up to
  4 MB each, with a 50 MB per-user total; unsupported files are rejected with a
  clear message.

### Changed
- **Conversations and generated code load from the server.** The App Builder and
  chat history, message order, and the latest generated app preview are read from
  the server on every open and refresh, replacing the previous browser-only
  storage. Signing out clears your local session while your work stays safe on the
  server.

## [1.2.0] - 2026-06-19

### Fixed
- **No more surprise logouts while navigating.** The route guard now silently
  refreshes an expired access token (using the still-valid 7-day refresh token)
  before redirecting, instead of bouncing you to the login screen the moment the
  15-minute access token lapsed mid-session. A transient network error during the
  refresh no longer wipes your session either — only a genuine auth failure signs
  you out, so a brief connectivity blip lets the next action retry.

### Removed
- **Deploy feature removed.** The non-functional "Deploy App" button — and its
  mock deploy page/route — is gone, along with the related Help Center FAQ, the
  "Understanding Deployment" section, and the deploy references in the App Builder
  copy.
- **Login "Contact IT Support Desk" link removed**, as it pointed at a
  non-functional destination.

### Changed
- **Consistent "Plan with AI" naming.** The App Builder sandbox's planning toggle
  is now labelled "Plan with AI" (was "Chat & Plan"), matching the hero and
  history CTAs. The duplicate "Plan with AI" button in the workspace empty state
  was removed, since the hero card above it already offers the same action.

## [1.1.1] - 2026-06-19

### Changed
- **BIAL Chat is temporarily hidden.** The general-assistant chat no longer
  appears in the top nav, the search dropdown, or the dashboard, and the
  dashboard reflows cleanly around the single remaining App Builder card. This is
  a temporary suppression behind a single flag — the `/chat` pages still work by
  direct URL and the feature can be restored in one line.
- **BIAL pilot users now get memorable temporary passwords.** The pilot seed sets
  each user's password to `<LastName>BIAL@123` (e.g. `FernandezBIAL@123`) instead
  of a random string, and every run now also resets existing users' passwords to
  this value — so missing users are created and existing ones refreshed in a
  single pass. Passwords are still stored only as Argon2id hashes. The redundant
  `--rotate` flag was removed; `--dry-run` still previews without writing.

## [1.1.0] - 2026-06-18

### Added
- **Send feedback from anywhere.** A "Feedback" button in the header opens a modal
  with a single text box; submitting stores the message tagged with who sent it,
  when, and which page they were on, then confirms with a toast.
- **Review feedback in Admin.** A read-only "Feedback" tab in the Admin console
  lists submissions newest-first (user, message, page, time), visible to admins only.

### Changed
- New required setting `MONGODB_FEEDBACK_COLLECTION` plus a pre-created Cosmos
  `feedback` collection are needed before deploy. Local dev (docker `mongo:7`)
  auto-creates the collection on first write, so only the env var is needed locally.
