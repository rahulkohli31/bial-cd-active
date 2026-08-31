/**
 * The beacon's name vocabulary, reconciled ACROSS THE LANGUAGE BOUNDARY (U3/U4).
 *
 * WHY THIS EXISTS. `POST /v1/observations` refuses any name not on its server-side allowlist —
 * that refusal is the security property, and it is correct. But the browser's half of this path
 * is deliberately fire-and-forget: `observe.ts` swallows every failure so a measurement can never
 * fail the thing it measures. Put those two correct decisions together and a drift between the
 * two lists is SILENT AND PERMANENT — the browser posts, the server 400s, nothing is logged where
 * anyone looks, and the counter simply reads zero forever while every test stays green. The only
 * moment that drift is catchable is here.
 *
 * Same shape as `deployApi.classification-parity.test.ts` and `nginx-apps-routing.test.ts`: read
 * the real file on the other side, parse the invariant out of it, and anchor on `process.cwd()`
 * because `import.meta.url` is a jsdom http URL under this config.
 *
 * It parses rather than greps, so reformatting the Python, reordering the dict, or rewrapping a
 * comment does not fail the suite — only a change to what the allowlist MEANS does.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { OBSERVATION_NAMES } from '../observe'

const BACKEND = path.resolve(process.cwd(), '..', 'backend', 'src')
const ROUTER_PY = readFileSync(path.join(BACKEND, 'api', 'v1', 'observations', 'router.py'), 'utf8')
const COUNTER_PY = readFileSync(path.join(BACKEND, 'db', 'models', 'harness_counter.py'), 'utf8')

/** `PROJECT_OPENED = "project_opened"` → `{ PROJECT_OPENED: 'project_opened' }`. */
function harnessCounterValues(): Record<string, string> {
  const values: Record<string, string> = {}
  for (const [, member, value] of COUNTER_PY.matchAll(/^\s{4}([A-Z][A-Z0-9_]*)\s*=\s*"([^"]+)"/gm)) {
    values[member] = value
  }
  return values
}

/** The wire names the route will accept, read out of `_CEILING_BY_NAME`'s own body. */
function serverAllowlist(): string[] {
  const block = ROUTER_PY.match(/_CEILING_BY_NAME[^=]*=\s*\{([\s\S]*?)\n\}/)
  expect(block, 'could not find _CEILING_BY_NAME in the observations router').toBeTruthy()
  const members = [...block![1].matchAll(/HarnessCounter\.([A-Z][A-Z0-9_]*)\.value/g)].map((m) => m[1])
  const byMember = harnessCounterValues()
  return members.map((member) => {
    expect(byMember[member], `HarnessCounter.${member} has no string value`).toBeTruthy()
    return byMember[member]
  })
}

describe('the browser sends only names the beacon route accepts', () => {
  it('parses a non-empty allowlist out of the server, so the comparison is not vacuous', () => {
    // The liveness half. Both regexes returning nothing would make every assertion below pass
    // against two empty sets — which is exactly the shape of green this test exists to refuse.
    const server = serverAllowlist()
    expect(server.length).toBeGreaterThan(0)
    expect(Object.keys(harnessCounterValues()).length).toBeGreaterThan(5)
    expect(OBSERVATION_NAMES.length).toBeGreaterThan(0)
  })

  it('★ every name the browser can send is on the server allowlist', () => {
    // The direction that fails silently in production: a name only the browser knows 400s on
    // every send, and the counter reads zero forever with nothing on fire.
    const server = serverAllowlist()
    for (const name of OBSERVATION_NAMES) {
      expect(server, `the browser sends "${name}" but the route refuses it`).toContain(name)
    }
  })

  it('★ every name the server allows has a browser that sends it', () => {
    // The other direction: an allowlisted name nobody sends is the decorative counter this
    // plan's own rule forbids — a name that ships without its writer.
    for (const name of serverAllowlist()) {
      expect(
        OBSERVATION_NAMES as readonly string[],
        `the route allows "${name}" but no browser code sends it`,
      ).toContain(name)
    }
  })
})
