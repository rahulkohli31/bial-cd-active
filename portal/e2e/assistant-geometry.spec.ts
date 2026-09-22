import { test, expect, type Page } from '@playwright/test'

/**
 * Where BIAL Chat's greeting sits on the screen — a claim only a layout engine can settle.
 *
 * jsdom returns zeroes from `getBoundingClientRect()`, so the unit suite can pin which classes are
 * on which element and nothing more. That is not enough here: the shipped defect had every class
 * a reviewer would look for and still drew the greeting at the top of an empty page, because a
 * percentage minimum height silently resolved to zero against an auto-height parent. A class
 * assertion passes on that. Pixels do not.
 *
 * THE FAILURE THIS GUARDS IS SILENT AND TOTAL. It breaks no test, logs nothing, and leaves the
 * screen looking like a deliberate top-aligned design — which is how it reached production and
 * stayed there for a release.
 *
 * Runs against `vite dev` at :5173 or the container; the greeting needs no sandbox, no project and
 * no model, so unlike `workspace-geometry.spec.ts` this file has no container-only requirement.
 */

/** Heights chosen to bracket a laptop: the centre must track the pane rather than a fixed offset. */
const HEIGHTS = [
  { width: 1440, height: 900 },
  { width: 1280, height: 720 },
  { width: 1680, height: 1050 },
]

/** Everything measured on this page is relative to the scroll pane, never the viewport — the
 *  navigation rail is beside it and the pane is what the page is centred within. */
async function paneAndColumn(page: Page) {
  await page.goto('/assistant')
  await page.getByTestId('assistant-greeting').waitFor({ state: 'visible', timeout: 30_000 })

  const column = page.getByTestId('assistant-column')
  const box = await column.boundingBox()
  expect(box, 'the greeting column has no bounding box — it did not render, so nothing below is measured').not.toBeNull()

  const pane = await page.evaluate(() => {
    const scroller = document.querySelector('[data-testid="assistant-page"]')?.parentElement
    if (!scroller) return null
    const r = scroller.getBoundingClientRect()
    return { top: r.top, bottom: r.bottom, height: r.height }
  })
  expect(pane, 'the page is not inside a scroll pane — the shell changed shape').not.toBeNull()
  return { column: box!, pane: pane! }
}

for (const size of HEIGHTS) {
  test.describe(`the greeting at ${size.width}x${size.height}`, () => {
    test.use({ viewport: size })

    test('★ fills the scroll pane, so centring has something to centre within', async ({ page }) => {
      const { column, pane } = await paneAndColumn(page)
      // Within a pixel of the pane. A column that collapsed to its content measured 299 against a
      // 900px pane, so any tolerance short of "a third of the screen" catches it.
      expect(Math.abs(column.height - pane.height)).toBeLessThanOrEqual(1)
    })

    test('★ sits in the middle of it, with the space above and below equal', async ({ page }) => {
      const { pane } = await paneAndColumn(page)
      // The column's own children, not the column: the column fills the pane by design, and what
      // has to be centred is what it holds.
      const content = await page.evaluate(() => {
        const col = document.querySelector('[data-testid="assistant-column"]')
        const kids = [...(col?.children ?? [])]
        if (!kids.length) return null
        const first = kids[0].getBoundingClientRect()
        const last = kids[kids.length - 1].getBoundingClientRect()
        return { top: first.top, bottom: last.bottom }
      })
      expect(content, 'the greeting column is empty — there is nothing to centre').not.toBeNull()

      const above = content!.top - pane.top
      const below = pane.bottom - content!.bottom
      // Symmetric to within a couple of pixels of sub-pixel rounding. The defect this replaces
      // left 48px above and 649px below.
      expect(Math.abs(above - below)).toBeLessThanOrEqual(2)
      // Negative control: a zero-height content block would satisfy the symmetry above trivially.
      expect(content!.bottom - content!.top).toBeGreaterThan(100)
    })
  })
}
