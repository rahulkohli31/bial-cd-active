import { test, expect, type Page } from '@playwright/test'

/**
 * The project-first journey, against a REAL control-plane.
 *
 * These are the claims that mocked-module unit tests structurally cannot make: a delete cascade
 * counted and named against real rows, and that `/apps/{appId}` genuinely leaves the SPA rather
 * than matching a client route.
 *
 * Only `/auth/me` is mocked (no live Entra tenant in CI); everything else is driven.
 * Run through the portal on :5173, never the backend on :8000 — the refresh cookie is
 * Path-scoped to /api/v1/auth/refresh and will not be sent otherwise.
 */

// A description that clears the 15-120 word bound (#191) — required on every create as
// of that issue, so every caller of `createProject` below needs one regardless of what
// the test itself is about.
const VALID_DESCRIPTION =
  'Ground staff log VIP movement requests for each terminal. A duty supervisor approves ' +
  "or rejects them, and the day's approved movements appear on a shared dashboard."

async function createProject(page: Page, name: string) {
  await page.goto('/projects')
  await page.getByRole('button', { name: /new project/i }).first().click()
  await page.getByPlaceholder(/VIP Movement Tracker/i).fill(name)
  await page.getByPlaceholder(/who uses it, and what do they do with it/i).fill(VALID_DESCRIPTION)
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
    // The card is a plain container, so the delete button's accessible name is unambiguous —
    // no strict-mode double match against an outer role="button".
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
 * Two findings from a prior review of the description editor pop-up that are
 * structurally invisible to Vitest, so they need a real browser:
 *
 *  - jsdom never blurs a disabled element, so the busy-state focus collapse that broke
 *    Tab-containment and Escape cannot reproduce there.
 *  - jsdom has no layout/paint engine (and ProjectPage.test.tsx stubs Navbar to null), so
 *    the overlay-renders-beneath-the-sticky-navbar bug cannot reproduce there either.
 *
 * Neither test drives a real model turn — no build needed for either check — so they run
 * under the suite's default 90s timeout rather than the 420s one above.
 */
test.describe('description editor — keyboard focus + stacking', () => {
  test('Tab stays contained inside the dialog once a busy request disables every other focusable', async ({ page }) => {
    await createProject(page, `E2E Focus Trap ${Date.now()}`)

    // Hold the SAVE request open indefinitely — the test only needs "busy", never
    // "resolved". Generate is gone (#191); Save is now the only request that can put
    // the surface in that state, so route on the PATCH specifically — the project's
    // own creation and any other GET on this path must still pass through untouched.
    await page.route('**/api/projects/*', (route) => {
      if (route.request().method() === 'PATCH') return
      void route.continue()
    })

    await page.getByRole('button', { name: /^edit$/i }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    // 15+ words — #191 requires the description to clear the word bound before Save
    // enables at all, so a short string here would never even reach the routed PATCH.
    await page.getByRole('textbox', { name: /project description/i }).fill(VALID_DESCRIPTION)
    await page.getByRole('button', { name: 'Save' }).click()
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

/**
 * The duplicate check at create (#191 slice 4, R31-R39).
 *
 * `check-duplicates` and `duplicate-check-resolved` are mocked, deliberately, and are the
 * only two routes in this file that are. Whether the screen below appears at all depends on
 * whether Foundry embeddings are configured AND the marketplace catalog happens to already
 * contain a confident match — neither of which this suite controls or should have to stand
 * up just to prove the UI itself is wired correctly. Mocking those two responses makes the
 * screen swap deterministic; the actual project creation underneath (`POST /api/projects`)
 * is real, against the real backend and database, same as every other test in this file.
 */
test.describe('duplicate check before create (#191 slice 4)', () => {
  const oneMatch = {
    name: 'Existing VIP Tracker',
    description: 'Already logs VIP movements for the terminal team.',
    builderDisplayName: 'Another Builder',
    url: 'https://example-published-app.azurecontainerapps.io/',
  }

  async function openCreateModalAndFill(page: Page, name: string) {
    await page.goto('/projects')
    await page.getByRole('button', { name: /new project/i }).first().click()
    await page.getByPlaceholder(/VIP Movement Tracker/i).fill(name)
    await page.getByPlaceholder(/who uses it, and what do they do with it/i).fill(VALID_DESCRIPTION)
  }

  test('a confident match shows the possible-duplicate screen, and "Create project anyway" proceeds', async ({
    page,
  }) => {
    await page.route('**/api/projects:check-duplicates', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ matches: [oneMatch] }),
      })
    })
    await page.route('**/api/projects:duplicate-check-resolved', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) })
    })

    await openCreateModalAndFill(page, `E2E Duplicate Check ${Date.now()}`)
    await page.getByRole('button', { name: /^create project$/i }).click()

    await expect(page.getByText('This might already exist')).toBeVisible()
    await expect(page.getByText(oneMatch.name)).toBeVisible()
    await expect(page.getByText(`Built by ${oneMatch.builderDisplayName}`)).toBeVisible()
    // The link is real (not a placeholder) — it opens the actual match's URL, not the new
    // project the citizen has not created yet.
    await expect(page.getByRole('link', { name: /open app/i })).toHaveAttribute('href', oneMatch.url)

    await page.getByRole('button', { name: /create project anyway/i }).click()
    await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}/)
  })

  test('"Go back" returns to the form with the typed name and description intact', async ({ page }) => {
    await page.route('**/api/projects:check-duplicates', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ matches: [oneMatch] }),
      })
    })

    const name = `E2E Duplicate Go Back ${Date.now()}`
    await openCreateModalAndFill(page, name)
    await page.getByRole('button', { name: /^create project$/i }).click()

    await expect(page.getByText('This might already exist')).toBeVisible()
    await page.getByRole('button', { name: /go back/i }).click()

    // The SAME form instance, not a fresh one — the typed values survived the round trip
    // through the duplicate screen rather than being lost when it unmounted.
    await expect(page.getByPlaceholder(/VIP Movement Tracker/i)).toHaveValue(name)
    await expect(page.getByPlaceholder(/who uses it, and what do they do with it/i)).toHaveValue(
      VALID_DESCRIPTION,
    )
  })

  test('no matches creates immediately, with no extra screen (the default, day-one case)', async ({ page }) => {
    await page.route('**/api/projects:check-duplicates', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ matches: [] }),
      })
    })

    await openCreateModalAndFill(page, `E2E No Duplicate ${Date.now()}`)
    await page.getByRole('button', { name: /^create project$/i }).click()

    await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}/)
    await expect(page.getByText('This might already exist')).toHaveCount(0)
  })

  test('a broken duplicate check still creates the project (R37 — a courtesy, never a gate)', async ({ page }) => {
    await page.route('**/api/projects:check-duplicates', async (route) => {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ error: { message: 'simulated failure' } }),
      })
    })

    await openCreateModalAndFill(page, `E2E Duplicate Check Failure ${Date.now()}`)
    await page.getByRole('button', { name: /^create project$/i }).click()

    await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}/)
  })
})

// The search box's value is read back from the URL. jsdom cannot show this: act() flushes every
// update before an assertion, so only a real browser sees a deferred URL update revert the input.
test('the search box keeps every key and the caret on a mid-string edit', async ({ page }) => {
  await page.goto('/projects')
  const search = page.getByLabel('Search applications')
  await search.click()
  await page.keyboard.type('abcdefgh')
  await expect(search).toHaveValue('abcdefgh')

  await search.evaluate((el: HTMLInputElement) => el.setSelectionRange(3, 3))
  await page.keyboard.type('XY')

  await expect(search).toHaveValue('abcXYdefgh')
  await expect(page).toHaveURL(/[?&]q=abcXYdefgh/)
})
