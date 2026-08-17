// Drives a real, running portal + backend through a named "profile" of screens and
// saves a PNG per step to pr-screenshots/. Built for the CI workflow at
// .github/workflows/pr-e2e-screenshots.yml, which mints the session this reads via
// backend/tests/tooling/mint_dev_session.py — see that workflow for how the stack
// this script drives gets started.
//
// New PR needing screenshots? Add a `capture<Name>()` function below and a branch
// in the dispatch at the bottom, then add the matching `profile` choice to the
// workflow's workflow_dispatch input.
import { chromium } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const PROFILE = process.env.SCREENSHOT_PROFILE;
const SESSION_FILE = process.env.SESSION_FILE || 'playwright/.auth/ci-session.json';
const PORTAL_BASE = process.env.PORTAL_BASE_URL || 'http://localhost:5173';
const BACKEND_BASE = process.env.BACKEND_BASE_URL || 'http://localhost:8000';
const OUT_DIR = 'pr-screenshots';

if (!PROFILE) {
  console.error('SCREENSHOT_PROFILE is required (chat-ui | identity-assertion)');
  process.exit(1);
}

fs.mkdirSync(OUT_DIR, { recursive: true });

const browser = await chromium.launch();
const context = await browser.newContext({
  storageState: SESSION_FILE,
  viewport: { width: 1440, height: 900 },
});
const page = await context.newPage();

let stepCount = 0;
async function shot(name) {
  stepCount += 1;
  const file = `${String(stepCount).padStart(2, '0')}-${name}.png`;
  await page.screenshot({ path: path.join(OUT_DIR, file), fullPage: true });
  console.log('captured', file);
}

/** PR #128 — assistant-ui composer migration (drag-drop, Streamdown markdown rendering). */
async function captureChatUI() {
  await page.goto(`${PORTAL_BASE}/projects`);
  await shot('projects');

  await page.getByRole('button', { name: /new project/i }).first().click();
  await page.getByPlaceholder(/VIP Movement Tracker/i).fill(`PR screenshot ${Date.now()}`);
  await page.getByRole('button', { name: /create project/i }).click();
  await page.waitForURL(/\/projects\/[0-9a-f-]{36}/, { timeout: 30_000 });
  await shot('new-thread-empty');

  // No live Foundry deployment in CI (Tier C infra) — stub the SSE relay the same way
  // portal/e2e/thread-lifecycle.spec.ts does, so the Streamdown swap this PR makes is
  // still provable: real backend, real auth, real persistence, fake model text only.
  await page.route('**/api/claude', async (route) => {
    const frame = {
      type: 'text',
      text:
        "Here's a **formatted** reply to confirm Streamdown rendering:\n\n" +
        '- bullet one\n- bullet two\n\n' +
        '```js\nconst answer = 42;\n```\n\n' +
        'And a [link](https://example.com) to check target/rel handling.',
    };
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: `data: ${JSON.stringify(frame)}\n\ndata: {"type":"done"}\n\n`,
    });
  });

  const composer = page.getByPlaceholder(/Describe what you're thinking/i);
  await composer.fill('Show me a formatted example');
  await composer.press('Enter');
  await page.waitForTimeout(1500);
  await shot('assistant-markdown-reply');

  // usePendingAttachments.ts binds its drop listener on `window`, not a dedicated
  // dropzone element (confirmed against ChatPage-dragdrop.test.jsx), so the synthetic
  // DragEvent below targets window directly rather than hunting for a testid.
  const fixtureDir = path.resolve('e2e/fixtures');
  const fixture = fs.existsSync(fixtureDir)
    ? fs.readdirSync(fixtureDir).find((f) => /\.(pdf|png|jpg|docx|xlsx|pptx)$/i.test(f))
    : null;
  if (fixture) {
    const buffer = fs.readFileSync(path.join(fixtureDir, fixture));
    await page.evaluate(
      async ({ b64, name }) => {
        const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
        const file = new File([bytes], name);
        const dt = new DataTransfer();
        dt.items.add(file);
        window.dispatchEvent(new DragEvent('dragover', { dataTransfer: dt, bubbles: true }));
        window.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true }));
      },
      { b64: buffer.toString('base64'), name: fixture }
    );
    await page.waitForTimeout(500);
    await shot('drag-drop-attachment');
  } else {
    console.log('no fixture file found under e2e/fixtures/, skipping drag-drop screenshot');
  }
}

/** PR #130 — platform-minted app identity assertions. */
async function captureIdentityAssertion() {
  // The public verification key every generated app fetches (R11) — no live sandbox
  // needed to prove this endpoint shape.
  await page.goto(`${BACKEND_BASE}/v1/auth/jwks`);
  await shot('jwks');

  // Confirms the deprecated login_required admin toggle (R22) is gone.
  await page.goto(`${PORTAL_BASE}/admin`);
  await page.waitForTimeout(1000);
  await shot('admin-console');

  // NOTE: this profile intentionally stops here. The preview-plane postMessage
  // handshake and the deployed-plane exchange-code round trip both require a real
  // Azure Container Apps sandbox (Tier C) — shared infra this CI run does not have
  // and should not provision. See docs/local-test-setup.md Tier C.
}

if (PROFILE === 'chat-ui') {
  await captureChatUI();
} else if (PROFILE === 'identity-assertion') {
  await captureIdentityAssertion();
} else {
  console.error(`Unknown SCREENSHOT_PROFILE: ${PROFILE}`);
  process.exit(1);
}

await browser.close();
console.log(`Done. ${stepCount} screenshot(s) in ${OUT_DIR}/`);
