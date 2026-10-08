import type { FullConfig } from '@playwright/test'

// Gate on the backend being up before any spec runs. The portal does not route the control
// plane's health check, so ask a route it does: GET /api/v1/auth/me with no session is the
// backend's own 401. A bare path from the portal origin would false-green on the SPA history
// fallback, so it MUST go through /api. Budget ~120s for container boot: a never-ready target
// fails fast here with a clear message instead of every spec failing on an auth redirect that
// masquerades as a packaging bug.
async function globalSetup(_config: FullConfig) {
  const baseURL = process.env.E2E_BASE_URL || 'http://localhost:5173'
  const target = `${baseURL.replace(/\/$/, '')}/api/v1/auth/me`
  const deadlineMs = Date.now() + 120_000
  let lastErr = 'no attempt'

  while (Date.now() < deadlineMs) {
    try {
      const res = await fetch(target)
      if (res.status === 401) return
      lastErr = `status ${res.status}`
    } catch (err) {
      lastErr = err instanceof Error ? err.message : String(err)
    }
    await new Promise((r) => setTimeout(r, 1500))
  }

  throw new Error(`global-setup: ${target} never returned 401 within 120s (last: ${lastErr}). ` +
    'Is the dev stack (npm run dev, plus the backend) or the container up?')
}

export default globalSetup
