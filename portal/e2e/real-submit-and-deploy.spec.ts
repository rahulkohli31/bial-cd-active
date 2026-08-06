import { test, expect, type Page, request } from '@playwright/test'
import fs from 'node:fs'

/**
 * The FULL real pipeline, end to end, no stubbing anywhere: build a real app against a
 * real Azure sandbox (the same flow `real-sandbox.spec.ts` proves), submit it for
 * approval through the real UI, answer the real six-question modal, watch the real
 * backend auto-score/auto-approve it (V4 Part 2), then drive the real admin
 * `deploy-reconcile` sweep (V4 Part 3) and confirm the resulting `deployed_url` is a
 * genuinely live, separate Container App answering real HTTP.
 *
 * OPT-IN ONLY, same shape as `real-sandbox.spec.ts`: needs the full real substrate
 * (SANDBOX__*, OBJECT_STORE__*, REDIS__*, FOUNDRY__*) wired into the backend's `.env`,
 * PLUS `DEPLOY__AUTO_DEPLOY_ENABLED=true` (the kill switch — off by default everywhere
 * else on purpose). No-ops unless E2E_REAL_SUBMIT_DEPLOY=1.
 *
 * Needs an ADMIN session to call `deploy-reconcile` (no UI button exists for it — it's
 * an operator-invoked endpoint, deliberately, per V4 Part 3's design: this repo has no
 * in-process scheduler). Mint one with:
 *   cd backend && uv run python scripts/mint_session.py \
 *     --email <email in SUPERADMIN_EMAILS> --out .mythos-local/admin.json
 * and point E2E_ADMIN_STORAGE_STATE at the resulting file.
 *
 * TIMEOUTS: inherits real-sandbox.spec.ts's measured ~14-minute first-boot ceiling for
 * the build half (its own comment is the cautionary tale on guessing these — a first
 * estimate was wrong by an order of magnitude until a real monitored run corrected it).
 * The submit->approve->deploy half is NEW and UNMEASURED — the deploy_app step alone
 * took ~64s in a real, isolated `pytest -m integration` run (2026-08-06), so the budget
 * below starts from that plus real margin, not a guess from nothing. CORRECT IT from
 * this spec's own first real run, the same way every other timeout in this file's
 * sibling was corrected.
 */

const REAL = process.env.E2E_REAL_SUBMIT_DEPLOY === '1'
const ADMIN_STATE_PATH = process.env.E2E_ADMIN_STORAGE_STATE

const composer = (page: Page) => page.getByPlaceholder(/describe what you need/i)

async function createProject(page: Page, name: string): Promise<string> {
  await page.goto('/projects')
  await page.getByRole('button', { name: /new project/i }).first().click()
  await page.getByPlaceholder(/VIP Movement Tracker/i).fill(name)
  await page.getByRole('button', { name: /create project/i }).click()
  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}/)
  return page.url().split('/projects/')[1]
}

const CATEGORY_KEYS_HIGH_SCORE = ['credentialsSecrets', 'financialData'] as const // 40+20=60 >= 50
const CATEGORY_KEYS_ALL = [
  'credentialsSecrets',
  'healthData',
  'personalInformation',
  'financialData',
  'confidentialBusinessData',
  'publicData',
] as const

