import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * An application's chat history, in a real browser: the Last chat card, the Chats list in the rail,
 * paging, rename and delete. jsdom has no layout, so the app pane staying put and the pager
 * sitting at the foot of the panel are only provable here.
 *
 * WHERE THIS RUNS. A REAL session (`E2E_STORAGE_STATE`, see `auth.setup.ts`): the chats are created
 * through the API, so a mocked `/auth/me` has no cookies to send and the suite skips itself.
 * Against the portal CONTAINER, not `vite dev`, for the same reason as `workspace-geometry.spec.ts`.
 * The continuation journeys at the foot need a real model and sandbox and run only with
 * `E2E_REAL_SANDBOX=1`, the gate `real-sandbox.spec.ts` uses.
 *
 * WHAT IT TOUCHES. It finds one of the signed-in user's applications, preferring one that serves an
 * app, and seeds header-only chats (no model turn, no tokens) under a unique title prefix. Every
 * search, assertion and cleanup is scoped to that prefix, so whatever else the account holds is
 * neither read into a result nor changed. `afterAll` deletes whatever is left of the seeded chats,
 * and `beforeAll` first deletes any an interrupted run left behind. Opening the application starts
 * its app container, as it would for the user: with one workspace per user, do not run this while
 * the same login is building elsewhere.
 *
 * THE RULE EVERY TEST HERE FOLLOWS, as in `workspace-geometry.spec.ts`: an absence is paired with
 * a liveness assertion, and a measurement is preceded by proof that the thing rendered.
 */

const AUTH_FILE = path.join(path.dirname(fileURLToPath(import.meta.url)), '../playwright/.auth/user.json')
const REAL_SANDBOX = process.env.E2E_REAL_SANDBOX === '1'

const WIDE = { width: 1280, height: 900 }
/** The list's fixed page size (`CHAT_PAGE_SIZE`). */
const PAGE_SIZE = 8
const SEEDED = 12

interface WireChat {
  _id: string
  kind: string
  title?: string
  projectId: string | null
  updatedAt: string
}

interface Seeded {
  id: string
  title: string
  kind: 'plan' | 'build'
}

interface WireProject {
  id: string
  name: string
  access: string
  isServing: boolean
}

const PREFIX = 'e2e-chathist-'
const run = `${PREFIX}${Date.now()}`
const titleOf = (n: number) => `${run} item-${String(n).padStart(2, '0')}`

let api: APIRequestContext
let csrf: Record<string, string> = {}
let ready = false
let projectId = ''
let serving = false
const seeded: Seeded[] = []

async function rect(locator: Locator, what: string) {
  const box = await locator.boundingBox()
  expect(box, `${what} has no bounding box — it did not render, so nothing below it is measured`).not.toBeNull()
  return box!
}

function csrfOf(cookies: Array<{ name: string; value: string }>): Record<string, string> {
  const cookie = cookies.find((c) => c.name === 'csrf' || c.name === '__Host-csrf')
  return cookie ? { 'X-CSRF-Token': cookie.value } : {}
}

async function ownedProjects(request: APIRequestContext): Promise<WireProject[]> {
  const res = await request.get('/api/projects?limit=50')
  expect(res.status(), 'GET /api/projects').toBe(200)
  const body = (await res.json()) as { items?: WireProject[] }
  return (body.items ?? []).filter((p) => p.access !== 'shared')
}

async function chatsOf(request: APIRequestContext, project: string): Promise<WireChat[]> {
  const res = await request.get(`/api/conversations?projectId=${project}`)
  expect(res.status(), 'GET /api/conversations').toBe(200)
  return ((await res.json()) as { conversations: WireChat[] }).conversations
}

async function seedChat(title: string, kind: Seeded['kind']): Promise<Seeded> {
  const id = crypto.randomUUID()
  const res = await api.post('/api/conversations', { headers: csrf, data: { id, projectId, kind, title } })
  expect(res.status(), `seeding "${title}"`).toBeLessThan(300)
  const chat = { id, title, kind }
  seeded.push(chat)
  return chat
}

async function openList(page: Page, search = '') {
  await page.goto(`/projects/${projectId}/chats${search}`)
  await expect(page.getByTestId('chat-history')).toBeVisible()
}

