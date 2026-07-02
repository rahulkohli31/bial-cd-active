import { test as setup, expect } from '@playwright/test'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const dirname = path.dirname(fileURLToPath(import.meta.url))
const AUTH_FILE = path.join(dirname, '../playwright/.auth/user.json')

// Cookie-session model (Entra ID via the FastAPI control-plane): the SPA holds no
// tokens — auth derives from a once-cached GET /auth/me over an HttpOnly session
// cookie. With no live tenant in CI (KD-9) we cannot obtain a REAL cookie session,
// so we MOCK /auth/me to admit a QA user and persist the resulting storageState.
//
// NOTE (KD-8 / KD-10): the reused-context data-API specs (deck-*) also call
// Express endpoints (chat/attachments) that, after the Bearer→cookie cutover, 401
// until each API migrates to the cookie session. So beyond this sign-in boundary,
// the authenticated data-flow e2e is gated on the FastAPI edge routing (KD-8) and
// the per-API migrations (KD-10); those specs are expected to be revisited then.
setup('seed a mocked cookie session (no live tenant)', async ({ page }) => {
  const email = process.env.E2E_QA_EMAIL
  if (!email) throw new Error('E2E_QA_EMAIL not set — copy .env.e2e.example to .env.e2e and fill it.')

  // Fulfil the session-context probe with a QA profile so RequireAuth admits.
  await page.route('**/api/v1/auth/me', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'e2e-qa', email, display_name: 'E2E QA' }),
    })
  })

  // A guarded route no longer bounces to /login once /auth/me resolves a user.
  await page.goto('/dashboard')
  await expect(page).toHaveURL(/\/dashboard/)

  await page.context().storageState({ path: AUTH_FILE })
})
