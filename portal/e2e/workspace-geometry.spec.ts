import { test, expect, type Page, type Locator } from '@playwright/test'

/**
 * Geometry the unit suite structurally cannot assert.
 *
 * jsdom has no layout engine: `getBoundingClientRect()` returns zeroes there, so every claim in
 * this file is unreachable from Vitest. The units that produced these behaviours pinned their
 * MECHANISM in Vitest — which class is on which control, which element precedes which in source
 * order — and said so in the test names. What is left is the part that needs pixels, and writing
 * it as a presence check instead is the exact false pass this campaign already produced once:
 * a framing scenario that ran green for weeks while the framed app returned a 500, because it
 * only ever read an attribute.
 *
 * ── WHERE THIS RUNS ────────────────────────────────────────────────────────────────────────
 * Against the portal CONTAINER, never `vite dev`. The dev server's config hardcodes a CSP that
 * forbids framing outright, so anything asserting on the app frame passes vacuously there —
 * it measures the CSP, not the product. Set E2E_BASE_URL at the container.
 *
 *   E2E_BASE_URL=https://localhost npx playwright test e2e/workspace-geometry.spec.ts
 *
 * ── THE RULE EVERY TEST HERE FOLLOWS ───────────────────────────────────────────────────────
 * Every absence assertion is paired with a liveness assertion, and every geometric assertion is
 * preceded by a negative control proving the thing being measured actually rendered. A page that
 * crashed inside an error boundary has no overlapping rectangles either.
 */

const NARROW = { width: 360, height: 800 }
const WIDE = { width: 1200, height: 800 }

/** A rectangle, or a failure that names what was missing rather than throwing on null. */
async function rect(locator: Locator, what: string) {
  const box = await locator.boundingBox()
  expect(box, `${what} has no bounding box — it did not render, so nothing below it is measured`).not.toBeNull()
  return box!
}

/**
 * Two rectangles are disjoint when neither overlaps the other on BOTH axes. Written out rather
 * than imported so the failure message can name the pair.
 */
function overlaps(a: { x: number; y: number; width: number; height: number }, b: typeof a) {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y
}

async function openFirstProject(page: Page) {
  await page.goto('/projects')
  // BOTH VIEWS, because the landing screen remembers which one you last chose (`projectsListMemory`)
  // and a run inherits whatever that was. `project-card` and `project-row` are the two roots, and
  // each is clickable at its centre — the name button's stretched `::after` covers the whole tile.
  // These testids exist BECAUSE of this file: when it first landed it named two testids that were
  // in neither component, so `waitFor` timed out at 30s before a single assertion ran and every
  // geometric check in here was inert while the file looked green in a suite nothing runs.
  const firstCard = page.getByTestId('project-card').first().or(page.getByTestId('project-row').first())
  await firstCard.waitFor({ state: 'visible', timeout: 30_000 })
  await firstCard.click()
  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}/)
  return page.url().split('/projects/')[1]
}

