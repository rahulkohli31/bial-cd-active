import { test, expect, type Page, type Request } from '@playwright/test'

/**
 * The project-first journey, against a REAL control-plane.
 *
 * These are the claims that mocked-module unit tests structurally cannot make:
 *
 *  - code continuity across two conversations (the seed is injected server-side),
 *  - provision is idempotent per project (one app, however many chats),
 *  - the SPA never issues a data-service call bearing the wrong project's X-App-Key.
 *
 * Only `/auth/me` is mocked (no live Entra tenant in CI, KD-9); everything else is driven.
 * Run through the portal on :5173, never the backend on :8000 — the refresh cookie is
 * Path-scoped to /api/v1/auth/refresh and will not be sent otherwise.
 */

const PROMPT = 'Build a gate equipment maintenance log with a status table.'

/** Every /api request the page made, in order, so we can assert call ORDERING. */
function recordApiCalls(page: Page): Request[] {
  const calls: Request[] = []
  page.on('request', (req) => {
    if (req.url().includes('/api/')) calls.push(req)
  })
  return calls
}

const indexOfCall = (calls: Request[], method: string, fragment: string) =>
  calls.findIndex((c) => c.method() === method && c.url().includes(fragment))

async function createProject(page: Page, name: string) {
  await page.goto('/projects')
  await page.getByRole('button', { name: /new project/i }).first().click()
  await page.getByPlaceholder(/VIP Movement Tracker/i).fill(name)
  await page.getByRole('button', { name: /create project/i }).click()
  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}/)
  const projectId = page.url().split('/projects/')[1]
  return projectId
}

async function sendBuildTurn(page: Page, text: string) {
  const composer = page.getByPlaceholder(/Type instructions/i)
  await composer.fill(text)
  await composer.press('Enter')
}

