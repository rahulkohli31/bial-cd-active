import { test, expect, type Page } from '@playwright/test'

/**
 * Open a composer and return its hidden file input.
 *
 * Two things moved under this spec's feet and it never noticed, because the suite could not
 * run at all (playwright.config imported an undeclared `dotenv`):
 *
 *  1. `goto('/chat')` — project-first deleted the bare route; a chat lives inside a project.
 *  2. `getByTestId('chat-file-input')` / `assistant-message` — both lived on BialChatPage.jsx,
 *     which the same change deleted. Nothing re-added them, so we address the composer's
 *     file input directly rather than re-introduce a test-only attribute into product code.
 *
 * Nothing is ever sent here, so no conversation row is created and no model turn is spent.
 */
async function openComposer(page: Page) {
  const res = await page.request.post('/api/projects', {
    data: { name: `E2E Deck ${Date.now()}` },
  })
  const projectId = (await res.json()).id as string
  await page.goto(`/chat/${crypto.randomUUID()}?projectId=${projectId}&kind=builder`)

  const input = page.locator('input[type=file]').first()
  await expect(input).toBeAttached()
  return input
}

/** A rejected attachment must never reach the model. Watch the wire, not a removed testid. */
function watchModelCalls(page: Page): { count: () => number } {
  let n = 0
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().includes('/api/claude')) n += 1
  })
  return { count: () => n }
}

// Client-side rejections — no network, no model, identical in dev and container.
// Fixtures are in-memory buffers (no committed binaries): the rejection is decided
// by extension/size before any upload, so the bytes need not be valid OOXML.
// BOTH tests below were written when DECK_ATTACHMENTS_ENABLED was on, and #157 B2 turned it
// off. Neither noticed, because this suite is not in CI (`ci.yml` runs typecheck + lint +
// vitest), so it stays green until someone runs `npm run e2e` by hand. They are retargeted at
// the shipped state rather than deleted: the invariant they exist for — a rejected attachment
// never reaches the model — is flag-independent and worth keeping pinned.
test.describe('deck attachment rejections (client-side)', () => {
  test('legacy .ppt gets the generic unsupported-type message, not advice it cannot follow', async ({ page }) => {
    const model = watchModelCalls(page)
    const input = await openComposer(page)

    await input.setInputFiles({
      name: 'legacy.ppt',
      mimeType: 'application/vnd.ms-powerpoint',
      buffer: Buffer.from('legacy-binary-ppt'),
    })

    // THE DEAD END this test used to pin the wrong side of: with decks off, "save as .pptx"
    // sent the user to a file the allowlist refuses too. The generic message is the honest
    // one — it names what IS accepted, so there is a next step.
    await expect(page.getByText(/isn't supported/)).toBeVisible()
    await expect(page.getByText(/save as \.pptx/i)).toHaveCount(0)
    // The `not.toContainText(/pdf/i)` assertion that lived here has moved to
    // attachmentInput-deck.test.js: it pinned "never reveal the internal deck→PDF
    // conversion," and the generic copy legitimately lists PDF as an ACCEPTED type. With
    // decks off there is no conversion to reveal.

    // No chip was added and no assistant turn was generated.
    await expect(page.getByText('legacy.ppt')).toHaveCount(0)
    expect(model.count(), 'a rejected attachment must never reach the model').toBe(0)
  })

  test('an oversize attachment (> 4 MB) is rejected and generates no assistant turn', async ({ page }) => {
    const model = watchModelCalls(page)
    const input = await openComposer(page)

    // A PDF, not the .pptx this used to use: with decks off the allowlist check fires
    // BEFORE the size check, so an oversize .pptx never reaches the 4 MB cap and this test
    // silently stopped exercising it. A PDF is accepted at any flag setting, so the cap is
    // genuinely the thing being tested again.
    await input.setInputFiles({
      name: 'oversize.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.alloc(4 * 1024 * 1024 + 128 * 1024), // ~4.1 MB > 4 MB cap
    })

    await expect(page.getByText('exceeds the 4 MB limit')).toBeVisible()
    await expect(page.getByText('oversize.pdf')).toHaveCount(0)
    expect(model.count(), 'a rejected attachment must never reach the model').toBe(0)
  })
})
