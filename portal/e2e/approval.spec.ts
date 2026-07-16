import { test, expect, type Page } from '@playwright/test'

/**
 * The pending-approval gate — and the 403s that are NOT it.
 *
 * Mirrors suspension.spec.ts: the interceptor matches on `403` AND the exact body
 * `{"detail":"Pending approval"}`. The negative cases are the ones that catch an
 * over-broad interceptor (mistaking a suspension, CSRF failure, or super-admin
 * gate for pending-approval, or vice versa) — the positive case only proves the
 * happy path.
 *
 * Everything is stubbed with `page.route`, so this spec needs no live tenant and
 * no control-plane — only the SPA.
 */

const json = (status: number, body: unknown) => ({ status, contentType: 'application/json', body: JSON.stringify(body) })
const PENDING_BODY = { detail: 'Pending approval' }
const SUSPENDED_BODY = { detail: 'Account suspended' }

async function mockPendingSession(page: Page) {
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill(json(200, { id: 'e2e-qa', email: 'qa@example.com', display_name: 'E2E QA', is_admin: false, status: 'pending' })),
  )
}

test('a pending user sees the awaiting-approval screen, not a redirect', async ({ page }) => {
  await mockPendingSession(page)

  await page.goto('/dashboard')

  // Not a redirect: the user IS authenticated, just not yet authorized — the
  // URL stays exactly where they landed, unlike the unauthenticated case.
  await expect(page).toHaveURL(/\/dashboard/)
  await expect(page.getByText(/awaiting approval/i)).toBeVisible()
  await expect(page.getByTestId('awaiting-approval-signout')).toBeVisible()
})

test('a stray 403 "Pending approval" on a live call does not get mistaken for suspension', async ({ page }) => {
  // Approved on /auth/me (so the page renders normally), but a specific data
  // call 403s "Pending approval" — the discriminating negative case, mirrored
  // from suspension.spec.ts's own CSRF/super-admin negative cases.
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill(json(200, { id: 'e2e-qa', email: 'qa@example.com', display_name: 'E2E QA', is_admin: false, status: 'approved' })),
  )
  await page.route('**/api/projects**', (route) => route.fulfill(json(403, PENDING_BODY)))

  await page.goto('/projects')

  // handlePendingSession() hard-redirects to /dashboard (proving the pending
  // interceptor actually fired) — must NOT land on the suspension banner (that
  // would mean the interceptor matched on status alone rather than the exact body).
  await expect(page).toHaveURL(/\/dashboard/)
  await expect(page).not.toHaveURL(/authError=account_suspended/)
})

test('a 403 "Account suspended" is never mistaken for pending approval', async ({ page }) => {
  // Mirrors suspension.spec.ts's own mock exactly: /auth/me must ALSO flip to the
  // suspended 403 once suspension kicks in — otherwise, after handleSuspendedSession's
  // hard redirect to /login, LoginPage's own "already signed in" check (a fresh
  // bootstrapSession() call on the reloaded page) would see a still-"approved" user
  // and immediately forward them back to /dashboard, masking the real assertion.
  let suspended = false
  await page.route('**/api/v1/auth/me', (route) =>
    route.fulfill(
      suspended
        ? json(403, SUSPENDED_BODY)
        : json(200, { id: 'e2e-qa', email: 'qa@example.com', display_name: 'E2E QA', is_admin: false, status: 'approved' }),
    ),
  )
  await page.route('**/api/projects**', (route) => {
    suspended = true
    return route.fulfill(json(403, SUSPENDED_BODY))
  })

  await page.goto('/projects')

  // The suspension interceptor still owns this body — bounces to /login with
  // the (correct) suspension banner, never the awaiting-approval screen.
  await expect(page).toHaveURL(/\/login\?authError=account_suspended/);
  await expect(page.getByText(/paused by an administrator/i)).toBeVisible()
  await expect(page.getByText(/awaiting approval/i)).toHaveCount(0)
})
