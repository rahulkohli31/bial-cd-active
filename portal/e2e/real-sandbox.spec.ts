import { test, expect, type Page } from '@playwright/test'

/**
 * A build against a REAL Azure Container Apps sandbox — no route stubbing anywhere in this file,
 * the only spec here that provisions a genuine container and spends real model tokens.
 *
 * OPT-IN ONLY, behind E2E_REAL_SANDBOX=1: needs the full real substrate wired into the backend's
 * .env (SANDBOX__*, REDIS__*, OBJECT_STORE__*, FOUNDRY__* — without the model, the turn never
 * gets past its first call), none of which exists in a normal CI run.
 *
 * A real ACA provision + `next dev` boot + first HMR-ready paint is NOT fast — a monitored run
 * against a cold environment (2026-07-29) took ~14 minutes end-to-end, not the 1-3 minutes an
 * earlier version of this comment estimated. So this carries its own long per-test timeout
 * rather than raising the shared playwright.config.ts one, which would make every other
 * (seconds-long) spec wait minutes before failing on a genuine break.
 */

const REAL_SANDBOX = process.env.E2E_REAL_SANDBOX === '1'

/**
 * The address a live preview is served at. Generated apps share ONE hostname with the app key in
 * the path (`/a/<32 hex alias>`) rather than a per-app `*.azurecontainerapps.io` one — BIAL refused
 * a wildcard certificate and its Container Apps environment publishes no public DNS. Matched by
 * SHAPE, not a literal host: the hostname is deployment config (`APPS_BASE_URL`), and pinning one
 * here is what made the previous assertion go stale silently. `apps-domain.spec.ts` proves the
 * base path itself works; this only needs the shape to stay right.
 */
const PREVIEW_ADDRESS = /^https?:\/\/[^/]+\/a\/[0-9a-f]{32}\/?$/

// A description that clears the 15-120 word bound (#191) — required on every create.
const VALID_DESCRIPTION =
  'Ground staff log VIP movement requests for each terminal. A duty supervisor approves ' +
  "or rejects them, and the day's approved movements appear on a shared dashboard."

async function createProject(page: Page, name: string): Promise<string> {
  await page.goto('/projects')
  await page.getByRole('button', { name: /new project/i }).first().click()
  await page.getByPlaceholder(/VIP Movement Tracker/i).fill(name)
  await page.getByPlaceholder(/who uses it, and what do they do with it/i).fill(VALID_DESCRIPTION)
  await page.getByRole('button', { name: /create project/i }).click()
  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}/)
  return page.url().split('/projects/')[1]
}

