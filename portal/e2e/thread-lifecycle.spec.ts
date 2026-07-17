import { test, expect, type Page, type Route } from '@playwright/test'

/**
 * The project thread's whole lifecycle, in a REAL browser (003-U6).
 *
 * WHY THIS EXISTS AS A BROWSER TEST. The jsdom suites drive the same page, but jsdom cannot tell
 * you the page HYDRATED. The campaign lesson was exactly that: a curl-green preview can be
 * interactivity-dead. Every assertion below is downstream of a real click on a real React handler.
 *
 * WHAT IS REAL: auth, the project, and the whole conversations API — the thread is genuinely
 * created, the turns are genuinely persisted, and the transcript is genuinely re-read on reload.
 *
 * WHAT IS SCRIPTED, AND WHY:
 *  - THE RELAY. The interview's ask-vs-brief judgment is the model's, and a live model is
 *    stochastic: the same vague prompt might ask two questions today and one tomorrow. That is
 *    fine as product behaviour and fatal as a gate. The stub pins the LIFECYCLE (a questions turn,
 *    then a brief turn); the protocol's efficacy against the real model is verified manually per
 *    U2's verification step. Both halves are named so neither is silently skipped.
 *  - THE BUILD SESSION. A real build provisions an Azure container and takes minutes. Stubbing the
 *    C3 control surface + its C7 feed keeps this a deterministic seconds-long gate. The real build
 *    is covered by the backend composition tests and the manual E2E round.
 *
 * So: this proves the PORTAL lifecycle end to end. It does not claim to prove Azure.
 */

const QUESTIONS =
  'A couple of things first: which terminals should this cover, and who approves a visitor?'
const BRIEF =
  'Build an application for Bengaluru International Airport (BIAL) that tracks visitor passes across T1 and T2, with host approval.'
const PREVIEW_URL = 'https://sbx-e2e.westeurope.azurecontainerapps.io/'
const SESSION_ID = '01931f7a-0000-7000-8000-0000000000e2'

/** One SSE relay reply, in the two-frame wire shape the SPA parses. */
const relayBody = (text: string) =>
  `data: ${JSON.stringify({ delta: { text } })}\n\ndata: [DONE]\n\n`

/** One C7 feed frame: `id: {seq}` + the snake_case envelope. */
const frame = (env: Record<string, unknown>) => `id: ${env.seq}\ndata: ${JSON.stringify(env)}\n\n`

/**
 * Script the relay: the FIRST turn asks, the SECOND emits the brief. That ordering is the
 * product claim under test — a vague prompt must not go straight to a build.
 */
async function scriptRelay(page: Page): Promise<{ turns: () => number }> {
  let turns = 0
  await page.route('**/api/claude', async (route: Route) => {
    turns += 1
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: relayBody(turns === 1 ? QUESTIONS : `Here's what I'll build:\n\n\`\`\`bial:build-brief\n${BRIEF}\n\`\`\``),
    })
  })
  return { turns: () => turns }
}

/**
 * Script the C3 control surface + its C7 feed.
 *
 * `terminal` decides whether the feed runs to `ended`. Both halves are needed and neither is
 * optional: a terminal session COLLAPSES the dead frame (the sandbox is gone with it), so the
 * framed preview can only be asserted while the build is still live — and the outcome record can
 * only be asserted once it is over. One stream cannot show both.
 */