const searchBox = (page: Page) => page.getByLabel('Search chats')
const rows = (page: Page) => page.getByTestId('chat-row')
const pager = (page: Page) => page.getByRole('navigation', { name: 'Chats pagination' })

/**
 * Scrolls whatever scrolls the element to its end, then reads the element's top. A pinned footer
 * lands in the same place however many rows sit above it; a footer that floats up under a short
 * list lands higher. At scroll end both a short and a long list are comparable.
 */
async function topAtScrollEnd(locator: Locator, what: string): Promise<number> {
  await locator.evaluate((el) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const overflow = getComputedStyle(p).overflowY
      if ((overflow === 'auto' || overflow === 'scroll') && p.scrollHeight > p.clientHeight) {
        p.scrollTop = p.scrollHeight
        return
      }
    }
  })
  return (await rect(locator, what)).y
}

test.describe('chat history on an application', () => {
  test.describe.configure({ mode: 'serial', timeout: 120_000 })
  test.use({ viewport: WIDE })

  test.beforeAll(async ({ playwright }, testInfo) => {
    if (!fs.existsSync(AUTH_FILE)) return
    api = await playwright.request.newContext({
      baseURL: testInfo.project.use.baseURL,
      storageState: AUTH_FILE,
    })
    csrf = csrfOf((await api.storageState()).cookies)
    if (Object.keys(csrf).length === 0) return

    const mine = await ownedProjects(api)
    if (mine.length === 0) return
    const chosen = mine.find((p) => p.isServing) ?? mine[0]!
    projectId = chosen.id
    serving = chosen.isServing
    ready = true

    for (const chat of await chatsOf(api, projectId)) {
      if (!chat.title?.startsWith(PREFIX)) continue
      const res = await api.delete(`/api/conversations/${chat._id}`, { headers: csrf })
      expect(res.status() < 300 || res.status() === 404, `removing "${chat.title}" left by an earlier run`).toBe(true)
    }
    for (let n = 1; n <= SEEDED; n += 1) await seedChat(titleOf(n), n % 2 === 0 ? 'build' : 'plan')
  })

  test.afterAll(async () => {
    if (!api) return
    const failed: string[] = []
    for (const chat of seeded) {
      const res = await api.delete(`/api/conversations/${chat.id}`, { headers: csrf })
      if (res.status() >= 300 && res.status() !== 404) failed.push(`${chat.title} (${res.status()})`)
    }
    await api.dispose()
    expect(failed, `seeded chats left behind:\n  ${failed.join('\n  ')}`).toEqual([])
  })

  test.beforeEach(() => {
    test.skip(!ready, 'needs a real session (E2E_STORAGE_STATE) holding at least one application of the user’s own')
  })

  test('the last chat sits under the composer and View all opens the list while the app pane stays put', async ({
    page,
  }) => {
    const frameRequests: string[] = []
    page.on('request', (req) => {
      if (req.isNavigationRequest() && req.frame() !== page.mainFrame()) frameRequests.push(req.url())
    })

    await page.goto(`/projects/${projectId}`)

    const newest = seeded[seeded.length - 1]!
    const card = page.getByTestId('last-chat-card')
    await expect(card).toBeVisible()
    await expect(card).toHaveAttribute('href', new RegExp(`/chat/${newest.id}$`))
    await expect(card).toContainText(newest.title)

    const send = await rect(page.getByTestId('composer-send'), 'the composer’s send control')
    const cardBox = await rect(card, 'the last chat card')
    expect(cardBox.y, 'the last chat card is not under the composer').toBeGreaterThanOrEqual(send.y + send.height)

    const pane = page.getByTestId('app-pane-region')
    await expect(pane).toBeVisible()
    const frame = page.locator('iframe[title="App Preview"]')
    // An application that serves an app must frame it; the container can take a while to start.
    // One that serves nothing has no frame to keep, and the report says that half did not run.
    if (serving) await expect(frame.first(), 'the serving application never framed its app').toBeAttached({ timeout: 180_000 })
    else test.info().annotations.push({ type: 'not run', description: 'the chosen application serves no app, so the framed-app half did not run' })
    const framed = serving

    // An element identity that survives only if the node is never unmounted: a JS property the
    // framework knows nothing about is gone the moment React replaces the element.
    const token = `pane-${Date.now()}`
    await pane.evaluate((el, t) => Reflect.set(el, '__e2eMark', t), token)
    if (framed) await frame.first().evaluate((el, t) => Reflect.set(el, '__e2eMark', t), token)
    if (framed) {
      // The frame's own first load (the sandbox comes up after the page does) is not what is under test.
      const inner = await (await frame.first().elementHandle())!.contentFrame()
      await inner?.waitForLoadState('load')
    }
    frameRequests.length = 0
    const before = await rect(pane, 'the app pane before View all')

    await page.getByRole('link', { name: /view all/i }).click()
    await expect(page).toHaveURL(new RegExp(`/projects/${projectId}/chats$`))
    await expect(page.getByTestId('chat-history')).toBeVisible()
    await expect(rows(page).first()).toBeVisible()
    await expect(page.getByTestId('toolbar-history')).toHaveAttribute('aria-pressed', 'true')

    const after = await rect(pane, 'the app pane after View all')
    for (const edge of ['x', 'y', 'width', 'height'] as const) {
      expect(Math.abs(after[edge] - before[edge]), `the app pane moved: ${edge} ${before[edge]} → ${after[edge]}`).toBeLessThanOrEqual(1)
    }
    expect(await pane.evaluate((el) => Reflect.get(el, '__e2eMark')), 'the app pane was replaced, not kept').toBe(token)
    if (framed) {
      expect(await frame.first().evaluate((el) => Reflect.get(el, '__e2eMark')), 'the framed app was replaced').toBe(token)
    }
    expect(frameRequests, 'the frame requested a new document when the list opened').toEqual([])

    await page.getByTestId('toolbar-history').click()
    await expect(page).toHaveURL(new RegExp(`/projects/${projectId}$`))
    await expect(card).toBeVisible()
    await expect(page.getByTestId('toolbar-history')).toHaveAttribute('aria-pressed', 'false')
    expect(await pane.evaluate((el) => Reflect.get(el, '__e2eMark')), 'the app pane was replaced on the way back').toBe(token)
  })

  test('a search and a second page survive opening a chat and coming back', async ({ page }) => {
    await openList(page)

    await searchBox(page).fill(run)
    await expect(rows(page)).toHaveCount(PAGE_SIZE)
    await expect(page.getByTestId('chat-count')).toHaveText(String(SEEDED))

    await pager(page).getByRole('button', { name: '2', exact: true }).click()
    await expect(rows(page)).toHaveCount(SEEDED - PAGE_SIZE)
    await expect(page).toHaveURL(/page=2/)

    const second = rows(page).first().getByRole('link')
    const openedTitle = (await second.getByText(run).innerText()).trim()
    expect(openedTitle, 'the row opened is one of the seeded chats').toContain(run)
    await second.click()

    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}/)
    await expect(page.getByTestId('all-chats-strip')).toBeVisible()
    await expect(page.getByTestId('all-chats-strip-title')).toHaveText(openedTitle)

    await page.getByRole('link', { name: 'Back to all chats' }).click()
    await expect(page).toHaveURL(new RegExp(`/projects/${projectId}/chats\\?`))
    await expect(page).toHaveURL(/page=2/)
    await expect(searchBox(page)).toHaveValue(run)
    await expect(rows(page)).toHaveCount(SEEDED - PAGE_SIZE)
    await expect(rows(page).filter({ hasText: openedTitle })).toHaveCount(1)
  })

  test('the Build tab narrows the list, and a search that finds nothing offers Clear filters', async ({ page }) => {
    await openList(page, `?q=${encodeURIComponent(run)}`)
    await expect(rows(page)).toHaveCount(PAGE_SIZE)

    const builds = seeded.filter((c) => c.kind === 'build').length
    await page.getByTestId('chat-kind-tabs').getByRole('radio', { name: /^build/i }).click()
    await expect(page).toHaveURL(/kind=build/)
    await expect(rows(page)).toHaveCount(builds)
    // Liveness first (the rows above rendered), then the absence of the other kind.
    await expect(rows(page).getByText(/build chat/i)).toHaveCount(builds)
    await expect(rows(page).getByText(/plan chat/i)).toHaveCount(0)

    await searchBox(page).fill(`${run} no-such-chat`)
    await expect(page.getByTestId('chat-history-message')).toContainText('No chats match')
    await expect(page.getByTestId('chat-kind-tabs')).toBeVisible()
    await expect(searchBox(page)).toBeVisible()

    await page.getByRole('button', { name: 'Clear filters' }).click()
    await expect(searchBox(page)).toHaveValue('')
    await expect(rows(page).first()).toBeVisible()
    await expect(page).not.toHaveURL(/kind=/)
  })

  test('the pager sits at the same offset with one row, a partial page and a full page', async ({ page }) => {
    await openList(page)
    const footer = page.getByTestId('chat-history-footer')
    const panel = page.getByTestId('chat-history')

    const measure = async (expectedRows: number, what: string) => {
      // NEGATIVE CONTROL: the rows the state names are on screen, so a crashed panel cannot pass.
      await expect(rows(page), `${what}: row count`).toHaveCount(expectedRows)
      await expect(footer).toBeVisible()
      const top = await topAtScrollEnd(footer, `the footer with ${what}`)
      const foot = await rect(footer, `the footer with ${what}`)
      const panelBox = await rect(panel, 'the chats panel')
      // The footer is at the panel's foot, not floating under the rows.
      expect(
        panelBox.y + panelBox.height - (foot.y + foot.height),
        `the footer is not at the foot of the panel with ${what}`,
      ).toBeLessThanOrEqual(24)
      return top
    }

    await searchBox(page).fill(`${run} item-01`)
    const one = await measure(1, '1 row')

    await searchBox(page).fill(run)
    const full = await measure(PAGE_SIZE, 'a full page')

    await pager(page).getByRole('button', { name: '2', exact: true }).click()
    const partial = await measure(SEEDED - PAGE_SIZE, 'a partial page')

    expect(Math.abs(one - full), `pager top moved between 1 row (${one}) and a full page (${full})`).toBeLessThanOrEqual(1)
    expect(Math.abs(partial - full), `pager top moved between a partial page (${partial}) and a full page (${full})`).toBeLessThanOrEqual(1)
  })

  test('renaming a chat keeps the new name and its place in the list after a reload', async ({ page }) => {
    const target = seeded[5]!
    const renamed = `${run} renamed`
    const before = (await chatsOf(api, projectId)).filter((c) => c.title?.startsWith(run))
    const placeBefore = before.findIndex((c) => c._id === target.id)
    const stampBefore = before[placeBefore]?.updatedAt
    expect(placeBefore, 'the chat to rename is in the list').toBeGreaterThanOrEqual(0)

    await openList(page, `?q=${encodeURIComponent(run)}&page=${Math.floor(placeBefore / PAGE_SIZE) + 1}`)
    const row = rows(page).filter({ hasText: target.title })
    await expect(row).toHaveCount(1)

    await row.getByRole('button', { name: /^More actions for/ }).click()
    await page.getByRole('menuitem', { name: 'Rename' }).click()
    const field = page.getByLabel('Chat name')
    await expect(field).toBeFocused()
    await field.fill(renamed)
    await field.press('Enter')
    await expect(rows(page).filter({ hasText: renamed })).toHaveCount(1)
    await expect(field).toHaveCount(0)

    seeded[5] = { ...target, title: renamed }

    await page.reload()
    await expect(rows(page).filter({ hasText: renamed })).toHaveCount(1)
    await expect(rows(page).filter({ hasText: target.title })).toHaveCount(0)

    const after = (await chatsOf(api, projectId)).filter((c) => c.title?.startsWith(run))
    expect(after.map((c) => c._id), 'a rename reordered the list').toEqual(before.map((c) => c._id))
    expect(after[placeBefore]?.updatedAt, 'a rename changed the Updated time').toBe(stampBefore)
  })

  test('deleting a chat removes it for good', async ({ page }) => {
    const target = seeded[seeded.length - 3]!
    await openList(page, `?q=${encodeURIComponent(target.title)}`)
    const row = rows(page).filter({ hasText: target.title })
    await expect(row).toHaveCount(1)

    await row.getByRole('button', { name: /^More actions for/ }).click()
    await page.getByRole('menuitem', { name: /^Delete/ }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toContainText('Delete this chat?')
    await expect(dialog).toContainText('Your application and its saved versions are not affected')
    await page.getByTestId('delete-chat-confirm').click()

    await expect(page.getByTestId('chat-history-message')).toContainText('No chats match')
    seeded.splice(seeded.indexOf(target), 1)

    await page.reload()
    await expect(page.getByTestId('chat-history-message')).toContainText('No chats match')
    // The list itself is alive: the other seeded chats are still there.
    await searchBox(page).fill(run)
    await expect(rows(page).first()).toBeVisible()
    await expect(rows(page).filter({ hasText: target.title })).toHaveCount(0)
    expect((await chatsOf(api, projectId)).some((c) => c._id === target.id), 'the server still holds the chat').toBe(false)
  })
})

