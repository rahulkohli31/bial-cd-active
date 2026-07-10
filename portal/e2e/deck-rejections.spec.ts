import { test, expect, type Page } from '@playwright/test'

const PPTX_MT = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'

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
test.describe('deck attachment rejections (client-side)', () => {
  test('legacy .ppt is rejected with the "save as .pptx" message (no PDF mention)', async ({ page }) => {
    const model = watchModelCalls(page)
    const input = await openComposer(page)

    await input.setInputFiles({
      name: 'legacy.ppt',
      mimeType: 'application/vnd.ms-powerpoint',
      buffer: Buffer.from('legacy-binary-ppt'),
    })

    // The honest message must NOT reveal the internal PDF conversion.
    const toast = page.getByText('save as .pptx and re-upload')
    await expect(toast).toBeVisible()
    await expect(toast).not.toContainText(/pdf/i)

    // No chip was added and no assistant turn was generated.
    await expect(page.getByText('legacy.ppt')).toHaveCount(0)
    expect(model.count(), 'a rejected attachment must never reach the model').toBe(0)
  })

  test('oversize .pptx (> 4 MB) is rejected and generates no assistant turn', async ({ page }) => {
    const model = watchModelCalls(page)
    const input = await openComposer(page)

    await input.setInputFiles({
      name: 'oversize.pptx',
      mimeType: PPTX_MT,
      buffer: Buffer.alloc(4 * 1024 * 1024 + 128 * 1024), // ~4.1 MB > 4 MB cap
    })

    await expect(page.getByText('exceeds the 4 MB limit')).toBeVisible()
    await expect(page.getByText('oversize.pptx')).toHaveCount(0)
    expect(model.count(), 'a rejected attachment must never reach the model').toBe(0)
  })
})
