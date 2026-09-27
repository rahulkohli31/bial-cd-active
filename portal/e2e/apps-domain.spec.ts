import { test, expect, type Page } from '@playwright/test'

/**
 * The shared apps hostname, end to end, against REAL infrastructure.
 *
 * `real-sandbox.spec.ts` proves a real ACA sandbox frames a real app; this proves what changed
 * underneath it — ONE shared hostname with the app key in the path, base path real all the way
 * down (router, container Caddy, Next's own `basePath`).
 *
 * Assertions read the FRAMED DOCUMENT, not just `iframe[src]`: the attribute is a string
 * composition, true by construction even when a wrong basePath serves an empty frame behind a
 * correct-looking URL — every assertion below reads something the CONTAINER produced.
 *
 * OPT-IN — provisions a real Container App and spends real model tokens. E2E_APPS_DOMAIN=1:
 *   E2E_BASE_URL=http://localhost E2E_APPS_HOSTNAME=http://citizenapps.localhost \
 *   E2E_STORAGE_STATE=<minted state> E2E_REQUIRE_REAL_SESSION=1 E2E_APPS_DOMAIN=1 \
 *   npx playwright test apps-domain
 */

const ENABLED = process.env.E2E_APPS_DOMAIN === '1'

// NOT defaulted to the production BIAL hostname: a default would let this suite silently assert
// against an origin the run cannot reach and report the resulting timeout as a product failure.
const APPS_ORIGIN = process.env.E2E_APPS_HOSTNAME ?? ''

const SBX_KEY = /^(?<origin>https?:\/\/[^/]+)\/a\/(?<key>sbx-[0-9a-f]{28})\/?$/
const PUB_KEY = /^(?<origin>https?:\/\/[^/]+)\/a\/(?<key>pub-[0-9a-f]{28})\/?$/


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

