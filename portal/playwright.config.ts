import { defineConfig, devices } from '@playwright/test'
import { config as loadEnv } from 'dotenv'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const dirname = path.dirname(fileURLToPath(import.meta.url))

// e2e config lives in .env.e2e (E2E_QA_EMAIL, and E2E_STORAGE_STATE — the path to a
// session minted by .mythos/fastapi-e2e/scripts/auth/mint_session.py, which is what
// makes a spec run against a REAL backend session rather than a mocked /auth/me).
// portal/.env is loaded as a fallback. dotenv never overrides an already-set
// process.env var, so anything the caller exports (E2E_BASE_URL) wins.
loadEnv({ path: path.join(dirname, '.env.e2e') })
loadEnv({ path: path.join(dirname, '.env') })

// E2E_BASE_URL UNSET  → dev pass: Playwright manages `npm run dev:full` at :5173.
// E2E_BASE_URL SET    → external server (the built container) at :3001, no webServer.
const E2E_BASE_URL = process.env.E2E_BASE_URL
const baseURL = E2E_BASE_URL || 'http://localhost:5173'

// Opt-in artifact capture (demos / debugging): E2E_CAPTURE=1 forces screenshots,
// video, and trace ON even on green runs. Default stays retain-on-failure so a
// normal pass leaves no JWT-bearing artifacts behind.
const CAPTURE = !!process.env.E2E_CAPTURE

// storageState is ORIGIN-SCOPED. The default two-invocation flow (one `playwright
// test` per target) re-runs auth.setup against the current baseURL each time, so
// a single user.json is always seeded for the origin under test — no per-origin
// cache reuse hazard.
const AUTH_FILE = path.join(dirname, 'playwright/.auth/user.json')

export default defineConfig({
  testDir: './e2e',
  // Serial: the suite shares one rate-limited QA account + a live model, so
  // parallel workers would trip the login limiter / daily token cap.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  // Health-gate the target before any test (poll {baseURL}/api/health → 200).
  globalSetup: './e2e/global-setup.ts',
  timeout: 90_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL,
    // NOTE: trace + video capture localStorage JWTs and response bodies. The
    // playwright/.auth/, test-results/, playwright-report/ dirs are gitignored
    // and MUST be scrubbed from any future CI artifact upload.
    trace: CAPTURE ? 'on' : 'retain-on-failure',
    screenshot: CAPTURE ? 'on' : 'only-on-failure',
    video: CAPTURE ? 'on' : 'retain-on-failure',
    // Deck conversion (LibreOffice) is the slow action — give clicks room.
    actionTimeout: 45_000,
  },
  projects: [
    // Seeds shared auth for the current origin by minting the JWT directly (no
    // /api/auth/login request → never trips the 10/15-min login limiter).
    { name: 'setup', testMatch: /auth\.setup\.ts/ },
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], storageState: AUTH_FILE },
      dependencies: ['setup'],
    },
  ],
  // Only manage the dev stack when no external target was given. reuseExisting
  // means a dev stack already up (the usual local case) is left untouched.
  webServer: E2E_BASE_URL
    ? undefined
    : {
        command: 'npm run dev:full',
        url: 'http://localhost:5173',
        reuseExistingServer: true,
        timeout: 120_000,
      },
})
