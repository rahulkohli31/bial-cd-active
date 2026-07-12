import { test, expect, type Page } from '@playwright/test'

/**
 * The mid-session suspension gate, and — just as importantly — the 403s that are NOT it.
 *
 * The interceptor matches on `403` AND the exact body `{"detail":"Account suspended"}`.
 * Matching on status alone would swallow CSRF failures and the super-admin gate, both of
 * which their callers must still handle. The negative cases below are the ones that catch an
 * over-broad interceptor; the positive one only proves the happy path.
 *
 * Everything is stubbed with `page.route`, so this spec needs no live tenant and no
 * control-plane — only the SPA.
 */

const json = (status: number, body: unknown) => ({ status, contentType: 'application/json', body: JSON.stringify(body) })
const SUSPENDED_BODY = { detail: 'Account suspended' }

/** Admit the QA user so RequireAuth renders a guarded route. */
async function mockSession(page: Page, { isAdmin = false } = {}) {
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill(json(200, { id: 'e2e-qa', email: process.env.E2E_QA_EMAIL ?? 'qa@example.com', display_name: 'E2E QA', is_admin: isAdmin })),
  )
}

test('a 403 "Account suspended" on any authed call bounces to login with the suspension banner', async ({ page }) => {
  // Model the real thing: once an admin suspends the user, EVERY authed route 403s —
  // including /auth/me, which is how the login page knows not to bounce them onward.
  let suspended = false
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill(
      suspended
        ? json(403, SUSPENDED_BODY)
        : json(200, { id: 'e2e-qa', email: 'qa@example.com', display_name: 'E2E QA', is_admin: false }),
    ),
  )
  // The projects index fires GET /api/projects on mount. Suspend the user there.
  await page.route('**/api/projects**', (route) => {
    suspended = true
    return route.fulfill(json(403, SUSPENDED_BODY))
  })

  await page.goto('/projects')

  await expect(page).toHaveURL(/\/login\?authError=account_suspended/)
  // The banner is deliberately non-alarming and points at an administrator — the user is not
  // at fault, and the word "suspended" never reaches them.
  await expect(page.getByText(/paused by an administrator/i)).toBeVisible()
  await expect(page.getByText('Sign-in failed. Please try again.')).toHaveCount(0)
})

test('a 403 "CSRF check failed" does NOT redirect — the caller handles its own 403', async ({ page }) => {
  await mockSession(page)
  await page.route('**/api/projects**', (route) => route.fulfill(json(403, { detail: 'CSRF check failed' })))

  await page.goto('/projects')

  // Still on /projects, still signed in. The page shows its own error state.
  await expect(page.getByText(/Couldn’t load your projects/i)).toBeVisible()
  await expect(page).toHaveURL(/\/projects/)
  await expect(page).not.toHaveURL(/authError=account_suspended/)
})

test('a 403 "Super-admin privileges required." does NOT redirect, and its message is shown', async ({ page }) => {
  // A super-admin whose gate check fails server-side must see the server's own words, not a
  // blank error and not a suspension redirect. This is the interceptor's discrimination test
  // seen from the UI.
  await mockSession(page, { isAdmin: true })
  await page.route('**/api/admin/users**', (route) => route.fulfill(json(403, { detail: 'Super-admin privileges required.' })))

  await page.goto('/admin')
  const usersTab = page.getByRole('button', { name: /^users/i })
  if (await usersTab.count()) await usersTab.first().click()

  await expect(page.getByTestId('users-load-error')).toHaveText('Super-admin privileges required.')
  await expect(page).toHaveURL(/\/admin/)
  await expect(page).not.toHaveURL(/authError=account_suspended/)
})

test('a citizen developer is gated by the SPA before it ever calls the admin API', async ({ page }) => {
  // RBAC is enforced at the API (.claude/rules/security.md); this client gate is an
  // affordance, not the enforcement. It must still not crash or look like a suspension.
  await mockSession(page, { isAdmin: false })
  let adminCalls = 0
  await page.route('**/api/admin/**', (route) => {
    adminCalls += 1
    return route.fulfill(json(403, { detail: 'Super-admin privileges required.' }))
  })

  await page.goto('/admin')
  await expect(page).not.toHaveURL(/authError=account_suspended/)
  expect(adminCalls).toBe(0)
})
