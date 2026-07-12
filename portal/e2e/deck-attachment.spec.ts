import { test, expect } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const dirname = path.dirname(fileURLToPath(import.meta.url))
const SAMPLE_PPTX = path.join(dirname, 'fixtures/sample.pptx')

// Whether this run targets the built container (vs the dev stack). Only the
// container can prove CSP / single-origin / asset health, so those assertions are
// gated on it (dev is Vite with no CSP and a proxy — green-in-dev can't prove it).
const IS_CONTAINER = !!process.env.E2E_BASE_URL

// @live: drives the REAL model + REAL renderer end-to-end. Paid + non-deterministic
// + subject to the per-user daily token cap, so: structural assertions only (never
// model wording), a generous timeout, and one retry — and the deterministic
// packaging gate (qa-attachments.sh @ :3001) is what actually gates packaging.
test.describe('@live deck attachment round-trip', () => {
  test.describe.configure({ retries: 1 })

  // The deck path converts PPTX → PDF through Gotenberg. Where Gotenberg is not deployed the
  // control-plane refuses a pptx upload with 501 "PowerPoint attachments aren't enabled." —
  // that is the intended default, not a failure, so running this here would go red for a
  // reason that has nothing to do with the code under test. Opt in once the renderer exists.
  test.skip(
    !process.env.E2E_DECK_ENABLED,
    'deck conversion (Gotenberg) is not deployed here — pptx uploads return 501 by design. ' +
      'Set E2E_DECK_ENABLED=1 against a stack that has it.',
  )

  test('attach .pptx → assistant reads it → download the ORIGINAL .pptx', async ({ page }) => {
    test.setTimeout(150_000)

    const consoleErrors: string[] = []
    page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })
    page.on('pageerror', (e) => consoleErrors.push(String(e)))
    // Record any CSP violation (container pass asserts there are none).
    await page.addInitScript(() => {
      ;(window as unknown as { __csp: string[] }).__csp = []
      window.addEventListener('securitypolicyviolation', (e) => {
        ;(window as unknown as { __csp: string[] }).__csp.push(`${e.violatedDirective} ${e.blockedURI}`)
      })
    })

    // Project-first: the bare `/chat` route no longer exists — a chat lives inside a project.
    const project = await (await page.request.post('/api/projects', {
      data: { name: `E2E Deck Live ${Date.now()}` },
    })).json()
    await page.goto(`/chat/${crypto.randomUUID()}?projectId=${project.id}&kind=builder`)

    // 1. Attach is CLIENT-SIDE only (base64 read → chip, no network). Assert the chip.
    await page.getByTestId('chat-file-input').setInputFiles(SAMPLE_PPTX)
    await expect(page.getByText('sample.pptx')).toBeVisible()

    // 2. The heavy Gotenberg conversion happens on SEND (POST /api/attachments).
    //    Arm the wait BEFORE clicking; assert res.ok() (route returns 201, never a
    //    literal 200).
    const uploadPromise = page.waitForResponse(
      (r) => r.url().includes('/api/attachments') && r.request().method() === 'POST',
      { timeout: 90_000 },
    )
    await page.getByTestId('chat-send').click()
    const uploadRes = await uploadPromise
    expect(uploadRes.ok(), `upload status ${uploadRes.status()}`).toBeTruthy()
    expect(uploadRes.status()).toBe(201)

    // 3. Assert the assistant turn via UI STATE — never the /api/claude SSE object
    //    (it resolves on headers, mid-stream). Bubble visible + model text rendered
    //    + streaming finished (typing indicator gone = composer re-enabled).
    await expect(page.getByTestId('assistant-message')).toBeVisible({ timeout: 90_000 })
    await expect(page.getByTestId('assistant-message').locator('.prose')).not.toBeEmpty({ timeout: 90_000 })
    await expect(page.getByTestId('assistant-typing')).toHaveCount(0, { timeout: 90_000 })

    // 4. Download the chip → it must be the ORIGINAL .pptx (PK zip), never the
    //    internal derived PDF (%PDF). The conversion stays invisible.
    const downloadPromise = page.waitForEvent('download')
    await page.getByTestId('deck-download-chip').click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toBe('sample.pptx')
    const saved = await download.path()
    const head = fs.readFileSync(saved).subarray(0, 4)
    expect(head.subarray(0, 2).toString('latin1')).toBe('PK')
    expect(head.toString('latin1')).not.toBe('%PDF')

    // R3: no UI surface leaks the derived PDF / pdfFileId.
    const bodyText = await page.locator('body').innerText()
    expect(bodyText.toLowerCase()).not.toContain('.pdf')
    expect(bodyText).not.toContain('pdfFileId')

    // Container-only: prove the packaging-sensitive health dev can't (strict CSP,
    // single-origin assets/SSE). A CSP/asset/origin defect breaks ONLY the container.
    if (IS_CONTAINER) {
      const csp = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp || [])
      expect(csp, `CSP violations:\n${csp.join('\n')}`).toHaveLength(0)
      expect(consoleErrors, `console errors:\n${consoleErrors.join('\n')}`).toHaveLength(0)
    }
  })
})