test.describe('the workspace toolbar at the declared 360px minimum', () => {
  test.describe.configure({ timeout: 180_000 })

  test.use({ viewport: NARROW })

  test('every pressable control presents at least 44x44, and the glyphs did not grow to do it', async ({ page }) => {
    await openFirstProject(page)

    const toolbar = page.getByTestId('workspace-toolbar')
    await toolbar.waitFor({ state: 'visible', timeout: 30_000 })

    // NEGATIVE CONTROL. Without this, a workspace that rendered an empty toolbar passes every
    // measurement below by having nothing to measure.
    const occupants = toolbar.locator('button, a, [role="button"]')
    // The application's own controls arrive after the project loads, so wait for them rather than count the first frame.
    await expect
      .poll(() => occupants.count(), { message: 'the toolbar rendered no pressable controls at all — the measurements below would be vacuous', timeout: 20_000 })
      .toBeGreaterThanOrEqual(5)

    // 44x44 is the floor, not the target: the controls are padded up to it, so `>=` is the
    // honest comparison. A control absent at this width is skipped rather than failed — the rail
    // toggle only exists when the rail is present — but the count control above stops that
    // becoming a way for everything to be skipped.
    const targets: Array<[string, Locator]> = [
      ['back', page.getByLabel(/^Back to project/)],
      ['rename pencil', page.getByLabel('Rename project')],
      ['reload', page.getByLabel('Reload your app')],
      ['open in new tab', page.getByLabel('Open your app in a new tab')],
      ['desktop width', page.getByLabel('Desktop')],
      ['tablet width', page.getByLabel('Tablet')],
      ['mobile width', page.getByLabel('Mobile')],
    ]

    let measured = 0
    for (const [name, locator] of targets) {
      if ((await locator.count()) === 0) continue
      const box = await rect(locator.first(), name)
      expect(box.width, `${name} is ${box.width}px wide — below the 44px finger target`).toBeGreaterThanOrEqual(44)
      expect(box.height, `${name} is ${box.height}px tall — below the 44px finger target`).toBeGreaterThanOrEqual(44)
      measured += 1
    }
    expect(measured, 'no toolbar control was found by label — the selectors have drifted').toBeGreaterThanOrEqual(4)

    // The chip and Save are wider than they are tall by design, so only the height is a target.
    for (const [name, testId] of [['publish chip', 'publish-chip'], ['save', 'save-project']] as const) {
      const el = page.getByTestId(testId)
      if ((await el.count()) === 0) continue
      const box = await rect(el.first(), name)
      expect(box.height, `${name} is ${box.height}px tall`).toBeGreaterThanOrEqual(44)
    }

    // THE HIT AREA GREW, THE GLYPH DID NOT. Without this the whole change could have been made by
    // scaling the icons up, which is a different and worse product.
    const glyphs: Array<[string, string, string]> = [
      ['back', 'Back to project', '16'],
      ['reload', 'Reload your app', '15'],
      ['open in new tab', 'Open your app in a new tab', '15'],
    ]
    for (const [name, label, expected] of glyphs) {
      const svg = page.getByLabel(new RegExp(`^${label}`)).first().locator('svg').first()
      if ((await svg.count()) === 0) continue
      await expect(svg, `${name}'s glyph changed size — the target should have grown by padding`).toHaveAttribute('width', expected)
    }
  })

  test('the History button is a 44x44 target that overlaps neither Save nor the framed app', async ({ page }) => {
    await openFirstProject(page)

    const toolbar = page.getByTestId('workspace-toolbar')
    await toolbar.waitFor({ state: 'visible', timeout: 30_000 })

    // NEGATIVE CONTROL: the button and the control it must not overlap both rendered. A toolbar
    // that lost either would pass every comparison below by having nothing to compare.
    const history = page.getByTestId('toolbar-history')
    const save = page.getByTestId('save-project')
    await expect(history, 'the History button is not in the toolbar').toBeAttached()
    await expect(save, 'Save is not in the toolbar — the overlap check below would be vacuous').toBeAttached()

    const historyBox = await rect(history, 'the History button')
    const saveBox = await rect(save, 'Save')
    expect(historyBox.width, `History is ${historyBox.width}px wide`).toBeGreaterThanOrEqual(44)
    expect(historyBox.height, `History is ${historyBox.height}px tall`).toBeGreaterThanOrEqual(44)

    expect(historyBox.x, 'History is not after Save').toBeGreaterThanOrEqual(saveBox.x + saveBox.width - 1)
    expect(overlaps(historyBox, saveBox), 'the History button overlaps Save').toBe(false)

    // The pane is measured through its own testid, so a stopped app, whose pane has no frame,
    // still has a rectangle to be clear of.
    const pane = page.getByTestId('app-pane-region')
    if ((await pane.count()) > 0 && (await pane.isVisible())) {
      const paneBox = await rect(pane, 'the app pane')
      expect(overlaps(historyBox, paneBox), 'the History button overlaps the app pane').toBe(false)
    }
    const frame = page.locator('iframe[title="App Preview"]')
    if ((await frame.count()) > 0) {
      const frameBox = await rect(frame.first(), 'the app frame')
      expect(overlaps(historyBox, frameBox), 'the History button overlaps the framed app').toBe(false)
    }
  })

  test('the row scrolls, so nothing is stranded off-screen with no way to reach it', async ({ page }) => {
    await openFirstProject(page)

    const toolbar = page.getByTestId('workspace-toolbar')
    await toolbar.waitFor({ state: 'visible', timeout: 30_000 })

    const geometry = await toolbar.evaluate((el) => {
      const before = el.scrollLeft
      el.scrollLeft = el.scrollWidth
      const after = el.scrollLeft
      el.scrollLeft = before
      return { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, reached: after }
    })

    // The premise: at 360px the row genuinely does not fit. If this ever stops being true the
    // assertion below becomes vacuous, so it is asserted rather than assumed.
    expect(
      geometry.scrollWidth,
      'the toolbar fits at 360px, so the scroll assertion below proves nothing — re-derive this test',
    ).toBeGreaterThan(geometry.clientWidth)

    // THIS IS THE ASSERTION THAT FAILS ON main. With `overflow-hidden` on the clipping ancestor,
    // scrollLeft stays pinned at 0 however far you push it: the controls past the fold are not
    // merely off-screen, they are unreachable by any input.
    expect(
      geometry.reached,
      'the toolbar cannot be scrolled — controls past the right edge are unreachable, which is #201',
    ).toBeGreaterThan(0)
  })
})

