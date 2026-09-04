import { test, expect, type Page } from '@playwright/test'

/**
 * A build against a REAL Azure Container Apps sandbox — no route stubbing anywhere in this
 * file. It is the only spec in this directory that provisions a genuine container and spends real
 * model tokens rather than scripting the wire.
 *
 * OPT-IN ONLY. This needs the full real substrate wired into the backend's .env: SANDBOX__* (ACA
 * env + ACR), REDIS__* (build-session locks/heartbeat), OBJECT_STORE__* (per-app blob), and
 * FOUNDRY__* (the model that actually writes the app — without it the turn never gets past its
 * first call and nothing is ever built). None of that exists in a normal CI run, so this spec no-ops
 * unless E2E_REAL_SANDBOX=1 is set — mirroring the opt-in shape of E2E_REQUIRE_REAL_SESSION here
 * and the `integration` pytest marker on the backend side, rather than inventing a new pattern.
 *
 * A real ACA provision + `next dev` boot + first HMR-ready paint is NOT fast — a monitored run
 * against a cold environment (2026-07-29) took ~14 minutes end-to-end, not the 1-3 minutes an
 * earlier version of this comment estimated before that run happened. So this carries its own
 * long per-test timeout instead of raising the shared playwright.config.ts one — bumping that
 * global value would also make every OTHER (normally seconds-long) spec wait minutes before
 * failing on a genuine break.
 */

const REAL_SANDBOX = process.env.E2E_REAL_SANDBOX === '1'

/**
 * The address a live preview is served at. Generated apps no longer get a per-app
 * `*.azurecontainerapps.io` hostname — they share ONE hostname with the app key in the path
 * (`/a/sbx-<28 hex>`), because BIAL refused a wildcard certificate and their Container Apps
 * environment publishes no public DNS. Matched by SHAPE rather than against a literal host: the
 * hostname is deployment configuration (`APPS_BASE_URL`), so pinning one here is what made the
 * previous assertion go stale silently.
 *
 * `apps-domain.spec.ts` is the test that proves the base path actually works; this one only needs
 * to keep asserting that a real sandbox produces a real, correctly-shaped preview address.
 */
const PREVIEW_ADDRESS = /^https?:\/\/[^/]+\/a\/sbx-[0-9a-f]{28}\/?$/

async function createProject(page: Page, name: string): Promise<string> {
  await page.goto('/projects')
  await page.getByRole('button', { name: /new project/i }).first().click()
  await page.getByPlaceholder(/VIP Movement Tracker/i).fill(name)
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

    // THE RAIL'S COMPOSER, and its placeholder follows the picked kind. Build is the rail's
    // default, which is why no kind is pressed here.
    //
    // A CONCRETE prompt on purpose — a vague one is a legitimate reason for the model to ask a
    // clarifying question instead of getting on with the app, and this test is about the sandbox,
    // not about interview behaviour.
    await page.getByPlaceholder(/describe the change you need/i).fill(
      'Build a simple visitor log: a form to add a visitor name and their host, and a table listing all visitors.',
    )
    await page.getByTestId('composer-send').click()
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)

    // NOTHING TO PRESS BETWEEN THE SEND AND THE BUILD. This used to wait on a brief card and click
    // its "Build this" — a proposal a Build chat no longer makes, because the send itself pins a
    // container and the agent starts writing into it. The iframe-src wait below is now the whole
    // gate, and it is the stronger one anyway: it needs a real `preview_ready` envelope.

    // Deliberately NOT asserting on the "Building your app" status line here (it was here once,
    // dropped 2026-07-30). Its own justification claimed it proved the C7 SSE feed was live, but
    // that status line is driven by `session.status` from the POST /v1/build-sessions response
    // body itself — set client-side before any SSE frame necessarily arrives — so it never
    // actually proved that. It also flaked in real runs (a real 201 came back server-side with no
    // client-visible render within 30s). The iframe-src wait right below is strictly stronger
    // proof of the same thing: it requires a genuine `preview_ready` SSE envelope with real
    // content, not just an optimistic client render. Fewer intermediate assertions, one real one.

    // The real sandbox's genuinely cross-origin preview URL — never localhost, never the mocked
    // spec's fixed sbx-e2e.westeurope literal. 12 minutes (was 8, was 5): this is now a MEASURED
    // number, not an estimate. Pulled from Azure Log Analytics' container console logs for a real
    // run (2026-07-30): session created 08:17:56 UTC (container-create accepted) -> first preview
    // 08:27:12 UTC (the harness's first successful readiness check, which only happens once the
    // agent's whole coding turn — file writes, npm install, verification — completes) = 9m15s.
    // Both 5 and 8 minutes were guesses that happened to sit just under that real number, which is
    // exactly why both failed here. 12 gives real margin over a measured value instead of another
    // guess, while leaving room in the 20-minute total test budget for everything after it.
    //
    // The pattern asserts the SHAPE of the address rather than a literal host: this used to pin
    // `*.azurecontainerapps.io`, which stopped being true the moment previews moved onto the
    // shared apps hostname. A stale literal here fails a 20-minute run for a reason that has
    // nothing to do with the sandbox it exists to test.
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

    // agc129's #6 (P1, LivePreview.test.jsx:229): the jsdom device-toggle specs can only assert
    // that the wrapper's inline `style.width` got SET — jsdom has no layout engine, so they
    // cannot see whether the framed document's own media queries actually evaluate against that
    // width. This is the other half of that claim, proven for real: a genuinely cross-origin
    // document, in a genuinely narrower browser window, actually reflowing to the requested
    // device width — not the literal the component wrote, but what the framed app itself
    // observes. 1024x768 is the specific width the pre-fix code silently rendered 728px at
    // instead of 834px (agc129's review, LivePreview.jsx:238) — the exact case that would have
    // caught the bug before it shipped. Scoped tightly to just these two assertions and restored
    // immediately after: the stop/relaunch section below doesn't touch the device-width card (the
    // ended-state card is a fixed max-w-xs, not device-width-driven, and Stop lives in the
    // cockpit's top toolbar), but there's no real cost to keeping the blast radius narrow here
    // rather than assuming that on a run this expensive.
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

    // agc129's #6 is proven at this exact point, independent of whatever the stop/relaunch tail
    // below does. Logged explicitly so a later failure in that tail (both its timeouts are
    // unmeasured) doesn't read as "the run failed" when what actually matters already passed.
    console.log(
      '#6 VERIFIED: framed innerWidth 834 at 1024px viewport, media query true, mobile 390',
    )

    // Restore to the project's default (Desktop Chrome, playwright.config.ts) before the
    // stop/relaunch assertions below — they were written and verified against that width.
    await page.setViewportSize({ width: 1280, height: 720 })

    // The compact ended-state card (#42 F3) against the real backend: stop the real session and
    // confirm the terminal card renders small, not the old full-pane dead state.
    await page.getByRole('button', { name: /^stop$/i }).click()
    const endedCard = page.getByTestId('preview-ended-card')
    await expect(endedCard).toBeVisible({ timeout: 60_000 })

    // STILL UNCOVERED IN A BROWSER: that pressing the one start control after a stop genuinely
    // restores a live preview from the real snapshot. The card's own Relaunch button was deleted
    // with `RelaunchAffordance` (LivePreview.tsx) — R3 leaves exactly one control that starts an
    // app, `StartAppControl`, and it renders from `AppPane`, not inside this card. Repointing at
    // it blind would swap a locator that is obviously dead for one that only looks right, so the
    // claim is named here and left for a run that can actually watch the container come back.

    void projectId // kept for readability at the call site above; no further assertion needs it
  })
})
