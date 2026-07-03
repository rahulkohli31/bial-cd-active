import { test, expect } from '@playwright/test'

// Cookie-model sign-in is a full-page hand-off to the FastAPI control-plane, which
// runs the Entra OIDC Authorization-Code + PKCE flow. With no live tenant in CI
// (KD-9), we assert the REDIRECT BOUNDARY: the page offers "Sign in with
// Microsoft" (and NO password field), and clicking it navigates the browser
// toward /api/v1/auth/login. Start logged OUT (drop any storageState).
test.use({ storageState: { cookies: [], origins: [] } })

test('login offers Microsoft sign-in and hands off to /api/v1/auth/login', async ({ page }) => {
  // Intercept the FastAPI hand-off so the browser never actually leaves for
  // login.microsoftonline.com — CI needs no live tenant.
  let handoffUrl = ''
  await page.route('**/api/v1/auth/login', async (route) => {
    handoffUrl = route.request().url()
    await route.fulfill({ status: 200, contentType: 'text/html', body: '<html>stub</html>' })
  })

  const consoleErrors: string[] = []
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })
  page.on('pageerror', (e) => consoleErrors.push(String(e)))

  await page.goto('/login')

  // The POC username/password form is gone.
  await expect(page.getByTestId('login-password')).toHaveCount(0)

  const signIn = page.getByTestId('login-microsoft')
  await expect(signIn).toBeVisible()
  await expect(signIn).toContainText('Sign in with Microsoft')

  await signIn.click()
  await expect.poll(() => handoffUrl).toContain('/api/v1/auth/login')

  expect(consoleErrors, `console errors:\n${consoleErrors.join('\n')}`).toHaveLength(0)
})
