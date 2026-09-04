import { test, expect, type Page } from '@playwright/test'

/**
 * The project-first journey, against a REAL control-plane.
 *
 * These are the claims that mocked-module unit tests structurally cannot make: a delete cascade
 * counted and named against real rows, and that `/apps/{appId}` genuinely leaves the SPA rather
 * than matching a client route.
 *
 * Only `/auth/me` is mocked (no live Entra tenant in CI, KD-9); everything else is driven.
 * Run through the portal on :5173, never the backend on :8000 — the refresh cookie is
 * Path-scoped to /api/v1/auth/refresh and will not be sent otherwise.
 */

async function createProject(page: Page, name: string) {
  await page.goto('/projects')
  await page.getByRole('button', { name: /new project/i }).first().click()
  await page.getByPlaceholder(/VIP Movement Tracker/i).fill(name)
  await page.getByRole('button', { name: /create project/i }).click()
  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}/)
  const projectId = page.url().split('/projects/')[1]
  return projectId
}

test.describe('project-first journey', () => {
  // The delete test drives a REAL model turn. A turn takes 30-60s on a small app and grows with
  // the artifact, so the suite-wide 90s budget (written when /auth/me was mocked and no turn ever
  // actually ran) cannot fit it.
  test.describe.configure({ timeout: 420_000 })

  // STILL UNCOVERED IN A BROWSER: that a first build mints the project's app exactly once, and that
  // every later chat in the project builds into that same one. Restoring it is a rewrite, not a rename.

  test('deleting the project names the cascade and sends a bookmarked chat URL back to /projects', async ({ page }) => {
    const name = `E2E Delete ${Date.now()}`
    await createProject(page, name)

    // A planning chat is minted from the rail composer: pick the kind, describe it, send. The
    // kind picker is a radio group ("Plan" / "Build", named by the bootstrap catalogue), the
    // placeholder follows the picked kind, and the send control is the composer's own — there is
    // no separate "start planning" button and no per-kind composer any more.
    await page.getByRole('radio', { name: 'Plan' }).click()
    await page.getByPlaceholder(/describe what you have in mind/i).fill('What should this tool do?')
    await page.getByTestId('composer-send').click()
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
