import { test, expect, type Page, type Request } from '@playwright/test'

/**
 * The project-first journey, against a REAL control-plane.
 *
 * These are the claims that mocked-module unit tests structurally cannot make:
 *
 *  - code continuity across two conversations (the seed is injected server-side),
 *  - the project's app is minted once, however many chats build in it.
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

  // ROTTED — these two encode the retired per-send build-chat model and have been red since well
  // before plan 003 touched them: both drive a "New build chat" button that no longer exists
  // (`ProjectPage.test.tsx` asserts its absence), and both wait on a `PATCH /api/conversations/`
  // code write that no production code path makes any more (`patchBuildCode` has zero callers).
  // Neither failure is reachable past the first click, so neither has been proving anything.
  //
  // The journey they were written for — build from the project composer, land in a chat, iterate —
  // is now `thread-lifecycle.spec.ts`, against the canonical thread. What they uniquely covered and
  // it does NOT is that a build mints the project's app exactly once; restoring that needs a
  // rewrite onto the thread model plus a decision about `current_code`'s dead writer, which is
  // follow-up work, not a rename. Marked rather than deleted so that stays visible.
  //
  // U6 NOTE: the client-side `POST /api/apps/provision` these two once ordered against no longer
  // exists — the app row is minted SERVER-SIDE inside the build session, so there is no browser
  // request to count or order. The claim that survives is the observable one: reading the project
  // shows exactly one app, the same one across chats.
  test.fixme('a first build mints the project app, which then appears on the project', async ({ page }) => {
    const calls = recordApiCalls(page)
    const projectId = await createProject(page, `E2E Gate Log ${Date.now()}`)

    await page.getByRole('button', { name: /new build chat/i }).click()
    // A brand-new chat carries its project in a transient query — the row does not exist yet.
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}\?projectId=/)

    await sendBuildTurn(page, PROMPT)

    // Once the first append creates the row, conversation.projectId is authoritative and
    // the query is dropped: the address the user copies is the flat one.
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/, { timeout: 90_000 })

    // The URL flattens on the APPEND — the server-side mint and the code PATCH are still in
    // flight. Wait for the code write before reading the project, or we race it.
    await page.waitForRequest(
      (req) => req.method() === 'PATCH' && req.url().includes('/api/conversations/'),
      { timeout: 180_000 },
    )
    // The browser never provisions: the app row is minted inside the build session (U6).
    expect(calls.filter((c) => c.url().includes('/api/apps/provision'))).toHaveLength(0)

    // The app is now discoverable by READING the project.
    await page.goto(`/projects/${projectId}`)
    await expect(page.getByText(/no app yet/i)).toHaveCount(0)
  })

  test.fixme('a SECOND chat in the project mints no new app and continues from the existing code', async ({ page }) => {
    const projectId = await createProject(page, `E2E Continuity ${Date.now()}`)

    // Exactly one first-build turn. The PATCH-before-provision bug loses only the FIRST
    // write, so a multi-turn setup would mask it.
    await page.getByRole('button', { name: /new build chat/i }).click()
    await sendBuildTurn(page, PROMPT)
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/, { timeout: 90_000 })
    // The flat URL means the APPEND landed; the mint and the code write are still in flight.
    await page.waitForRequest(
      (req) => req.method() === 'PATCH' && req.url().includes('/api/conversations/'),
      { timeout: 180_000 },
    )

    // The app the FIRST chat's build session minted.
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

    // The browser never provisions (U6); idempotence is proven by the appId below, unchanged.
    expect(calls.filter((c) => c.url().includes('/api/apps/provision'))).toHaveLength(0)

    // R6/R21: the second chat continued from the project's current code rather than a blank
    // slate. NOTE: the builder live-preview is knowingly DARK on release/phase2 — U9 retired
    // the same-origin /preview shell, and the per-session cross-origin sandbox preview lands
    // with the Wave-1 PORTAL-PREVIEW track (C8). So the iframe-src assertion is DEFERRED until
    // then; what still holds and matters here is that the project owns exactly the one app the
    // first chat's build minted, whose code the second chat seeds from.
    // TODO(PORTAL-PREVIEW): restore once the cross-origin sandbox preview lands —
    //   await expect(page.locator('iframe')).toHaveAttribute('src', <sandbox-FQDN pattern>)
    const appIdAfter = (await (await page.request.get(`/api/projects/${projectId}`)).json()).appId
    expect(appIdAfter).toBe(appIdBefore)
  })

  test('deleting the project names the cascade and sends a bookmarked chat URL back to /projects', async ({ page }) => {
    const name = `E2E Delete ${Date.now()}`
    await createProject(page, name)

    // A planning chat is minted from the project composer's Plan mode — the standalone
    // "new plan chat" button this used to click was removed with the project-first consolidation.
    // (Planning still mints per send; only BUILD sends route into the canonical thread.)
    await page.getByRole('button', { name: /plan with ai/i }).click()
    await page.getByPlaceholder(/I'll help you plan it out/i).fill('What should this tool do?')
    await page.getByRole('button', { name: /start planning/i }).click()
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}\?projectId=/)

    // The first append creates the row and the transient query drops — that is what proves the
    // chat is real, and the cascade below is about a real row.
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/, { timeout: 90_000 })
    const chatUrl = page.url()

    await page.goto('/projects')
    // F-10 fixed: the card is a plain container now, so the delete button's accessible name is
    // unambiguous again — no strict-mode double match against an outer role="button".
    await page.getByRole('button', { name: `Delete ${name}` }).click()

    // The dialog states what it destroys — including the project's own database, which is
    // the half with no undo — and arms only on an exact name match.
    await expect(page.getByText(/This deletes the project, its app, and all 1 chat\./)).toBeVisible()
    await expect(
      page.getByText(/The database and files behind the app are destroyed permanently\./),
    ).toBeVisible()
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

/**
 * Two findings from agc129's review of #86 (the description editor pop-up) that are
 * structurally invisible to Vitest, so they need a real browser:
 *
 *  - jsdom never blurs a disabled element, so the busy-state focus collapse that broke
 *    Tab-containment and Escape (405a1d6) cannot reproduce there.
 *  - jsdom has no layout/paint engine (and ProjectPage.test.tsx stubs Navbar to null), so
 *    the overlay-renders-beneath-the-sticky-navbar bug cannot reproduce there either.
 *
 * Neither test drives a real model turn — no build needed for either check — so they run
 * under the suite's default 90s timeout rather than the 420s one above.
 */
