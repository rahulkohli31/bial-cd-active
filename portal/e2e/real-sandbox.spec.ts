import { test, expect, type Page } from '@playwright/test'

/**
 * A build against a REAL Azure Container Apps sandbox — no route stubbing anywhere in this
 * file. This is the "manual E2E round" `thread-lifecycle.spec.ts` refers to (its build session
 * and relay are both scripted; this one is deliberately not).
 *
 * OPT-IN ONLY. This needs the full real substrate wired into the backend's .env: SANDBOX__* (ACA
 * env + ACR), REDIS__* (build-session locks/heartbeat), OBJECT_STORE__* (per-app blob), and
 * FOUNDRY__* (the model that actually writes the app — without it the relay never returns a
 * brief and no build ever starts). None of that exists in a normal CI run, so this spec no-ops
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

const composer = (page: Page) => page.getByPlaceholder(/describe what you need/i)

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

    // A CONCRETE prompt on purpose — the interview protocol is a real, stochastic model call
    // here (unlike thread-lifecycle.spec.ts's scripted two-turn relay), and a vague prompt could
    // legitimately ask a clarifying question instead of proposing a brief on the first turn. A
    // fully-specified request keeps this test about the sandbox, not about interview behaviour.
    await page.getByPlaceholder(/Describe the app you want to build/i).fill(
      'Build a simple visitor log: a form to add a visitor name and their host, and a table listing all visitors.',
    )
    await page.getByRole('button', { name: /generate app/i }).click()
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)

    // The model may ask a clarifying question before proposing a brief on the FIRST turn — but we
    // cannot know which until that first reply actually lands, and the handed-off prompt's own
    // send is still in flight right after navigation. Checking isVisible() immediately here (no
    // wait) raced the handoff's own fireRelayTurn and fired a second, concurrent turn into the
    // same thread — sending "no further requirements" before the handoff prompt had even been
    // answered. Wait properly for the FIRST outcome instead; only send the clarifying follow-up
    // if that wait genuinely times out (i.e., a question, not a brief, came back).
    const card = page.getByTestId('build-brief-card')
    try {
      await expect(card).toBeVisible({ timeout: 45_000 })
    } catch {
      await composer(page).fill('No further requirements — build it as described.')
      await composer(page).press('Enter')
      await expect(card).toBeVisible({ timeout: 45_000 })
    }
    await card.getByRole('button', { name: /build this/i }).click()

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
    const iframe = page.locator('iframe[title="App Preview"]')
    await expect(iframe).toHaveAttribute('src', /^https:\/\/.*\.azurecontainerapps\.io\//, {
      timeout: 12 * 60_000,
    })

    // Drive INTO the framed app — this is the assertion `projects.spec.ts`'s PORTAL-PREVIEW TODO
    // defers and thread-lifecycle.spec.ts cannot make at all (its iframe src is a scripted
    // string, not a real document). A real page title proves the cross-origin frame actually
    // hydrated a real Next.js app, not just that the iframe's src attribute got set.
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

    // The compact ended-state card (#42 F3) + a working relaunch, against the real backend: stop
    // the real session, confirm the terminal card renders small (not the old full-pane dead
    // state), and that relaunch genuinely restores a fresh live preview from the real snapshot.
    await page.getByRole('button', { name: /^stop$/i }).click()
    const endedCard = page.getByTestId('preview-ended-card')
    await expect(endedCard).toBeVisible({ timeout: 60_000 })
    await endedCard.getByRole('button', { name: /relaunch/i }).click()
    await expect(iframe).toHaveAttribute('src', /^https:\/\/.*\.azurecontainerapps\.io\//, {
      timeout: 3 * 60_000,
    })

    void projectId // kept for readability at the call site above; no further assertion needs it
  })
})