test.describe('the workspace toolbar above the stacking threshold', () => {
  test.describe.configure({ timeout: 180_000 })

  test.use({ viewport: WIDE })

  test('the desktop row is untouched by the narrow-width work', async ({ page }) => {
    await openFirstProject(page)

    const toolbar = page.getByTestId('workspace-toolbar')
    await toolbar.waitFor({ state: 'visible', timeout: 30_000 })

    // At desktop width the row fits, so it is not a scroller. This is the guard against the
    // narrow-width fix leaking upward and putting a scrollbar in every desktop workspace.
    const fits = await toolbar.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)
    expect(fits, 'the toolbar overflows at 1200px — the narrow-width rule leaked above its breakpoint').toBe(true)

    // And the targets are back to their compact sizes: the 44px floor must not apply here.
    const back = page.getByLabel(/^Back to project/).first()
    if ((await back.count()) > 0) {
      const box = await rect(back, 'back control at desktop width')
      expect(box.height, 'the 44px floor leaked above the narrow breakpoint').toBeLessThan(44)
    }
  })
})

test.describe('the platform draws no chrome over the citizen’s own app', () => {
  test.describe.configure({ timeout: 240_000 })

  test.use({ viewport: WIDE })

  /**
   * Nothing the platform draws may overlap the citizen's own app — stated in the form that
   * SURVIVES the chips being deleted.
   *
   * The chips are gone, so "the chip's rectangle and the app's controls are disjoint" is now
   * trivially true — there is no chip. Asserting that would be a scenario that passes because
   * its subject no longer exists. So the durable property is asserted instead, over whatever the
   * pane happens to draw: while the frame is revealed and not covered, nothing the platform owns
   * overlaps any control inside the frame. That catches the NEXT overlay somebody adds.
   *
   * ── THE SCOPE CLAUSE IS LOAD-BEARING, NOT HEDGING ─────────────────────────────────────────
   * `BouncingWait` is `absolute inset-0 z-20` with an opaque background and DELIBERATELY covers
   * the entire frame — controls included — in the cover, loading and stalled states. Stated
   * without the qualifier this rule is false by design in three shipped states, and a later
   * reader applying it literally would file a regression against correct behaviour. So the test
   * establishes the frame is revealed and uncovered FIRST, and skips honestly if it is not.
   */
  test('no platform-owned element overlaps a control inside the framed app', async ({ page }) => {
    await openFirstProject(page)

    const frame = page.locator('iframe[title="App Preview"]')
    if ((await frame.count()) === 0) {
      test.skip(true, 'no app is framed on this target — start one to run this')
      return
    }
    await frame.first().waitFor({ state: 'attached', timeout: 120_000 })

    // THE SCOPE GATE. A cover over the pane is correct behaviour; measuring through it would
    // report a defect against a working product.
    const covered = await page.locator('[data-testid="app-pane-wait"], [data-testid="app-pane-cover"]').count()
    if (covered > 0) {
      test.skip(true, 'the pane is covered (starting, loading or stalled) — the rule does not apply in those states')
      return
    }

    const frameBox = await rect(frame.first(), 'the app frame')

    // NEGATIVE CONTROL, and it is the whole reason this test means anything: enumerate the real
    // controls the citizen's app draws. A page that rendered nothing has no overlaps either.
    // `frameLocator` addresses the frame BY SELECTOR — never index into `page.frames()`, whose
    // order is not stable across a reload and which carries the app's own child frames.
    const appControls = page
      .frameLocator('iframe[title="App Preview"]')
      .locator('a, button, [role="button"], nav a, input, select')
    const controlCount = await appControls.count()
    expect(
      controlCount,
      'no controls were found inside the framed app — it did not render, so "nothing overlaps it" is vacuous',
    ).toBeGreaterThanOrEqual(2)

    // Everything the PLATFORM draws inside the pane's own box, positioned over the frame. This is
    // deliberately enumerated by position rather than by a known list of chips, so an overlay
    // added later is caught without this file being edited.
    const platformOverlays = await page.evaluate((box) => {
      const iframe = document.querySelector('iframe[title="App Preview"]')
      if (!iframe) return []
      const out: Array<{ label: string; x: number; y: number; width: number; height: number }> = []
      for (const el of Array.from(document.querySelectorAll('body *'))) {
        if (el === iframe || el.contains(iframe)) continue
        const style = getComputedStyle(el)
        if (style.position !== 'absolute' && style.position !== 'fixed') continue
        if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue
        const r = el.getBoundingClientRect()
        if (r.width === 0 || r.height === 0) continue
        // Only what actually sits over the frame's rectangle.
        const over = r.x < box.x + box.width && r.x + r.width > box.x && r.y < box.y + box.height && r.y + r.height > box.y
        if (!over) continue
        out.push({
          label: (el.textContent || el.getAttribute('aria-label') || el.tagName).trim().replace(/\s+/g, ' ').slice(0, 40),
          x: r.x, y: r.y, width: r.width, height: r.height,
        })
      }
      return out
    }, frameBox)

    // App-control rectangles are frame-relative; translate them into page coordinates before
    // comparing against the platform's, or every pair looks disjoint by an accident of offset.
    const collisions: string[] = []
    for (let i = 0; i < controlCount; i += 1) {
      const control = appControls.nth(i)
      const box = await control.boundingBox()
      if (!box) continue
      for (const overlay of platformOverlays) {
        if (overlaps(overlay, box)) {
          const name = (await control.innerText().catch(() => '')) || (await control.getAttribute('aria-label')) || '(unnamed)'
          collisions.push(`platform "${overlay.label}" covers app control "${name.trim().slice(0, 40)}"`)
        }
      }
    }

    expect(collisions, `platform chrome is drawn over the citizen's own app:\n  ${collisions.join('\n  ')}`).toEqual([])
  })
})

