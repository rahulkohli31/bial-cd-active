import { test, expect, type Page } from '@playwright/test'

/**
 * The shared apps hostname, end to end, against REAL infrastructure.
 *
 * `real-sandbox.spec.ts` proves a real ACA sandbox frames a real app. This proves the thing that
 * changed underneath it: the app is no longer addressed at its own per-app Azure hostname, it is
 * addressed at ONE shared hostname with the app key in the path, and the base path is real all
 * the way down — router, container Caddy, and Next's own `basePath`.
 *
 * WHY THE ASSERTIONS ARE ABOUT THE FRAMED DOCUMENT AND NOT JUST THE `src` ATTRIBUTE. A test that
 * only reads `iframe[src]` passes even when every asset behind that address 404s — the attribute
 * is set by the control plane from a string composition and is true by construction. The defect
 * this suite exists to catch lives one layer down: under `basePath` Next gates every route,
 * asset and route handler behind the prefix, so an address that is right and a container that is
 * wrong render an empty frame with a correct-looking URL. Every assertion below therefore reads
 * something the CONTAINER produced.
 *
 * OPT-IN, for the same reason `real-sandbox.spec.ts` is: it provisions a real Container App and
 * spends real model tokens. E2E_APPS_DOMAIN=1 to run.
 *
 *   E2E_BASE_URL=http://localhost \
 *   E2E_APPS_HOSTNAME=http://citizenapps.localhost \
 *   E2E_STORAGE_STATE=<minted state> E2E_REQUIRE_REAL_SESSION=1 \
 *   E2E_APPS_DOMAIN=1 npx playwright test apps-domain
 */

const ENABLED = process.env.E2E_APPS_DOMAIN === '1'

// The browser-facing apps origin. NOT defaulted to the production BIAL hostname: a default would
// let this suite silently assert against an origin the run cannot reach and report the resulting
// timeout as a product failure.
const APPS_ORIGIN = process.env.E2E_APPS_HOSTNAME ?? ''

const SBX_KEY = /^(?<origin>https?:\/\/[^/]+)\/a\/(?<key>sbx-[0-9a-f]{28})\/?$/
const PUB_KEY = /^(?<origin>https?:\/\/[^/]+)\/a\/(?<key>pub-[0-9a-f]{28})\/?$/


async function createProject(page: Page, name: string): Promise<string> {
  await page.goto('/projects')
  await page.getByRole('button', { name: /new project/i }).first().click()
  await page.getByPlaceholder(/VIP Movement Tracker/i).fill(name)
  await page.getByRole('button', { name: /create project/i }).click()
  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}/)
  return page.url().split('/projects/')[1]
}

