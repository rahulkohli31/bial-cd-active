# Test plan — Issue #92: End-User Authentication for Generated Apps

**Branch:** `feat/app-identity-entra` (PR target: `main`)
**Who this is for:** an engineer with a working real-Azure test environment (Sandbox/ACA, Foundry, Redis, Postgres already configured), verifying the PR before merge. Written to be handed to Claude Code directly — every step names an exact command, endpoint, or file.

## What this PR does, in one paragraph

Generated apps previously had no way to know who was using them. This adds a platform-minted, short-lived signed **identity assertion**: the control plane signs it with a dedicated RS256 key pair (never the session-signing secret), naming the person (by Entra object id, never email), one app, and one plane (`preview` or `deployed`). The generated app verifies the assertion **itself** against a published JWKS — the platform never verifies in production. Preview delivery is a `postMessage` handshake with the framed app (never a cookie — cross-site framed cookies are blocked by Safari/Firefox). Deployed delivery reuses the app's single existing Entra redirect URI and rides a short-lived exchange **code** through the redirect, never the assertion itself. The build agent is taught to use the platform's accessor instead of scaffolding its own auth, and an automated check fails the build if a generated app grows an auth surface anyway. The old `login_required` admin toggle is deleted — whether an app needs sign-in is the app's own code's decision now, not a platform switch.

## 0. Setup

```bash
git fetch origin
git checkout feat/app-identity-entra
git rebase origin/main   # this branch should already be rebased before you get it — confirm, don't assume
```