test.describe('a reviewer’s note never pushes the review action out of view', () => {
  test.describe.configure({ timeout: 180_000 })

  test.use({ viewport: NARROW })

  /**
   * The note is free text capped at 1,000 characters and the rail is 360-640px wide, so the
   * only thing standing between a long note and an unreachable review button is that the note's
   * BOX is bounded rather than its text. A jsdom test can assert both elements exist and learn
   * nothing about which one moved.
   */
  test('a 1,000-character rejection note leaves the review action clickable', async ({ page }) => {
    await openFirstProject(page)

    const note = page.getByTestId('status-row-rejection-note')
    const action = page.getByTestId('status-action')

    // This scenario needs a REJECTED app carrying a long note. Where the fixture is absent the
    // test says so and skips, rather than passing on an app that was never rejected — a skip is
    // honest, a green is a lie.
    if ((await note.count()) === 0) {
      test.skip(true, 'no rejected app with a rejection note on this target — seed one to run this')
      return
    }

    // PREMISE FIRST: the note really is long, and the box really is overflowing. Assert both
    // before any geometry, so a note that failed to render cannot false-pass the rest.
    const text = (await note.innerText()).trim()
    expect(text.length, 'the note is short — this scenario is about the 1,000-character cap').toBeGreaterThan(400)

    const overflowing = await note.evaluate((el) => el.scrollHeight > el.clientHeight)
    expect(overflowing, 'the note box is not overflowing, so bounding it proves nothing here').toBe(true)

    const noteBox = await rect(note, 'the rejection note')
    const actionBox = await rect(action, 'the review action')

    // The BOX is what is bounded, not the text.
    expect(noteBox.height, 'the note box grew with its text instead of scrolling').toBeLessThanOrEqual(140)

    // Geometrically above, not merely earlier in source order.
    expect(noteBox.y + noteBox.height, 'the note is not above the review action on screen').toBeLessThanOrEqual(actionBox.y + 1)

    // And the action is genuinely reachable: inside the viewport, visible, and a real click lands
    // on it rather than on something drawn over it.
    expect(actionBox.y + actionBox.height, 'the review action is below the fold').toBeLessThanOrEqual(NARROW.height)
    await expect(action).toBeVisible()
    await action.click({ trial: true })
  })
})