test.describe('real Azure sandbox (opt-in, E2E_REAL_SANDBOX=1)', () => {
  test.skip(!REAL_SANDBOX, 'set E2E_REAL_SANDBOX=1 to run this against a real ACA sandbox')

  test('a real build provisions a real sandbox and frames a genuinely interactive app', async ({ page }) => {
    // Provisioning + npm install + next dev boot, unscripted. A real monitored run (2026-07-29)
    // took ~14 minutes end-to-end against a cold ACA environment before hitting its terminal —
    // 20 minutes gives real margin over that observation while staying under the harness's own
    // 30-minute hard ceiling (RUN_WALL_CLOCK_DEADLINE_S), so a genuine hang still fails in
    // bounded time rather than riding the full 30.
    test.setTimeout(20 * 60_000)

    const projectId = await createProject(page, `E2E Real Sandbox ${Date.now()}`)

    // THE RAIL'S COMPOSER, defaulting to Build (why no kind is pressed here), with its
    // placeholder following the picked kind. A CONCRETE prompt on purpose: a vague one is a
    // legitimate reason for the model to ask a clarifying question, and this test is about the
    // sandbox, not interview behaviour.
    await page.getByPlaceholder(/describe the change you need/i).fill(
      'Build a simple visitor log: a form to add a visitor name and their host, and a table listing all visitors.',
    )
    await page.getByTestId('composer-send').click()
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)

    // NOTHING TO PRESS BETWEEN THE SEND AND THE BUILD. This used to wait on a brief card and click
    // its "Build this" — a proposal a Build chat no longer makes, because the send itself pins a
    // container and the agent starts writing into it. The iframe-src wait below is now the whole
    // gate, and it is the stronger one anyway: it needs a real `preview_ready` envelope.

    // Deliberately NOT asserting on the "Building your app" status line: it is driven by
    // `session.status` from the POST response body itself, set client-side before any SSE frame
    // necessarily arrives, so it never actually proved the feed was live — and it flaked in real
    // runs. The iframe-src wait below is strictly stronger proof of the same thing: it requires a
    // genuine `preview_ready` SSE envelope with real content, not an optimistic client render.

    // The real sandbox's genuinely cross-origin preview URL — never localhost, never a mocked
    // fixture. 12 minutes is a MEASURED value (a real cold run took 9m15s end-to-end), not a
    // guess — earlier guesses (5, then 8 minutes) sat just under the real number and both failed.
    // Asserts the SHAPE of the address, not a literal host — see PREVIEW_ADDRESS above.
    const iframe = page.locator('iframe[title="App Preview"]')
    await expect(iframe).toHaveAttribute('src', PREVIEW_ADDRESS, {
      timeout: 12 * 60_000,
    })

    // Drive INTO the framed app — the assertion no spec with a scripted iframe src can make, since
    // there is no real document behind one. A real page title proves the cross-origin frame
    // actually hydrated a real Next.js app, not just that the iframe's src attribute got set.
    const frame = page.frameLocator('iframe[title="App Preview"]')
    await expect(frame.locator('body')).toBeVisible({ timeout: 60_000 })
    await expect(frame.locator('html')).not.toBeEmpty()

    // jsdom specs can only assert that the wrapper's inline `style.width` got SET — they cannot
    // see whether the framed document's own media queries actually evaluate against that width.
    // This is that claim, proven for real, against a genuinely cross-origin document. 1024x768 is
    // the specific viewport the pre-fix code silently rendered 728px at instead of 834px — the
    // exact case that would have caught the bug before it shipped.
    await page.setViewportSize({ width: 1024, height: 768 })
    const root = frame.locator(':root')

    const tabletButton = page.getByRole('button', { name: /tablet/i })
    await tabletButton.click()
    // A missed/late click should fail here, on the cause, rather than surface later as a
    // confusing width mismatch that looks like a reflow bug but is actually a bad click.
    await expect(tabletButton).toHaveAttribute('aria-pressed', 'true')
    // The click resizes the iframe element itself; window.innerWidth inside the FRAMED document
    // only updates on the browser's next layout pass, not synchronously with the click. A bare
    // evaluate() right after the click risks reading the pre-resize value and failing spuriously
    // on a real reflow that just hadn't landed yet — expect.poll re-reads until it settles (or
    // genuinely times out, which is the real failure this test exists to catch).
    await expect.poll(() => root.evaluate(() => window.innerWidth), { timeout: 5_000 }).toBe(834)
    await expect
      .poll(() => root.evaluate(() => matchMedia('(min-width: 768px)').matches), { timeout: 5_000 })
      .toBe(true)

    const mobileButton = page.getByRole('button', { name: /mobile/i })
    await mobileButton.click()
    await expect(mobileButton).toHaveAttribute('aria-pressed', 'true')
    await expect.poll(() => root.evaluate(() => window.innerWidth), { timeout: 5_000 }).toBe(390)

    // Proven at this exact point, independent of the stop/relaunch tail below — logged so a
    // later failure there (its timeouts are unmeasured) doesn't read as "the run failed" when
    // what actually matters already passed.
    console.log(
      '#6 VERIFIED: framed innerWidth 834 at 1024px viewport, media query true, mobile 390',
    )

    // Restore to the project's default (Desktop Chrome, playwright.config.ts) before the
    // stop/relaunch assertions below — they were written and verified against that width.
    await page.setViewportSize({ width: 1280, height: 720 })

    // The compact ended-state card against the real backend: stop the real session and
    // confirm the terminal card renders small, not the old full-pane dead state.
    await page.getByRole('button', { name: /^stop$/i }).click()
    const endedCard = page.getByTestId('preview-ended-card')
    await expect(endedCard).toBeVisible({ timeout: 60_000 })

    // STILL UNCOVERED IN A BROWSER: that pressing the one start control after a stop genuinely
    // restores a live preview from the real snapshot. The card's own Relaunch button was deleted
    // with `RelaunchAffordance` (LivePreview.tsx), leaving exactly one control that starts an
    // app, `StartAppControl`, and it renders from `AppPane`, not inside this card. Repointing at
    // it blind would swap a locator that is obviously dead for one that only looks right, so the
    // claim is named here and left for a run that can actually watch the container come back.

    void projectId // kept for readability at the call site above; no further assertion needs it
  })
})
