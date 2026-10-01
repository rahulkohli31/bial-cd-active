import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test'

/**
 * The left navigation's order, the dashboard's "Live applications" tile, and the three-dot menu on
 * My Applications, in a real browser.
 *
 * WHERE THIS RUNS. The navigation and the tile need only a session. The menu tests read the
 * signed-in user's own applications through the API (a REAL session, `E2E_STORAGE_STATE`) and
 * choose one dynamically: a not-live one for the disabled entry, a live one, when the account has
 * one, for the live address. With no such application the test skips and says so, rather than
 * passing on nothing.
 *
 * THE RULE EVERY TEST HERE FOLLOWS, as in `workspace-geometry.spec.ts`: an absence is paired with
 * a liveness assertion, and a measurement is preceded by proof that the thing rendered.
 */

const WIDE = { width: 1280, height: 900 }
const NARROW = { width: 360, height: 800 }

const DESTINATIONS = ['My Applications', 'Shared Applications', 'App Marketplace', 'BIAL Chat']

interface WireProject {
  id: string
  name: string
  access: string
  isServing: boolean
}

/** The user's own applications whose name no other application of theirs shares. */
async function uniquelyNamed(request: APIRequestContext): Promise<WireProject[]> {
  const res = await request.get('/api/projects?limit=50')
  test.skip(res.status() !== 200, `GET /api/projects answered ${res.status()} — this needs a real session`)
  const items = ((await res.json()) as { items?: WireProject[] }).items ?? []
  return items.filter((p) => p.access !== 'shared' && items.filter((q) => q.name === p.name).length === 1)
}

/** The navigation's destination labels, top to bottom, with their vertical positions. */
async function destinationsOf(nav: Locator) {
  const buttons = nav.getByRole('navigation', { name: 'Primary' }).getByRole('button')
  const labels = await buttons.allTextContents()
  const tops: number[] = []
  for (let i = 0; i < labels.length; i += 1) {
    const box = await buttons.nth(i).boundingBox()
    expect(box, `destination ${labels[i]} has no bounding box`).not.toBeNull()
    tops.push(box!.y)
  }
  return { labels: labels.map((l) => l.trim()), tops }
}

function expectFourthIsBialChat(found: { labels: string[]; tops: number[] }, where: string) {
  // Admin, when present, comes after the four; only the first four are the contract.
  expect(found.labels.slice(0, 4), `${where}: destination order`).toEqual(DESTINATIONS)
  const [a, b, c, d] = found.tops
  expect(a! < b! && b! < c! && c! < d!, `${where}: destinations are not stacked in that order on screen (${found.tops.join(', ')})`).toBe(true)
}

async function openMenuFor(page: Page, project: WireProject) {
  await page.goto(`/projects?q=${encodeURIComponent(project.name)}`)
  const trigger = page.getByRole('button', { name: `More actions for ${project.name}`, exact: true })
  await expect(trigger).toBeVisible()
  await trigger.click()
  await expect(page.getByRole('menuitem', { name: /Settings/ })).toBeVisible()
}

test.describe('navigation order', () => {
  test.describe.configure({ timeout: 90_000 })

  test.describe('in the docked rail', () => {
    test.use({ viewport: WIDE })

    test('BIAL Chat is fourth, in the collapsed rail and in the expanded one', async ({ page }) => {
      await page.goto('/projects')
      const rail = page.getByTestId('nav-docked')
      await expect(rail).toBeVisible()

      // PREMISE: the rail is at rest, which is what labels folded to icons means. Without this the
      // "collapsed" half of the test could be measuring the expanded rail twice.
      await expect(page.getByTestId('nav-projects'), 'the rail is not collapsed — the premise of this test').toHaveAttribute(
        'title',
        'My Applications',
      )
      expectFourthIsBialChat(await destinationsOf(rail), 'collapsed rail')

      await rail.hover()
      await expect(page.getByTestId('nav-projects')).not.toHaveAttribute('title', /.+/)
      expectFourthIsBialChat(await destinationsOf(rail), 'expanded rail')
    })
  })

  test.describe('in the mobile drawer', () => {
    test.use({ viewport: NARROW })

    test('BIAL Chat is fourth', async ({ page }) => {
      await page.goto('/projects')
      await page.getByTestId('nav-menu-button').click()
      const drawer = page.getByTestId('nav-floating')
      await expect(drawer).toBeVisible()
      expectFourthIsBialChat(await destinationsOf(drawer), 'drawer')
    })
  })
})

test.describe('My Applications', () => {
  test.describe.configure({ timeout: 90_000 })
  test.use({ viewport: WIDE })

  test('the live count is labelled "Live applications"', async ({ page }) => {
    await page.goto('/projects')
    const counts = page.getByTestId('projects-counts')
    await expect(counts.getByRole('button', { name: /Live applications/ })).toBeVisible()
    // The other tiles rendered, so the absence below is of the old word and not of the whole strip.
    await expect(counts.getByRole('button', { name: /Total applications/ })).toBeVisible()
    await expect(counts).not.toContainText(/In production/i)
  })

  test('Open Application is disabled and says "Not live yet" on an application that is not live', async ({ page }) => {
    const notLive = (await uniquelyNamed(page.request)).find((p) => !p.isServing)
    test.skip(notLive === undefined, 'the account has no application of its own that is not live')

    await openMenuFor(page, notLive!)
    const open = page.getByRole('menuitem', { name: /Open Application/ })
    await expect(open).toHaveAttribute('aria-disabled', 'true')
    await expect(open).toContainText('Not live yet')
    // Settings is in the same open menu, so the absent entry is absent from a menu that rendered.
    await expect(page.getByRole('menuitem', { name: /Settings/ })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: /Copy Production URL/ })).toHaveCount(0)
  })

  test('on a live application Open Application carries the live address and Copy Production URL is offered', async ({
    page,
  }) => {
    const live = (await uniquelyNamed(page.request)).find((p) => p.isServing)
    test.skip(live === undefined, 'the account has no live application of its own — publish one to run this')

    const deployment = await page.request.get(`/api/projects/${live!.id}/deployment`)
    expect(deployment.status(), 'GET deployment').toBe(200)
    const liveUrl = ((await deployment.json()) as { liveUrl?: string | null }).liveUrl
    expect(liveUrl, 'the live application reports no address, so there is nothing to compare').toBeTruthy()

    await openMenuFor(page, live!)
    const open = page.getByRole('menuitem', { name: /Open Application/ })
    await expect(open).toHaveAttribute('href', liveUrl!)
    await expect(open).toHaveAttribute('target', '_blank')
    await expect(open).toHaveAttribute('rel', /noopener/)

    const copy = page.getByRole('menuitem', { name: /Copy Production URL/ })
    await expect(copy).toBeVisible()
    await expect(copy).not.toHaveAttribute('aria-disabled', 'true')
  })
})
