import { test, expect, type Locator, type Page } from '@playwright/test'

/**
 * The Marketplace's List | Grid switch, in a real browser: the remembered view, the links that
 * leave the portal, and the pager's place at the foot of the page. The pager measurement is the
 * part jsdom cannot make, since it has no layout.
 *
 * WHERE THIS RUNS. Any target with a session; the catalog itself is whatever the account can see,
 * so every test finds its rows dynamically and skips, saying why, when the catalog is empty or
 * too small for the scenario. The pager scenario needs more than ten published applications
 * (the page-size control appears only above the smallest size).
 *
 * THE RULE EVERY TEST HERE FOLLOWS, as in `workspace-geometry.spec.ts`: an absence is paired with
 * a liveness assertion, and a measurement is preceded by proof that the thing rendered.
 */

const WIDE = { width: 1280, height: 900 }

const entries = (page: Page) => page.getByTestId('marketplace-entry')
const viewButton = (page: Page, view: 'List' | 'Grid') => page.getByRole('radio', { name: `${view} view` })

/** Opens the Marketplace in a known view and waits for rows, or skips when there are none. */
async function openMarketplace(page: Page, view: 'List' | 'Grid') {
  await page.goto('/marketplace')
  await expect(page.getByTestId('marketplace-search')).toBeVisible()
  await viewButton(page, view).click()
  await expect(viewButton(page, view)).toHaveAttribute('aria-checked', 'true')
  const first = entries(page).first()
  const empty = page.getByTestId('marketplace-empty')
  await first.or(empty).waitFor({ state: 'visible' })
  test.skip((await empty.count()) > 0, 'nothing is published to this Marketplace, so there is no row to show')
  await expect(first).toBeVisible()
}

/** Scrolls whatever scrolls the element to its end and returns the element's bottom edge. */
async function bottomAtScrollEnd(locator: Locator): Promise<number> {
  await locator.evaluate((el) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const overflow = getComputedStyle(p).overflowY
      if ((overflow === 'auto' || overflow === 'scroll') && p.scrollHeight > p.clientHeight) {
        p.scrollTop = p.scrollHeight
        return
      }
    }
  })
  const box = await locator.boundingBox()
  expect(box, 'the pager has no bounding box — it did not render').not.toBeNull()
  return box!.y + box!.height
}

test.describe('the Marketplace list view', () => {
  test.describe.configure({ timeout: 120_000 })
  test.use({ viewport: WIDE })

  test('List replaces the grid, is still List after a reload, and Grid restores', async ({ page }) => {
    await openMarketplace(page, 'Grid')
    // Grid has cards and no table; the cards rendering is what makes the missing table mean something.
    await expect(entries(page).first()).toBeVisible()
    await expect(page.getByRole('table')).toHaveCount(0)

    await viewButton(page, 'List').click()
    await expect(page.getByRole('table')).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Application' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Built by' })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Open' })).toBeAttached()
    await expect(entries(page).first()).toBeVisible()

    await page.reload()
    await expect(entries(page).first()).toBeVisible()
    await expect(page.getByRole('table')).toBeVisible()
    await expect(viewButton(page, 'List')).toHaveAttribute('aria-checked', 'true')

    await viewButton(page, 'Grid').click()
    await expect(entries(page).first()).toBeVisible()
    await expect(page.getByRole('table')).toHaveCount(0)
    await expect(viewButton(page, 'Grid')).toHaveAttribute('aria-checked', 'true')
  })

  test('every list row opens its application in a new tab without handing over the opener', async ({ page }) => {
    await openMarketplace(page, 'List')

    const rows = await entries(page).count()
    const links = page.getByTestId('marketplace-open')
    // One link per row, so a row that lost its link cannot hide behind the others.
    await expect(links).toHaveCount(rows)
    for (let i = 0; i < rows; i += 1) {
      const link = links.nth(i)
      await expect(link, `row ${i + 1} opens in a new tab`).toHaveAttribute('target', '_blank')
      await expect(link, `row ${i + 1} keeps the opener to itself`).toHaveAttribute('rel', /noopener/)
      await expect(link, `row ${i + 1} has an address`).toHaveAttribute('href', /^https?:\/\//)
    }
  })

  test('the pager sits at the same place at 10 and 25 per page, in the list and in the grid', async ({ page }) => {
    await openMarketplace(page, 'List')

    const sizer = page.getByTestId('marketplace-page-size')
    test.skip((await sizer.count()) === 0, 'ten or fewer published applications — the page-size control is not offered')

    const summary = (await page.getByText(/^\d+ published apps?$/).innerText()).trim()
    const total = Number.parseInt(summary, 10)
    expect(total, `could not read the catalog size from "${summary}"`).toBeGreaterThan(10)

    const pager = page.getByTestId('marketplace-pager')
    const bottoms: Array<[string, number]> = []

    for (const view of ['List', 'Grid'] as const) {
      await viewButton(page, view).click()
      await expect(sizer).toHaveAttribute('aria-label', view === 'List' ? 'Rows per page' : 'Cards per page')
      for (const size of [10, 25]) {
        await sizer.click()
        await page.getByRole('option', { name: String(size), exact: true }).click()
        // NEGATIVE CONTROL: the page really holds this many rows, so the pager is measured
        // under the content it is meant to sit below.
        await expect(entries(page)).toHaveCount(Math.min(size, total))
        await expect(pager).toBeVisible()
        // The BOTTOM edge, because the pager's own height differs by a few pixels when the page
        // numbers are absent (25 per page can fit the whole catalog) and it is the foot that is pinned.
        bottoms.push([`${view} at ${size}`, await bottomAtScrollEnd(pager)])
      }
    }

    const [firstLabel, firstBottom] = bottoms[0]!
    for (const [label, bottom] of bottoms.slice(1)) {
      expect(
        Math.abs(bottom - firstBottom),
        `pager moved: ${firstLabel} ends at ${firstBottom}, ${label} at ${bottom}`,
      ).toBeLessThanOrEqual(1)
    }
  })
})