test.describe.serial('generated apps on the shared apps hostname (opt-in, E2E_APPS_DOMAIN=1)', () => {
  test.skip(!ENABLED, 'set E2E_APPS_DOMAIN=1 to run this against real infrastructure')

  // The apps hostname MUST be https for this suite to mean anything: the portal's framing policy
  // is `frame-src 'self' https://<apps hostname>`, and CSP scheme-matching is strict, so an http
  // apps origin is refused by the browser before a single request leaves it — an empty frame
  // behind a perfectly correct `src`, which reads exactly like a broken base path. Outside
  // production that hostname is served with a self-signed certificate, so trust is waived HERE
  // (per-suite) rather than in playwright.config.ts, where it would silently weaken every
  // other spec's transport assumptions too.
  test.use({ ignoreHTTPSErrors: true })

  let chatUrl = process.env.E2E_CHAT_URL ?? ''
  let previewSrc = ''

  test('a preview is served from the shared hostname, and the base path is real end to end', async ({ page }) => {
    test.setTimeout(25 * 60_000)
    expect(APPS_ORIGIN, 'E2E_APPS_HOSTNAME must name the browser-facing apps origin').not.toBe('')

    // Recorded from the BROWSER's point of view, so what is asserted is what the container
    // actually answered rather than what the control plane intended.
    const responses: { url: string; status: number }[] = []
    const sockets: string[] = []
    page.on('response', (r) => responses.push({ url: r.url(), status: r.status() }))
    page.on('websocket', (ws) => sockets.push(ws.url()))

    await createProject(page, `Apps Domain E2E ${Date.now()}`)

    // A chat's kind is fixed when it is created, so it is chosen HERE, before the first message,
    // through the rail's picker — the way a citizen does. Build is the rail's default, but this
    // suite is about the address a built app is served at, so the kind is pressed explicitly
    // rather than inherited from a default that is free to change.
    await page.getByRole('radio', { name: 'Build' }).click()

    await page.getByPlaceholder(/describe the change you need/i).fill(
      'Build a simple visitor log: a form to add a visitor name and their host, and a table listing all visitors.',
    )
    await page.getByTestId('composer-send').click()
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)
    chatUrl = page.url()
    console.log(`CHAT: ${chatUrl}`)

    // NOTHING TO PRESS BETWEEN THE SEND AND THE BUILD. This used to accept either a proposed brief
    // card or a live preview, because which one arrived was a MODEL decision — a Build chat makes
    // no such proposal any more: the send pins a container and the agent writes into it. So the
    // only outcome to wait for is the preview address, which the next assertion already waits for.
    const iframe = page.locator('iframe[title="App Preview"]')

    // THE CONTRACT: the shared origin, `/a/`, and an `sbx-` key. Explicitly NOT a per-app
    // `*.azurecontainerapps.io` address — that is the shape this change replaced, and asserting
    // its absence is what makes this a regression test rather than a smoke test.
    await expect(iframe).toHaveAttribute('src', SBX_KEY, { timeout: 15 * 60_000 })
    previewSrc = (await iframe.getAttribute('src')) ?? ''
    const key = SBX_KEY.exec(previewSrc)?.groups?.key ?? ''
    expect(key, 'preview src must carry an sbx- key').not.toBe('')
    expect(previewSrc.startsWith(APPS_ORIGIN), `preview src ${previewSrc} must be on ${APPS_ORIGIN}`).toBe(true)
    expect(previewSrc).not.toMatch(/azurecontainerapps\.io/)
    console.log(`PREVIEW SRC: ${previewSrc}`)

    const basePath = `/a/${key}`
    const frame = page.frameLocator('iframe[title="App Preview"]')

    // The frame holds a real, hydrated document — not a blank frame behind a correct address.
    await expect(frame.locator('body')).toBeVisible({ timeout: 3 * 60_000 })
    await expect(frame.locator('html')).not.toBeEmpty()

    // The document's OWN idea of where it lives still carries the key. If the router stripped the
    // prefix and let the app believe it was at the root, this is where that shows up — and every
    // link the app generates from here would drop the key.
    const framedHref = await frame.locator(':root').evaluate(() => window.location.href)
    console.log(`FRAMED location.href: ${framedHref}`)
    expect(framedHref).toContain(basePath)

    // Next's own client assets came back OK from UNDER the base path. This is the assertion that
    // distinguishes a working basePath from a correct-looking URL in front of a broken app.
    //
    // POLLED, not read once. The document's own URL is known the instant the frame commits, but
    // its script chunks are still in flight for seconds afterwards — reading the tally right after
    // `location.href` reports zero and fails the run for a race rather than for a defect.
    await expect
      .poll(() => responses.filter((r) => r.url.includes(`${basePath}/_next/`)).length, {
        timeout: 90_000,
        message: 'the app must request its assets under the base path',
      })
      .toBeGreaterThan(0)
    const nextAssets = responses.filter((r) => r.url.includes(`${basePath}/_next/`))
    console.log(`_next assets under ${basePath}: ${nextAssets.length}, ` +
      `non-2xx: ${nextAssets.filter((r) => r.status >= 300).length}`)
    expect(nextAssets.filter((r) => r.status >= 400)).toEqual([])

    // Nothing escaped to the ROOT of the apps origin. A root-relative asset request would be
    // resolved by the keyless arm from the Referer, so it would still 200 — which is exactly why
    // this asserts on the URL shape rather than on the status.
    const escaped = responses.filter(
      (r) => r.url.startsWith(`${APPS_ORIGIN}/_next/`) || r.url === `${APPS_ORIGIN}/`,
    )
    expect(escaped, `requests escaped the base path: ${JSON.stringify(escaped.slice(0, 5))}`).toEqual([])

    // Live reload. The path moved to /_next/hmr in Next 16; a socket on the OLD path never
    // connects, and the preview then silently stops updating between edits. Polled for the same
    // reason as the assets above — the socket is opened by a chunk that has to arrive first.
    await expect
      .poll(() => sockets.filter((u) => u.includes('/_next/hmr')).length, {
        timeout: 90_000,
        message: 'the live-reload socket must be opened',
      })
      .toBeGreaterThan(0)
    const hmr = sockets.filter((u) => u.includes('/_next/hmr'))
    console.log(`websockets: ${JSON.stringify(sockets)}`)
    expect(hmr.every((u) => u.includes(basePath)), 'the HMR socket must be opened under the base path').toBe(true)

    // Root-relative links inside the app carry the prefix, so an in-app click cannot walk out of
    // the app and land on the apps origin's own 404.
    const badLinks = await frame.locator(':root').evaluate((_el, bp) => {
      return Array.from(document.querySelectorAll('a[href^="/"]'))
        .map((a) => a.getAttribute('href') ?? '')
        .filter((h) => !h.startsWith(bp))
    }, basePath)
    expect(badLinks, `links escape the base path: ${JSON.stringify(badLinks)}`).toEqual([])
  })

  test('publishing puts the app on the same hostname under a pub- key, and it loads', async ({ page }) => {
    test.setTimeout(20 * 60_000)
    test.skip(chatUrl === '', 'the build test did not complete, so there is nothing to publish')

    await page.goto(chatUrl)

    // Publish refuses on unsaved work, so save first — the same order a user is forced into.
    //
    // MANDATORY, not `if (visible)`. A conditional save silently does nothing when the control is
    // slow to render, and the run then walks into the classification modal's dead end — "There's
    // nothing saved to check yet — press Save first", whose only control is Cancel — and fails 45
    // seconds later on a missing confirm button, which reads like a broken publish flow rather
    // than a skipped click. A step this load-bearing has to fail AT the step.
    //
    // WAITED ON A POSITIVE SIGNAL. The first version of this waited for `publish-unsaved` to reach
    // count 0, which is vacuously true before any save has happened — the control simply is not in
    // the DOM yet — so the run sailed past an unfinished save and met the classification modal's
    // dead end ("There's nothing saved to check yet"). The button's own label is the real state
    // machine here: `Save` when dirty, `Saving…` in flight, `Saved` when the snapshot exists.
    // WAIT FOR THE AGENT TO PUT ITS PEN DOWN FIRST. A live preview does NOT mean the build is
    // finished — the app is served as soon as it boots, and the agent keeps writing files behind
    // it. Saving during that window genuinely works (the POST lands) but the workspace is dirty
    // again moments later, so the button cycles Save -> Saving… -> Save forever and the run reads
    // like a broken save.
    //
    // THE GATE NOTE IS THE HONEST "it is my turn now" SIGNAL, now that the mode pill this used to
    // watch is gone. The composer is never `disabled` in any state, so there is no control whose
    // enabled-ness says this; what the composer does instead is state the one reason Send is
    // waiting, in that note, for exactly as long as something holds the thread. No note, no hold.
    await expect(
      page.getByTestId('composer-gate-note'),
      'the build agent never released the conversation',
    ).toHaveCount(0, { timeout: 20 * 60_000 })

    // RETRIED, because a Save can be REFUSED and say nothing. `POST …/save` answers 409 while the
    // build session still holds the workspace lock — which outlives the composer being re-enabled
    // — and the UI drops that on the floor: the button returns to "Save" with no message, no
    // alert, nothing. A single click therefore looks exactly like a save that silently did
    // nothing. Measured, not inferred: four consecutive runs logged `POST …/save → 409`, and the
    // same call by hand a minute later returned 200 with `dirty: false`.
    const save = page.getByTestId('save-project')
    await expect(save).toBeVisible({ timeout: 120_000 })
    let saved = false
    for (let attempt = 0; attempt < 20 && !saved; attempt++) {
      const label = (await save.textContent())?.trim()
      if (label === 'Saved') { saved = true; break }
      if (label === 'Save') await save.click().catch(() => {})
      await page.waitForTimeout(5_000)
      saved = (await save.textContent())?.trim() === 'Saved'
    }
    expect(saved, 'the workspace never reached a saved state (POST /save keeps answering 409)').toBe(true)

    // The single publish button is gone: publishing now lives behind the status chip, so the
    // action is two presses — open the popover, then press the one action it offers (there is
    // AT MOST one, and a state with nothing to do renders none). `publish-url` below is inside
    // that same popover, so this spec needed the chip open regardless.
    await page.getByTestId('publish-chip').click()
    await expect(page.getByTestId('publish-popover')).toBeVisible({ timeout: 30_000 })
    await page.getByTestId('publish-action').click()
    await expect(page.getByTestId('data-classification-modal')).toBeVisible({ timeout: 30_000 })

    // Declare nothing sensitive, so the server auto-publishes instead of routing to a human
    // (AUTO_DEPLOY_MAX_SCORE is 0 — any weighted category needs review and this test would then
    // be asserting on a queue rather than on an address).
    for (const key of [
      'credentialsSecrets', 'healthData', 'personalInformation',
      'financialData', 'confidentialBusinessData', 'publicData',
    ]) {
      // RETRIED, and asserted on `aria-checked` rather than on the click returning. The modal
      // re-renders while its own background gate check runs, so a single click loses the race
      // ("element is not stable" / "element was detached from the DOM") on whichever question
      // happens to be under the cursor when the re-render lands. Clicking until the control
      // reports itself checked is the only form of this that is not timing-dependent.
      let checked = false
      for (let attempt = 0; attempt < 8 && !checked; attempt++) {
        const no = page.getByTestId(`dc-question-${key}-no`)
        if (!(await no.isVisible().catch(() => false))) break
        if ((await no.getAttribute('aria-checked').catch(() => null)) === 'true') { checked = true; break }
        await no.click({ timeout: 5_000 }).catch(() => {})
        checked = (await no.getAttribute('aria-checked').catch(() => null)) === 'true'
        if (!checked) await page.waitForTimeout(750)
      }
      expect(checked, `could not answer "${key}"`).toBe(true)
    }
    const confirm = page.getByTestId('dc-confirm')
    await expect(confirm).toBeEnabled({ timeout: 30_000 })
    await confirm.click()

    const publishUrl = page.getByTestId('publish-url')
    await expect(publishUrl).toBeVisible({ timeout: 15 * 60_000 })
    const href = (await publishUrl.getAttribute('href')) ?? (await publishUrl.getAttribute('title')) ?? ''
    console.log(`PUBLISHED URL: ${href}`)
    expect(href).toMatch(PUB_KEY)
    expect(href.startsWith(APPS_ORIGIN), `published url ${href} must be on ${APPS_ORIGIN}`).toBe(true)
    expect(href).not.toMatch(/azurecontainerapps\.io/)

    // The published address is not merely well-formed — it serves the app. Opened as a plain
    // navigation, the way somebody who was sent the link would open it.
    const published = await page.context().newPage()
    const res = await published.goto(href, { waitUntil: 'domcontentloaded', timeout: 120_000 })
    expect(res?.status(), `GET ${href}`).toBeLessThan(400)
    await expect(published.locator('body')).toBeVisible({ timeout: 60_000 })
    await expect(published.locator('html')).not.toBeEmpty()
    // Never the router's own not-available page behind a 200.
    await expect(published.locator('body')).not.toContainText('This app is not available at this address')
    await published.close()
  })
})