async function scriptBuild(
  page: Page,
  { terminal = true }: { terminal?: boolean } = {},
): Promise<{ starts: () => Array<Record<string, unknown>> }> {
  const starts: Array<Record<string, unknown>> = []

  await page.route('**/api/build-sessions', async (route: Route) => {
    starts.push(JSON.parse(route.request().postData() || '{}'))
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        sessionId: SESSION_ID,
        projectId: starts[starts.length - 1].projectId,
        appId: 'a1',
        status: 'provisioning',
        previewUrl: null,
        createdAt: new Date().toISOString(),
      }),
    })
  })

  await page.route('**/api/build-sessions/*/events', async (route: Route) => {
    const live =
      frame({ type: 'step', seq: 1, name: 'scaffold', label: 'Scaffolding your app…', state: 'started' }) +
      frame({ type: 'log', seq: 2, source: 'exec', stream: 'stdout', text: 'added 312 packages' }) +
      frame({ type: 'preview_ready', seq: 3, preview_url: PREVIEW_URL })
    const end =
      frame({ type: 'ended', seq: 4, status: 'ended', preview_url: PREVIEW_URL, snapshot_committed: true, reason: 'completed' }) +
      'data: [DONE]\n\n'
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: terminal ? live + end : live,
    })
  })

  // The keep-alive + status surfaces the hook drives while a session is live.
  await page.route('**/api/build-sessions/*/heartbeat', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ sessionId: SESSION_ID, alive: true, cadenceSeconds: 30, heartbeatExpiresAt: 'e' }) }),
  )
  await page.route('**/api/build-sessions/*/lock/**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ sessionId: SESSION_ID, held: true, ownerUserId: 'u', ttlSeconds: 900, expiresAt: 'e' }) }),
  )
  await page.route('**/api/build-sessions/*', async (route: Route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        sessionId: SESSION_ID, projectId: 'p1', appId: 'a1',
        status: terminal ? 'ended' : 'ready',
        previewUrl: PREVIEW_URL, lastSeq: 4, createdAt: 'c', updatedAt: 'u',
      }),
    })
  })

  return { starts: () => starts }
}

async function createProject(page: Page, name: string): Promise<string> {
  await page.goto('/projects')
  await page.getByRole('button', { name: /new project/i }).first().click()
  await page.getByPlaceholder(/VIP Movement Tracker/i).fill(name)
  await page.getByRole('button', { name: /create project/i }).click()
  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}/)
  return page.url().split('/projects/')[1]
}

const composer = (page: Page) => page.getByPlaceholder(/describe what you need/i)

