// Walking-skeleton seam driver (MOCKED leg). Ties the mock C1 supervisor to the mock C7
// brain over the frozen contracts and asserts the seams connect end-to-end:
//   mock brain --(C1: /exec, /dev/start, /dev/status)--> mock supervisor
//   mock brain --(C7 envelope stream)--> this driver (the SESSION-API relay stand-in)
//
// This proves the contracts are buildable-to-the-doc (the anti-"both sides invent the shape"
// check). It is the MOCK half; the REAL half — a genuine cross-origin frame + a real next-dev
// render — is proven by frame-proof/prove-framing.mjs. Run: `node run-seam.mjs`.

import assert from 'node:assert/strict'
import { start } from './mock-supervisor.mjs'
import { runBuild, C7_TYPES } from './mock-brain.mjs'

const PORT = 9051
const TOKEN = 'skeleton-mock-token'
const BASE = `http://127.0.0.1:${PORT}`
const AUTH = { authorization: `Bearer ${TOKEN}` }

// A minimal C1 client (the SandboxClient subset BRAIN calls) over the mock supervisor.
const client = {
  async health() {
    return (await fetch(`${BASE}/health`)).json()
  },
  async runExec(cmd) {
    const r = await fetch(`${BASE}/exec`, { method: 'POST', headers: { ...AUTH, 'content-type': 'application/json' }, body: JSON.stringify({ cmd }) })
    return r.json()
  },
  async devStart() {
    const r = await fetch(`${BASE}/dev/start`, { method: 'POST', headers: { ...AUTH, 'content-type': 'application/json' }, body: JSON.stringify({ cmd: ['npm', 'run', 'dev'] }) })
    return r.json()
  },
  async devStatus() {
    return (await fetch(`${BASE}/dev/status`, { headers: AUTH })).json()
  },
}

async function main() {
  process.env.SUPERVISOR_TOKEN = TOKEN
  const server = await start(PORT)
  try {
    // C1: /health is unauth and returns {ok:true}.
    assert.deepEqual(await client.health(), { ok: true }, 'C1 /health must return {ok:true}')

    // C1: a missing bearer is 401 (the exact `Bearer {TOKEN}` match).
    const unauth = await fetch(`${BASE}/dev/status`)
    assert.equal(unauth.status, 401, 'C1 non-health routes require the bearer token')

    // Drive a mock build turn; collect the C7 envelope stream.
    const events = []
    const PREVIEW_URL = 'https://app-xyz.example.azurecontainerapps.io/'
    const result = await runBuild(client, (env) => events.push(env), PREVIEW_URL)

    // C7: every emitted type is a frozen member; seq is monotonic +1 gap-free; the stream
    // ends preview_ready -> ended (the happy path).
    for (const e of events) assert.ok(C7_TYPES.includes(e.type), `unknown C7 type: ${e.type}`)
    events.forEach((e, i) => assert.equal(e.seq, i + 1, 'C7 seq must be monotonic +1 gap-free'))
    const last = events[events.length - 1]
    assert.equal(last.type, 'ended', 'the stream must end with an `ended` envelope')
    assert.equal(last.status, 'ended', 'the ended envelope status must be `ended` on the happy path')
    const preview = events.find((e) => e.type === 'preview_ready')
    assert.ok(preview && preview.preview_url === PREVIEW_URL, 'preview_ready must carry the preview_url')
    assert.equal(result.status, 'ended', 'BuildResult must terminate `ended`')
    assert.equal(result.preview_url, PREVIEW_URL, 'BuildResult carries the preview_url')

    console.log(`PASS  seam: ${events.length} C7 envelopes over the C1 seam; preview_ready -> ended; BuildResult ok`)
    console.log(`      types seen: ${[...new Set(events.map((e) => e.type))].join(', ')}`)
  } finally {
    server.close()
  }
}

main().catch((err) => {
  console.error('FAIL  seam:', err.message)
  process.exit(1)
})