/**
 * CONTINUATION IN A BROWSER — OPT-IN ONLY, behind E2E_REAL_SANDBOX=1, the gate
 * `real-sandbox.spec.ts` uses. These send real messages: they need the model (FOUNDRY__*) and a
 * real sandbox (SANDBOX__*), and a Build chat provisions a container and spends tokens. Each test
 * stops its turns and deletes its chat when it finishes.
 */
test.describe('a stopped chat continues (opt-in, E2E_REAL_SANDBOX=1)', () => {
  test.skip(!REAL_SANDBOX, 'set E2E_REAL_SANDBOX=1 — these send real messages to a real model and sandbox')
  test.describe.configure({ timeout: 15 * 60_000 })
  test.use({ viewport: WIDE })

  async function startChat(page: Page, project: string, kind: 'plan' | 'build', text: string): Promise<string> {
    await page.goto(`/projects/${project}`)
    await page.getByLabel('What kind of chat').getByRole('radio', { name: kind === 'plan' ? /^plan/i : /^build/i }).click()
    await page.getByTestId('composer-input').fill(text)
    await page.getByTestId('composer-send').click()
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}/)
    return page.url().split('/chat/')[1]!.split(/[?#]/)[0]!
  }

  async function stopTurn(page: Page) {
    const stop = page.getByTestId('stop-turn')
    await expect(stop, 'a turn is running to stop').toBeVisible({ timeout: 120_000 })
    await stop.click()
    await expect(stop).toHaveCount(0, { timeout: 120_000 })
    // Idle again: the composer takes text.
    await expect(page.getByTestId('composer-input')).toBeEditable()
  }

  async function sendFollowUp(page: Page, text: string) {
    await page.getByTestId('composer-input').fill(text)
    await page.getByTestId('composer-send').click()
    await expect(page.getByText(text)).toBeVisible()
    await expect(page.getByTestId('stop-turn'), 'the follow-up started a turn').toBeVisible({ timeout: 120_000 })
  }

  async function removeChat(page: Page, id: string) {
    const headers = csrfOf(await page.context().cookies())
    await expect
      .poll(
        async () => {
          const status = (await page.request.delete(`/api/conversations/${id}`, { headers })).status()
          return status < 300 || status === 404
        },
        { timeout: 60_000 },
      )
      .toBe(true)
  }

  for (const kind of ['plan', 'build'] as const) {
    test(`a ${kind} chat stopped mid-reply takes a new message after ${kind === 'plan' ? 'leaving and returning' : 'a reload'}`, async ({ page }) => {
      const mine = await ownedProjects(page.request)
      test.skip(mine.length === 0, 'the signed-in user has no application of their own')
      const project = mine[0]!.id

      const id = await startChat(page, project, kind, `${run} Describe a simple visitor log for a front desk in two short paragraphs.`)
      try {
        await stopTurn(page)

        if (kind === 'plan') {
          await page.goto('/projects')
          await expect(page.getByTestId('projects-counts')).toBeVisible()
          await page.goto(`/chat/${id}`)
        } else {
          await page.reload()
        }
        await expect(page.getByTestId('all-chats-strip')).toBeVisible()
        await expect(page.getByTestId('composer-input')).toBeEditable()

        await sendFollowUp(page, `${run} Now add one more sentence.`)
        await stopTurn(page)
      } finally {
        await removeChat(page, id)
      }
    })
  }
})