test.describe('the project thread lifecycle', () => {
  test('vague prompt → questions → answer → brief → build → outcome, without leaving the page', async ({ page }) => {
    const relay = await scriptRelay(page)
    const build = await scriptBuild(page)
    const projectId = await createProject(page, `E2E Thread ${Date.now()}`)

    // 1. The project composer routes into the project's ONE thread — a real round-trip through
    //    the canonical get-or-create, not a client-minted chat id.
    await page.getByPlaceholder(/Describe the app you want to build/i).fill('I need a visitor app')
    await page.getByRole('button', { name: /generate app/i }).click()
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)
    const threadUrl = page.url()

    // 2. A vague prompt gets QUESTIONS, and nothing is built.
    await expect(page.getByText(/which terminals should this cover/i)).toBeVisible()
    expect(build.starts()).toHaveLength(0)
    await expect(page.getByTestId('build-brief-card')).toHaveCount(0)

    // 3. The user answers — proving the page is genuinely interactive, not just server-rendered.
    await composer(page).fill('T1 and T2. The host approves.')
    await composer(page).press('Enter')

    // 4. The brief arrives as a CARD, with the fence hidden.
    const card = page.getByTestId('build-brief-card')
    await expect(card).toBeVisible()
    await expect(card).toContainText('tracks visitor passes across T1 and T2')
    await expect(page.getByText('bial:build-brief')).toHaveCount(0)
    expect(relay.turns()).toBe(2)
    expect(build.starts()).toHaveLength(0) // still a proposal

    // 5. One click builds — with the REFINED brief and the thread's id, not the typed text.
    await card.getByRole('button', { name: /build this/i }).click()
    await expect.poll(() => build.starts().length).toBe(1)
    expect(build.starts()[0]).toMatchObject({ projectId, prompt: BRIEF })
    expect(build.starts()[0].conversationId).toBeTruthy()

    // 6. The build is visible as it runs — the feed streams into the page with no refresh.
    await expect(page.getByText(/Scaffolding your app/i)).toBeVisible()
    await expect(page.getByText(/added 312 packages/i)).toBeVisible()

    // 7. The outcome is recorded in the transcript, carrying the preview link.
    const outcome = page.getByTestId('build-outcome')
    await expect(outcome).toBeVisible()
    await expect(outcome).toContainText(/build finished/i)
    await expect(outcome.getByRole('link')).toHaveAttribute('href', PREVIEW_URL)

    // The whole lifecycle happened at ONE address — the two-chat handoff is gone.
    expect(page.url()).toBe(threadUrl)
  })

  test('a LIVE build frames its sandbox preview in the page', async ({ page }) => {
    // Split from the lifecycle test because a terminal session collapses the dead frame (the
    // sandbox is gone with it) — the framed preview only exists while the build is live.
    await scriptRelay(page)
    await scriptBuild(page, { terminal: false })
    await createProject(page, `E2E Preview ${Date.now()}`)

    await page.getByPlaceholder(/Describe the app you want to build/i).fill('I need a visitor app')
    await page.getByRole('button', { name: /generate app/i }).click()
    await expect(page.getByText(/which terminals should this cover/i)).toBeVisible()
    await composer(page).fill('T1 and T2. The host approves.')
    await composer(page).press('Enter')
    await page.getByTestId('build-brief-card').getByRole('button', { name: /build this/i }).click()

    await expect(page.locator('iframe')).toHaveAttribute('src', PREVIEW_URL)
    await expect(page.getByText(/preview is live/i)).toBeVisible()
  })

  test('the thread survives a reload: transcript restored, still buildable', async ({ page }) => {
    await scriptRelay(page)
    await scriptBuild(page)
    await createProject(page, `E2E Reload ${Date.now()}`)
    await page.getByPlaceholder(/Describe the app you want to build/i).fill('I need a visitor app')
    await page.getByRole('button', { name: /generate app/i }).click()
    await expect(page.getByText(/which terminals should this cover/i)).toBeVisible()
    await composer(page).fill('T1 and T2. The host approves.')
    await composer(page).press('Enter')
    await page.getByTestId('build-brief-card').getByRole('button', { name: /build this/i }).click()
    await expect(page.getByTestId('build-outcome')).toBeVisible()

    await page.reload()

    // The persisted transcript comes back — interview turns included, in order. (The answer is
    // matched on its own sentence: "T1 and T2" alone also appears inside the brief.)
    await expect(page.getByText(/i need a visitor app/i)).toBeVisible()
    await expect(page.getByText(/which terminals should this cover/i)).toBeVisible()
    await expect(page.getByText(/the host approves\./i)).toBeVisible()
    // The card re-renders from the persisted fence — a reloaded thread is still buildable, with
    // no extra persisted state to keep in sync.
    await expect(page.getByTestId('build-brief-card')).toBeVisible()
    await expect(page.getByTestId('build-brief-card').getByRole('button', { name: /build this/i })).toBeEnabled()

    // NOT asserted here: that the OUTCOME survives the reload. This spec stubs the build session,
    // so no real build ran and the server-side writer (`build_sessions/outcome.py`) — the thing
    // that actually persists the outcome — never fired. Claiming it here would be asserting the
    // stub. Its durability is proven where the real writer runs:
    // `backend/tests/api/v1/build_sessions/test_outcome_composition.py`, and the no-duplicate rule
    // in `BuilderPage-outcome.test.jsx`.
  })

  test('opening the project again lands in the SAME thread', async ({ page }) => {
    await scriptRelay(page)
    await scriptBuild(page)
    const projectId = await createProject(page, `E2E Canonical ${Date.now()}`)

    await page.getByPlaceholder(/Describe the app you want to build/i).fill('I need a visitor app')
    await page.getByRole('button', { name: /generate app/i }).click()
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)
    const first = page.url()

    // A second build send from the project composer must resolve the same thread — this is the
    // claim that stops a project accumulating one-shot build chats.
    await page.goto(`/projects/${projectId}`)
    await page.getByPlaceholder(/Describe the app you want to build/i).fill('actually, add badges')
    await page.getByRole('button', { name: /generate app/i }).click()
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)

    expect(page.url()).toBe(first)
  })
})