test.describe.serial('generated apps on the shared apps hostname (opt-in, E2E_APPS_DOMAIN=1)', () => {
  test.skip(!ENABLED, 'set E2E_APPS_DOMAIN=1 to run this against real infrastructure')

  // The apps hostname MUST be https: the portal's framing policy is `frame-src 'self'
  // https://<apps hostname>` and CSP scheme-matching is strict, so an http origin is refused
  // browser-side — an empty frame behind a correct `src`. Outside production that hostname
  // carries a self-signed cert, so trust is waived HERE (per-suite), not in playwright.config.ts,
  // where it would weaken every other spec's transport assumptions too.
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

    // A chat's kind is fixed at creation, so it's chosen HERE, through the rail's picker. Build
    // is the rail's default, but pressed explicitly rather than inherited from a default free
    // to change, since this suite is about the address a built app is served at.
    await page.getByRole('radio', { name: 'Build' }).click()

    await page.getByPlaceholder(/describe the change you need/i).fill(
      'Build a simple visitor log: a form to add a visitor name and their host, and a table listing all visitors.',
    )
    await page.getByTestId('composer-send').click()
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)
    chatUrl = page.url()
    console.log(`CHAT: ${chatUrl}`)

    // NOTHING TO PRESS BETWEEN SEND AND BUILD: a Build chat no longer proposes a brief card to
    // accept — send pins a container and the agent writes into it — so the only outcome to wait
    // for is the preview address.
    const iframe = page.locator('iframe[title="App Preview"]')

    // THE CONTRACT: the shared origin, `/a/`, and an `sbx-` key — explicitly NOT a per-app
    // `*.azurecontainerapps.io` address, the shape this change replaced. Asserting its absence
    // is what makes this a regression test rather than a smoke test.
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

    // The document's OWN idea of where it lives still carries the key — if the router stripped
    // the prefix, this is where that shows up, and every link the app generates would drop it.
    const framedHref = await frame.locator(':root').evaluate(() => window.location.href)
    console.log(`FRAMED location.href: ${framedHref}`)
    expect(framedHref).toContain(basePath)

    // Next's own client assets came back OK from UNDER the base path — the assertion that
    // distinguishes a working basePath from a correct-looking URL in front of a broken app.
    // POLLED, not read once: script chunks are still in flight seconds after the frame commits,
    // so reading the tally right after `location.href` fails the run for a race, not a defect.
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

    // A root-relative asset request would be resolved by the keyless arm from the Referer and
    // still 200 — so this asserts on the URL shape rather than on the status.
    const escaped = responses.filter(
      (r) => r.url.startsWith(`${APPS_ORIGIN}/_next/`) || r.url === `${APPS_ORIGIN}/`,
    )
    expect(escaped, `requests escaped the base path: ${JSON.stringify(escaped.slice(0, 5))}`).toEqual([])

    // Live reload: the path moved to /_next/hmr in Next 16, and a socket on the OLD path never
    // connects, silently stopping preview updates between edits. Polled for the same race reason
    // as the assets above.
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

    // The one publish button saves unsaved work before it opens the dialog, and the reviewer
    // then checks the version saved. Saving here first, retried, keeps a lock-held 409 on the
    // save from surfacing as a dialog that never opens.
    //
    // WAIT FOR THE AGENT TO PUT ITS PEN DOWN FIRST: a live preview does NOT mean the build is
    // finished, and saving while the agent keeps writing leaves the workspace dirty again
    // moments later — cycling Save -> Saving… -> Save forever. The gate note is the honest "it
    // is my turn now" signal; the composer is never `disabled`, so the note is the only thing
    // that says so.
    //
    // Below: the save click is unconditional, not `if (visible)`, and it's polled on the
    // button's OWN label (Save/Saving…/Saved).
    await expect(
      page.getByTestId('composer-gate-note'),
      'the build agent never released the conversation',
    ).toHaveCount(0, { timeout: 20 * 60_000 })

    // RETRIED: `POST …/save` can answer 409 while the build session still holds the workspace
    // lock — which outlives the composer being re-enabled — and the UI drops that on the floor,
    // returning to "Save" with no message. Measured over four consecutive runs; the same call
    // by hand a minute later returned 200.
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

    // Publishing lives behind the status chip now: open the popover, then press the one action
    // it offers (at most one; a state with nothing to do renders none).
    await page.getByTestId('publish-chip').click()
    await expect(page.getByTestId('publish-popover')).toBeVisible({ timeout: 30_000 })
    await page.getByTestId('publish-action').click()
    await expect(page.getByTestId('publish-dialog')).toBeVisible({ timeout: 30_000 })

    // The reviewer answers every class before any answer shows. A visitor log keeps names and
    // hosts, which is not PII, so both hard blocks come back as locked "Not found" results.
    for (const key of ['pii', 'financial_data']) {
      await expect(page.getByTestId(`pd-class-${key}`)).toContainText('Not found', { timeout: 5 * 60_000 })
    }

    // Every scored class to No, so the score cannot route this to an administrator and the
    // test asserts on an address rather than a queue.
    for (const key of [
      'credentials_keys', 'confidential_business_data', 'ai_usage', 'integrations', 'public_data',
    ]) {
      const no = page.getByTestId(`pd-toggle-${key}`).getByRole('radio', { name: 'No' })
      await no.click()
      await expect(no).toHaveAttribute('aria-checked', 'true')
    }
    const confirm = page.getByTestId('pd-confirm')
    await expect(confirm).toHaveText('Publish')
    await expect(confirm).toBeEnabled()
    await confirm.click()

    const publishUrl = page.getByTestId('publish-url')
    await expect(publishUrl).toBeVisible({ timeout: 15 * 60_000 })
    const href = (await publishUrl.getAttribute('href')) ?? (await publishUrl.getAttribute('title')) ?? ''
    console.log(`PUBLISHED URL: ${href}`)
    expect(href).toMatch(PUB_KEY)
    expect(href.startsWith(APPS_ORIGIN), `published url ${href} must be on ${APPS_ORIGIN}`).toBe(true)
    expect(href).not.toMatch(/azurecontainerapps\.io/)

    // Not merely well-formed — it serves the app, opened as a plain navigation.
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