test.describe('project-first journey', () => {
  // Every test here drives at least one REAL model turn, and two of them drive two. A turn
  // takes 30-60s on a small app and grows with the artifact, so the suite-wide 90s budget
  // (written when /auth/me was mocked and no turn ever actually ran) cannot fit them.
  test.describe.configure({ timeout: 420_000 })

  test('a first build provisions once, BEFORE its first code write, and the app appears on the project', async ({ page }) => {
    const calls = recordApiCalls(page)
    const projectId = await createProject(page, `E2E Gate Log ${Date.now()}`)

    await page.getByRole('button', { name: /new build chat/i }).click()
    // A brand-new chat carries its project in a transient query — the row does not exist yet.
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}\?projectId=/)

    await sendBuildTurn(page, PROMPT)

    // Once the first append creates the row, conversation.projectId is authoritative and
    // the query is dropped: the address the user copies is the flat one.
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/, { timeout: 90_000 })

    // The URL flattens on the APPEND — provision and the code PATCH are still in flight.
    // Wait for the code write before asserting the ordering, or we race the very calls we
    // are ordering (this assertion read 0 provisions before the wait existed).
    await page.waitForRequest(
      (req) => req.method() === 'PATCH' && req.url().includes('/api/conversations/'),
      { timeout: 180_000 },
    )

    // Exactly ONE provision, and it precedes the first code PATCH. Get this backwards and
    // the backend's `if app is not None` mirror silently drops the first build's code.
    const provisions = calls.filter((c) => c.method() === 'POST' && c.url().includes('/api/apps/provision'))
    expect(provisions).toHaveLength(1)
    const provisionAt = indexOfCall(calls, 'POST', '/api/apps/provision')
    const patchAt = indexOfCall(calls, 'PATCH', '/api/conversations/')
    expect(provisionAt).toBeGreaterThanOrEqual(0)
    expect(patchAt).toBeGreaterThanOrEqual(0)
    expect(provisionAt).toBeLessThan(patchAt)

    // The app is now discoverable by READING the project.
    await page.goto(`/projects/${projectId}`)
    await expect(page.getByText(/no app yet/i)).toHaveCount(0)
  })

  test('a SECOND chat in the project provisions no new app and continues from the existing code', async ({ page }) => {
    const projectId = await createProject(page, `E2E Continuity ${Date.now()}`)

    // Exactly one first-build turn. The PATCH-before-provision bug loses only the FIRST
    // write, so a multi-turn setup would mask it.
    await page.getByRole('button', { name: /new build chat/i }).click()
    await sendBuildTurn(page, PROMPT)
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/, { timeout: 90_000 })
    // The flat URL means the APPEND landed; provision and the code write are still in flight.
    await page.waitForRequest(
      (req) => req.method() === 'PATCH' && req.url().includes('/api/conversations/'),
      { timeout: 180_000 },
    )

    // The app the FIRST chat provisioned.
    const appIdBefore = (await (await page.request.get(`/api/projects/${projectId}`)).json()).appId
    expect(appIdBefore).toBeTruthy()

    // Second chat, same project.
    const calls = recordApiCalls(page)
    await page.goto(`/projects/${projectId}`)
    await page.getByRole('button', { name: /new build chat/i }).click()
    await sendBuildTurn(page, 'Add a column for the last inspection date.')
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/, { timeout: 90_000 })
    await page.waitForRequest(
      (req) => req.method() === 'PATCH' && req.url().includes('/api/conversations/'),
      { timeout: 180_000 },
    )

    // Provision is idempotent per project: at most one call, and it returns the same appId.
    const provisions = calls.filter((c) => c.method() === 'POST' && c.url().includes('/api/apps/provision'))
    expect(provisions.length).toBeLessThanOrEqual(1)

    // R6/R21: the second chat continued from the project's current code rather than a blank
    // slate. NOTE: the builder live-preview is knowingly DARK on release/phase2 — U9 retired
    // the same-origin /preview shell, and the per-session cross-origin sandbox preview lands
    // with the Wave-1 PORTAL-PREVIEW track (C8). So the iframe-src assertion is DEFERRED until
    // then; what still holds and matters here is that the project owns exactly the one app the
    // first chat provisioned, whose code the second chat seeds from.
    // TODO(PORTAL-PREVIEW): restore once the cross-origin sandbox preview lands —
    //   await expect(page.locator('iframe')).toHaveAttribute('src', <sandbox-FQDN pattern>)
    const appIdAfter = (await (await page.request.get(`/api/projects/${projectId}`)).json()).appId
    expect(appIdAfter).toBe(appIdBefore)
  })

  test.fixme('navigating from project A’s build chat to project B’s never carries A’s X-App-Key', {
    annotation: {
      type: 'fixme',
      description:
        'vacuous until a real build can reach preview_ready — re-enable at the first real-ACA integration run (BRAIN wiring landed 2026-07-14; needs a live sandbox env)',
    },
  }, async ({ page }) => {
    const a = await createProject(page, `E2E Iso A ${Date.now()}`)
    await page.getByRole('button', { name: /new build chat/i }).click()
    await sendBuildTurn(page, PROMPT)
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/, { timeout: 90_000 })

    // Learn A's app key from the requests its preview makes.
    // NOTE (release/phase2): the builder live-preview is knowingly DARK until the Wave-1
    // PORTAL-PREVIEW track (U9 retired /preview; C8 cross-origin preview is deferred), so the
    // preview makes no X-App-Key requests and `keysSeen` would stay empty — this isolation check
    // would pass VACUOUSLY, hence the test.fixme above. It becomes real coverage only once the
    // preview is restored against a live sandbox — TODO(PORTAL-PREVIEW).
    const keysSeen = new Set<string>()
    page.on('request', (req) => {
      const key = req.headers()['x-app-key']
      if (key) keysSeen.add(key)
    })
    await page.waitForTimeout(2000)
    const aKeys = new Set(keysSeen)

    const b = await createProject(page, `E2E Iso B ${Date.now()}`)
    await page.getByRole('button', { name: /new build chat/i }).click()
    await sendBuildTurn(page, 'A completely different tool: a baggage reconciliation table.')
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/, { timeout: 90_000 })

    // Every key seen from here on must be B's. A record written under A's key while B's code
    // is on screen lands in the wrong app's store (.claude/rules/security.md).
    keysSeen.clear()
    await page.waitForTimeout(3000)
    for (const key of aKeys) expect(keysSeen.has(key)).toBe(false)
    expect(a).not.toBe(b)
  })

  test('deleting the project names the cascade and sends a bookmarked chat URL back to /projects', async ({ page }) => {
    const name = `E2E Delete ${Date.now()}`
    await createProject(page, name)
    await page.getByRole('button', { name: /new plan chat/i }).click()
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}\?projectId=/)

    const composer = page.getByPlaceholder(/Describe what you're thinking/i)
    await composer.fill('What should this tool do?')
    await composer.press('Enter')
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/, { timeout: 90_000 })
    const chatUrl = page.url()

    await page.goto('/projects')
    // F-10 fixed: the card is a plain container now, so the delete button's accessible name is
    // unambiguous again — no strict-mode double match against an outer role="button".
    await page.getByRole('button', { name: `Delete ${name}` }).click()

    // The dialog states what it destroys, and arms only on an exact name match.
    await expect(page.getByText(/This deletes the project, its app, and all 1 chat\./)).toBeVisible()
    const confirm = page.getByRole('button', { name: /delete project/i })
    await expect(confirm).toBeDisabled()
    await page.getByLabel(/type the project name/i).fill(`${name} `) // trailing space: still no
    await expect(confirm).toBeDisabled()
    await page.getByLabel(/type the project name/i).fill(name)
    await expect(confirm).toBeEnabled()
    await confirm.click()

    // The bookmarked chat went with its project.
    await page.goto(chatUrl)
    await expect(page).toHaveURL(/\/projects$/)
  })

  test('/apps/{appId} is a full-page navigation that leaves the SPA', async ({ page }) => {
    // nginx proxies /apps/ to the backend runner and the Vite dev proxy does not, so an SPA
    // route there would work locally and 404 in the container. Assert no client-side match:
    // the URL survives, and none of the SPA's chrome renders.
    await page.goto('/projects')
    await expect(page.getByRole('heading', { name: 'Projects' })).toBeVisible()

    await page.goto('/apps/00000000-0000-0000-0000-000000000000')
    expect(page.url()).toContain('/apps/')
    await expect(page.getByRole('heading', { name: 'Projects' })).toHaveCount(0)
  })
})
