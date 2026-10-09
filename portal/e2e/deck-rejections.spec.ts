import { test, expect, type Page } from '@playwright/test'

/**
 * Open a composer on a fresh project's chat, ready to stage a file.
 *
 * Nothing is ever sent here, so no conversation row is created and no model turn is spent.
 */
async function openComposer(page: Page): Promise<void> {
  const res = await page.request.post('/api/projects', {
    // #191: description is required, 15-120 words.
    data: {
      name: `E2E Deck ${Date.now()}`,
      description:
        'Ground staff log VIP movement requests for each terminal. A duty supervisor ' +
        "approves or rejects them, and the day's approved movements appear on a shared dashboard.",
    },
  })
  const projectId = (await res.json()).id as string
  await page.goto(`/chat/${crypto.randomUUID()}?projectId=${projectId}&kind=builder`)

  await expect(page.getByTestId('composer-attach')).toBeVisible()
}

/**
 * Stage a file the way a citizen does — through the paperclip's own file chooser.
 *
 * THERE IS NO PERSISTENT `input[type=file]` TO ADDRESS. The composer is the library's, and its
 * add-attachment control opens a chooser on press rather than sitting over a hidden input in the
 * DOM — so `locator('input[type=file]')` matched nothing and this helper waited for an element
 * that is only ever created for the duration of a click. Arm the wait BEFORE the press: the
 * chooser event fires during the click, so a wait armed afterwards has already missed it.
 */
async function attachFile(
  page: Page,
  file: { name: string; mimeType: string; buffer: Buffer },
): Promise<void> {
  const chooser = page.waitForEvent('filechooser')
  await page.getByTestId('composer-attach').click()
  await (await chooser).setFiles(file)
}

/**
 * The one thing this spec exists for: a rejected attachment must never reach the model. Watch the
 * wire, not a removed testid.
 *
 * THE TURN-SEND ENDPOINT, not the retired `/api/claude` relay. A message reaches the model through
 * `POST /api/conversations/{id}/turns` (`src/utils/turnStreamApi.ts`), and that route is the only
 * thing that starts a turn — so counting `/api/claude` counted a request the portal has not made
 * since the relay was retired, and the headline assertion below was vacuously true.
 *
 * MATCHED ON THE PATH'S SHAPE, not on a substring: the sibling `…/turns/{turnId}/stop` is also a
 * POST whose URL contains `/turns`, and counting it would make a Stop press read as a send.
 */
const TURN_SEND = /^\/api\/conversations\/[^/]+\/turns$/

function watchModelCalls(page: Page): { count: () => number } {
  let n = 0
  page.on('request', (req) => {
    if (req.method() === 'POST' && TURN_SEND.test(new URL(req.url()).pathname)) n += 1
  })
  return { count: () => n }
}

// Client-side rejections — no network, no model, identical in dev and container. Fixtures are
// in-memory buffers (no committed binaries): the rejection is decided by extension/size before
// any upload, so the bytes need not be valid OOXML.
//
// Both tests below were written when the deck flag was on, then it was turned off; neither
// noticed, since this suite is not in CI, so it stays green until `npm run e2e` runs by hand.
// Retargeted at the shipped state rather than deleted — the invariant they exist for (a rejected
// attachment never reaches the model) is flag-independent.
test.describe('deck attachment rejections (client-side)', () => {
  test('legacy .ppt is refused by name, with the format to re-save it as', async ({ page }) => {
    const model = watchModelCalls(page)
    await openComposer(page)

    await attachFile(page, {
      name: 'legacy.ppt',
      mimeType: 'application/vnd.ms-powerpoint',
      buffer: Buffer.from('legacy-binary-ppt'),
    })

    await expect(page.getByText(/"legacy\.ppt" is an older PowerPoint format/)).toBeVisible()

    // No chip was added and no assistant turn was generated.
    await expect(page.getByTestId('composer-chips')).toHaveCount(0)
    expect(model.count(), 'a rejected attachment must never reach the model').toBe(0)
  })

  test('an oversize PDF (> 20 MB) is rejected and generates no assistant turn', async ({ page }) => {
    const model = watchModelCalls(page)
    await openComposer(page)

    // A PDF, not the .pptx this used to use: with decks off the allowlist check fires
    // BEFORE the size check, so an oversize .pptx never reaches the size cap and this test
    // silently stopped exercising it. A PDF is accepted at any flag setting, so the cap is
    // genuinely the thing being tested again.
    await attachFile(page, {
      name: 'oversize.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.alloc(20 * 1024 * 1024 + 128 * 1024), // just over the 20 MB PDF limit
    })

    await expect(page.getByText('exceeds the 20 MB limit')).toBeVisible()
    await expect(page.getByText('oversize.pdf')).toHaveCount(0)
    expect(model.count(), 'a rejected attachment must never reach the model').toBe(0)
  })
})