Start your normal backend + frontend dev servers against your real-Azure `.env` (Sandbox/Foundry/Redis/Object Store all configured). Sign in as yourself (real Entra login is fine in a real environment — unlike a sandboxed dev box, this doesn't need session-minting).

## 1. Automated regression (run first, before any manual testing)

```bash
cd backend
ENV_FILE=.env.test uv run pytest -q
```
**Pass criteria:** no NEW failures compared to `main`. (A pre-existing, environment-specific failure count is expected and fine — compare against a `main`-branch run in the same environment if you want a precise diff, don't assume our numbers transfer to your machine.)

Targeted, so you can look closely at just the new code:
```bash
ENV_FILE=.env.test uv run pytest \
  tests/services/auth/test_app_assertion.py \
  tests/api/v1/auth/test_app_assertion_endpoints.py \
  tests/services/build_sessions/test_launch.py \
  tests/services/build_sessions/test_auth_surface.py \
  tests/services/build_sessions/test_manager.py \
  tests/services/orchestrator/test_prompt.py \
  tests/services/agent/test_mode_prompts.py \
  -v
```

```bash
cd portal
npx vitest run
```
**Pass criteria:** no new failures vs `main`.

```bash
cd backend
uv run alembic upgrade head
uv run alembic heads
```
**Pass criteria:** exactly one head, `0025_drop_login_required`.

## 2. Mint + JWKS (R3–R6)

```bash
curl https://<your-backend-origin>/api/v1/auth/jwks
```
**Pass criteria:** a JSON Web Key Set (`{"keys": [...]}`) with exactly one RSA public key. No private-key material anywhere in the response.

Mint a real preview assertion (needs a live session cookie + matching CSRF cookie/header from your browser session, and an `app_id` you own):
```bash
curl -X POST https://<your-backend-origin>/api/v1/auth/app-assertion/preview \
  -H "Content-Type: application/json" \
  -H "x-csrf-token: <csrf token from your browser cookie>" \
  -b "session=<your session cookie>; csrf=<your csrf cookie>" \
  -d '{"app_id": "<a real app id you own>"}'
```
Decode the returned `assertion` JWT (e.g. paste into any JWT decoder, or `python -c "import jwt; print(jwt.decode(TOKEN, options={'verify_signature': False}))"`). **Pass criteria:**
- `aud` = the app id you sent
- `sub` = an Entra object id (a GUID), **not** an email address
- a custom `plane` claim = `"preview"`
- short expiry (~5 minutes from mint time)

## 3. Starting workspace (R19)

Build a brand-new app through the normal chat flow (any simple prompt). Once it's built, inspect the workspace and confirm these files exist and weren't touched by the prompt:
- `lib/bial-identity.ts`
- `lib/bial-identity-client.tsx`
- `components/bial/identity-demo.tsx`
- `app/actions.ts` (has a `touchAppUser` Server Action)
- a Drizzle migration adding an `app_users` table (roster keyed on Entra object id)
- `app/layout.tsx` mounts `<BialIdentityProvider>`

## 4. Preview transport (R7–R9)

Open the app's live preview in the portal builder. Click the **"Who am I?"** button the worked example ships with (`identity-demo.tsx`, on the home page by default).

**Pass criteria:**
- It resolves to your real signed-in name/email within a couple seconds (the `postMessage` handshake completing).
- A row appears in that app's `app_users` table keyed on your Entra object id.
- Open DevTools → Application → Cookies for the sandbox's own origin: **no cookie holding the assertion.** It's in-memory only.
- Reload the preview iframe: identity re-resolves from scratch (proves nothing persisted client-side).

## 5. Deployed transport (R10)

Mark that same app as deployed with a real, reachable URL (admin app-registry panel → Mark deployed). Then, **in a fresh browser tab, not through the portal iframe**, navigate directly to the deployed app's own URL and trigger sign-in (the app should redirect you via `getBialLaunchUrl()` if you built a page that calls it — the worked example's Server Action does this, or you can hit `/api/v1/auth/launch?app_id=<id>&next=/` on the backend directly).

**Pass criteria:**
- You land back on the deployed app signed in, without ever seeing a second Entra app registration or a new-looking consent screen (it's the same, existing Entra registration).
- Open DevTools → Network on the whole redirect chain: the assertion itself never appears in a URL, query string, browser history entry, or `Referer` header — at most a short-lived opaque exchange **code** appears in the URL momentarily.
- The app's own behavior (what it shows, how it calls `getBialIdentity()`) is identical to the preview case — confirm by diffing the app's own source: it should contain zero conditionals branching on preview-vs-deployed.

## 6. Agent instructions + build check (R20–R21)

Start a new build (or continue an existing one) and ask, verbatim: **"add user login with email and password."**

**Pass criteria:** the agent explains sign-in is already handled by the platform and does **not** scaffold a login page, a password field, or add `next-auth`/`@clerk/*`/similar to `package.json`.

Now test the enforcement side deliberately: in a build session, have the agent (or edit directly) add `"next-auth": "^5.0.0"` to the generated app's `package.json` dependencies, or create a file at `app/sign-in/page.tsx`. Trigger a finalize (`declare_done`).

**Pass criteria:** the build **fails**, and the failure reason names the specific finding (e.g. `"a forbidden auth/credential dependency: next-auth"` or `"a self-built auth route: app/sign-in/page.tsx"`) — not a generic/opaque failure.

## 7. `login_required` is fully gone (R22)

```bash
git grep -in "login_required"
```
**Pass criteria:** the only two hits in the whole repo are `backend/alembic/versions/2026_07_06_0010_app_registry.py` (the original creation migration, correctly left as history) and `backend/alembic/versions/2026_08_11_0025_drop_login_required.py` (the migration that removes it). Nothing in `api/`, `schemas.py`, `router.py`, or `portal/src/`.

In the portal's admin app-registry panel, confirm there is no "Require login" toggle or column anywhere in the UI.

## Sign-off checklist

- [ ] Automated backend + portal suites: no new failures vs `main`
- [ ] `alembic heads` → exactly one, `0025_drop_login_required`
- [ ] `GET /api/v1/auth/jwks` returns a valid single-key public JWKS
- [ ] A minted preview assertion has the right `aud`/`sub`/`plane`/TTL
- [ ] New-app template ships the accessor, roster table, and worked example
- [ ] Preview "Who am I?" resolves identity, no cookie, survives a reload
- [ ] Deployed launch flow resolves identity, assertion never in URL/history/Referer, same app code as preview
- [ ] Asking the agent for "login" steers to the platform's accessor, doesn't scaffold auth
- [ ] A deliberately-added auth surface (forbidden dependency or login route) fails the build with a specific, named reason
- [ ] `login_required` is gone everywhere except the two migration files, and the admin UI toggle is gone

## Known, accepted gaps (not blockers — called out in the spec itself)

- The deployed-plane exchange code has a short TTL but isn't tracked for true single-use.
- No seamless in-frame SSO for a visitor already signed into the portal — every gated preview does a full handshake (deliberately deferred by the spec; the portal's session cookie genuinely can't be read cross-origin by the sandbox).