test.describe('description editor — keyboard focus + stacking (#86 review)', () => {
  test('Tab stays contained inside the dialog once a busy request disables every other focusable', async ({ page }) => {
    await createProject(page, `E2E Focus Trap ${Date.now()}`)

    // Hold the request open indefinitely — the test only needs "busy", never "resolved".
    await page.route('**/description:generate', () => {})

    await page.getByRole('button', { name: /^edit$/i }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    await page.getByRole('button', { name: /generate/i }).click()
    await expect(page.getByRole('textbox', { name: /project description/i })).toBeDisabled()

    // The card itself now holds focus (tabIndex={-1}) instead of falling to <body>.
    await expect(dialog).toBeFocused()

    await page.keyboard.press('Tab')
    const activeInsideDialog = await page.evaluate(() => {
      const d = document.querySelector('[role="dialog"]')
      return d != null && d.contains(document.activeElement)
    })
    expect(activeInsideDialog).toBe(true)
  })

  test('the overlay renders above the sticky navbar at desktop width — the navbar is not clickable through it', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await createProject(page, `E2E Overlay Stacking ${Date.now()}`)

    await page.getByRole('button', { name: /^edit$/i }).click()
    await expect(page.getByRole('dialog')).toBeVisible()

    const navLink = page.getByRole('navigation').getByRole('link').first()
    const box = await navLink.boundingBox()
    expect(box).toBeTruthy()
    const center = { x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 }

    // Whatever paints at the navbar link's own on-screen position must NOT be inside <nav> —
    // the modal's backdrop/dialog should be intercepting it. Before the createPortal fix,
    // the sticky rail's own stacking context kept the overlay's z-50 from ever competing with
    // the navbar's z-40 at the page root, so this would have found the nav link on top.
    const topmostIsInNav = await page.evaluate(({ x, y }) => {
      const el = document.elementFromPoint(x, y)
      return el?.closest('nav') != null
    }, center)
    expect(topmostIsInNav).toBe(false)
  })
})