test.describe('real submit -> auto-approve -> auto-deploy (opt-in, E2E_REAL_SUBMIT_DEPLOY=1)', () => {
  test.skip(!REAL, 'set E2E_REAL_SUBMIT_DEPLOY=1 to run this against real Azure end to end')
  test.skip(
    !!REAL && !ADMIN_STATE_PATH,
    'set E2E_ADMIN_STORAGE_STATE to an admin session minted by scripts/mint_session.py',
  )

  test('build a real app, submit with a high-scoring answer set, watch it auto-approve and auto-deploy for real', async ({
    page,
  }) => {
    test.setTimeout(25 * 60_000)

    const projectId = await createProject(page, `E2E Submit+Deploy ${Date.now()}`)

    await page.getByPlaceholder(/Describe the app you want to build/i).fill(
      'Build a simple visitor log: a form to add a visitor name and their host, and a table listing all visitors.',
    )
    await page.getByRole('button', { name: /generate app/i }).click()
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/)

    const card = page.getByTestId('build-brief-card')
    try {
      await expect(card).toBeVisible({ timeout: 45_000 })
    } catch {
      await composer(page).fill('No further requirements — build it as described.')
      await composer(page).press('Enter')
      await expect(card).toBeVisible({ timeout: 45_000 })
    }
    await card.getByRole('button', { name: /build this/i }).click()

    // Real sandbox boot to a real, cross-origin preview — see real-sandbox.spec.ts's own
    // comment for why 12 minutes is a MEASURED ceiling, not a guess.
    const iframe = page.locator('iframe[title="App Preview"]')
    await expect(iframe).toHaveAttribute('src', /^https:\/\/.*\.azurecontainerapps\.io\//, {
      timeout: 12 * 60_000,
    })

    // submit() refuses (409) while a build session lock is held — the build must be
    // stopped first, exactly like real-sandbox.spec.ts's own stop/relaunch tail does.
    await page.getByRole('button', { name: /^stop$/i }).click()
    await expect(page.getByTestId('preview-ended-card')).toBeVisible({ timeout: 60_000 })

    // SubmitControl lives on the project page, not the chat page.
    await page.goto(`/projects/${projectId}`)
    await expect(page.getByTestId('submit-control')).toBeVisible({ timeout: 30_000 })

    await page.getByTestId('submit-for-review').click()
    await expect(page.getByTestId('data-classification-modal')).toBeVisible()

    // A high-scoring answer set (Credentials/Secrets 40 + Financial Data 20 = 60 >=
    // AUTO_APPROVE_AT 50) so the backend auto-APPROVES rather than auto-rejecting —
    // the interesting path for proving deploy, not the rejection path (already covered
    // by test_lifecycle.py's real-DB tests).
    for (const key of CATEGORY_KEYS_ALL) {
      const answer = (CATEGORY_KEYS_HIGH_SCORE as readonly string[]).includes(key) ? 'yes' : 'no'
      await page.getByTestId(`dc-question-${key}-${answer}`).click()
    }
    await page
      .getByTestId('dc-notes')
      .fill('E2E probe: reviewed internally before submission, real end-to-end run.')

    const confirmButton = page.getByTestId('dc-confirm')
    await expect(confirmButton).toBeEnabled()
    await confirmButton.click()

    // The auto-decision happens inside the SAME request as Confirm (V4 Part 2 — no
    // PENDING stop) — so this is the real backend's real score gate, not a poll for an
    // eventual state change.
    await expect(page.getByTestId('data-classification-modal')).not.toBeVisible({
      timeout: 30_000,
    })
    await expect(page.getByTestId('submit-status')).toHaveText(/approved/i, { timeout: 30_000 })

    // deploy-reconcile is operator-invoked (V4 Part 3 — no scheduler in this repo, by
    // design) — call it directly as an admin, the same way a real cron/Logic App would.
    const adminState = JSON.parse(fs.readFileSync(ADMIN_STATE_PATH as string, 'utf8'))
    const adminApi = await request.newContext({
      baseURL: test.info().project.use.baseURL,
      storageState: { cookies: adminState.cookies, origins: [] },
    })
    const reconcileResp = await adminApi.post('/api/admin/apps/deploy-reconcile')
    expect(reconcileResp.ok(), await reconcileResp.text()).toBeTruthy()
    const report = await reconcileResp.json()
    expect(report.attempted).toBeGreaterThanOrEqual(1)

    // Poll the OWNER'S OWN status read for deployedUrl — the same field SubmitControl
    // itself renders as the "Live" link, so this is exactly what a real user would see,
    // not an admin-only signal.
    await expect(page.getByTestId('live-link')).toBeVisible({ timeout: 5 * 60_000 })
    const deployedHref = await page.getByTestId('live-link').locator('a').getAttribute('href')
    expect(deployedHref).toMatch(/^https:\/\/.*\.azurecontainerapps\.io\/$/)

    // THE REAL LIVENESS CLAIM — an actual HTTP GET against the actual deployed app, in
    // a NEW browser context (this is a genuinely separate origin/container from the
    // build sandbox above), not a proxy for it.
    const deployedPage = await page.context().browser()!.newPage()
    const deployedResp = await deployedPage.goto(deployedHref as string, { timeout: 30_000 })
    expect(deployedResp?.status()).toBe(200)
    await deployedPage.close()
    await adminApi.dispose()

    console.log(`DEPLOYED FOR REAL: ${deployedHref}`)
    void projectId
  })
})
