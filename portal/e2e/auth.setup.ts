import { test as setup, expect } from '@playwright/test'
import type { BrowserContext } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const dirname = path.dirname(fileURLToPath(import.meta.url))
const AUTH_FILE = path.join(dirname, '../playwright/.auth/user.json')

type Cookies = Parameters<BrowserContext['addCookies']>[0]

/**
 * Seed the storageState every spec runs under.
 *
 * Two modes, and which one you get is not a coin toss — it is whether you handed us a REAL
 * session to use.
 *
 * ── Real session (preferred; the only mode that exercises the backend) ──────────────────
 * Auth is a cookie session the FastAPI control-plane mints for ITSELF once the Entra round-trip
 * is done. A browser cannot obtain one without a live tenant — but the cookies are only cookies,
 * and `.mythos/fastapi-e2e/scripts/auth/mint_session.py` issues them for an existing user row
 * using the control-plane's own `mint_session_jwt` / `issue_csrf_token` / `issue_new_family`.
 * No product code is touched; only the Microsoft round-trip is skipped.
 *
 *     cd backend && uv run python ../.mythos/fastapi-e2e/scripts/auth/mint_session.py \
 *       --email <addr> --out ../.mythos/fastapi-e2e/.auth/anant.storage_state.json
 *
 * Point `E2E_STORAGE_STATE` at that file — it lives in a git-ignored tree, which is exactly why
 * this is an env var and not a path baked in here — and every spec drives a real session.
 *
 * ── Mocked session (fallback; CI has neither a tenant nor a database) ───────────────────
 * With no `E2E_STORAGE_STATE` we fulfil `GET /api/v1/auth/me` with a QA profile so `RequireAuth`
 * admits and the SPA renders. Be honest about what that buys: **the browser holds no session
 * cookie, so every other API call is unauthenticated.** Specs that drive real data — projects,
 * chats, provisioning — cannot pass in this mode; they exercise a shell. Only render-level
 * assertions mean anything here.
 *
 * `E2E_REQUIRE_REAL_SESSION=1` turns the fallback into a hard failure rather than a silent
 * downgrade. Use it locally, so a stale storageState never quietly demotes a real run to a
 * mocked one.
 */
setup('seed an authenticated session', async ({ page }) => {
  const statePath = process.env.E2E_STORAGE_STATE
  const requireReal = process.env.E2E_REQUIRE_REAL_SESSION === '1'

  if (statePath && fs.existsSync(statePath)) {
    const minted = JSON.parse(fs.readFileSync(statePath, 'utf8')) as { cookies?: Cookies }
    const cookies = minted.cookies ?? ([] as unknown as Cookies)
    if (cookies.length === 0) throw new Error(`E2E_STORAGE_STATE has no cookies: ${statePath}`)

    await page.context().addCookies(cookies)

    // Prove the session is live against the REAL control-plane before persisting it. A state
    // whose token_version was bumped by a logout still *looks* fine on disk; only the server
    // can say it is dead, and learning that here is far cheaper than watching every spec fail
    // on an auth redirect that masquerades as a product bug.
    const me = await page.request.get('/api/v1/auth/me')
    expect(
      me.status(),
      `The minted session at ${statePath} is not valid (GET /auth/me → ${me.status()}). ` +
        'Logging out bumps token_version and kills every session minted before it — re-run mint_session.py.',
    ).toBe(200)

    await page.goto('/dashboard')
    await expect(page).toHaveURL(/\/dashboard/)
    await page.context().storageState({ path: AUTH_FILE })
    return
  }

  if (requireReal) {
    throw new Error(
      'E2E_REQUIRE_REAL_SESSION=1 but no usable E2E_STORAGE_STATE. Mint one first:\n' +
        '  cd backend && uv run python ../.mythos/fastapi-e2e/scripts/auth/mint_session.py --email <addr> --out <path>\n' +
        '  export E2E_STORAGE_STATE=<path>',
    )
  }

  const email = process.env.E2E_QA_EMAIL
  if (!email) throw new Error('E2E_QA_EMAIL not set — copy .env.e2e.example to .env.e2e and fill it.')

  console.warn(
    '[auth.setup] No E2E_STORAGE_STATE — mocking GET /auth/me. The browser holds NO session ' +
      'cookie, so any spec that drives real data is testing a shell, not the backend.',
  )

  await page.route('**/api/v1/auth/me', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'e2e-qa', email, display_name: 'E2E QA', is_admin: false }),
    })
  })

  await page.goto('/dashboard')
  await expect(page).toHaveURL(/\/dashboard/)
  await page.context().storageState({ path: AUTH_FILE })
})
