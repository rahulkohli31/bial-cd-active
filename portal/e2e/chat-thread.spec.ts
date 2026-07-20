import { test, expect } from '@playwright/test'

/**
 * The assistant-ui Thread migration, against a REAL control-plane + REAL model turn.
 *
 * Verifies the two riskiest pieces of the ChatPage rewrite that unit tests (mocked
 * fetchClaudeStream) structurally cannot prove:
 *   - the ChatModelAdapter actually round-trips a real Foundry-backed Claude turn and
 *     Thread renders the streamed reply (via Streamdown) as it arrives,
 *   - the `location.state.initialMessage` hand-off from ProjectBuilder's "Start
 *     Planning" button fires through the composer runtime (InitialMessageSender)
 *     exactly once, with no user interaction needed after the navigation.
 */
test.describe('assistant-ui Thread — planning chat', () => {
  test.describe.configure({ timeout: 180_000 })

  test('Start Planning hands off an initial message that Thread sends and renders a real reply for', async ({ page }) => {
    await page.goto('/projects')
    await page.getByRole('button', { name: /new project/i }).first().click()
    await page.getByPlaceholder(/VIP Movement Tracker/i).fill(`E2E Thread ${Date.now()}`)
    await page.getByRole('button', { name: /create project/i }).click()
    await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}/)

    // Switch the project composer into planning mode and hand off an initial message —
    // exercises ProjectBuilder's `{ state: { initialMessage } }` navigation.
    await page.getByRole('button', { name: /plan with ai/i }).click()
    const builderComposer = page.getByPlaceholder(/Describe what you're thinking… I'll help you plan it out/i)
    await builderComposer.fill('In one short sentence, what should I consider first when planning a gate equipment maintenance app?')
    await page.getByRole('button', { name: /start planning/i }).click()

    // A brand-new chat carries its project in a transient query until the first append.
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}\?projectId=/)

    // InitialMessageSender fires the hand-off through the composer runtime with no
    // further user action — the user bubble should appear on its own.
    await expect(
      page.getByText('In one short sentence, what should I consider first'),
    ).toBeVisible({ timeout: 20_000 })

    // The user turn persisting flattens the URL (dropTransientQuery).
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/, { timeout: 20_000 })

    // A real assistant reply streams in and renders via Thread/Streamdown. Look for
    // prose text in the assistant's message region rather than any specific wording.
    const assistantMessage = page.locator('[data-slot="aui_assistant-message-content"], .aui-md-p').first()
    await expect(assistantMessage).toBeVisible({ timeout: 90_000 })
    const replyText = await assistantMessage.textContent()
    expect(replyText?.trim().length ?? 0).toBeGreaterThan(0)

    // Thread swaps the composer's Send button for a Cancel button only while a run is
    // in flight (AuiIf on thread.isRunning) — Send's mere presence proves the run ended.
    // It's still DISABLED here, correctly: the composer text is empty post-send, and a
    // disabled send-on-empty button is normal UX, not a stuck run.
    await expect(page.getByRole('button', { name: /send message/i })).toBeVisible({ timeout: 15_000 })
  })
})
