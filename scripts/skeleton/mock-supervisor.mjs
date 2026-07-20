// Mock C1 supervisor — a faithful stand-in for the in-sandbox supervisor HTTP API
// (docs/engineering/contracts/C1-supervisor-http-api.md), built from the CONTRACT ALONE.
// A Wave-1 orchestrator (BRAIN) can drive this exactly as it would drive the real
// supervisor: same paths, same request/response shapes, same bearer auth, same status
// codes. It runs no real processes — /exec and /dev/* return canned shapes.
//
// This is the "mocked helper" leg of the walking skeleton: it proves the C1 seam is
// buildable-to-the-doc (the anti-"both sides invent the shape" check). The REAL supervisor
// is sandbox/supervisor/app.py (forked byte-identical from the proven spike).

import http from 'node:http'

const TOKEN = process.env.SUPERVISOR_TOKEN || 'skeleton-mock-token'
const PORT = Number(process.env.MOCK_SUP_PORT || 9000)

// A tiny in-memory dev-server state machine so /dev/status flips ready after /dev/start,
// mirroring C1's "ready = 'Ready in' marker seen AND process alive".
const dev = { running: false, ready: false, pid: 0, logs: [] }

function readJson(req) {
  return new Promise((resolve) => {
    let body = ''
    req.on('data', (c) => (body += c))
    req.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : {})
      } catch {
        resolve(null) // signal a bad body → 422
      }
    })
  })
}

function send(res, status, obj) {
  const payload = JSON.stringify(obj)
  res.writeHead(status, { 'content-type': 'application/json' })
  res.end(payload)
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1')
  const path = url.pathname

  // GET /health is UNAUTH (C1); everything else needs `Authorization: Bearer {TOKEN}`.
  if (path === '/health' && req.method === 'GET') return send(res, 200, { ok: true })
  if (req.headers.authorization !== `Bearer ${TOKEN}`) {
    return send(res, 401, { detail: 'bad or missing bearer token' })
  }

  if (path === '/exec' && req.method === 'POST') {
    const body = await readJson(req)
    if (!body || !Array.isArray(body.cmd)) return send(res, 422, { detail: 'cmd must be a list' })
    // Non-zero exit is a NORMAL 200 in C1 (self-heal reads exit/stderr off a 200). The mock
    // returns exit 0 for a known-good command and a non-zero exit for `false`, to exercise both.
    const exit = body.cmd.join(' ') === 'false' ? 1 : 0
    return send(res, 200, { stdout: `mock exec: ${body.cmd.join(' ')}\n`, stderr: '', exit })
  }

  if (path === '/files' && req.method === 'POST') {
    const body = await readJson(req)
    if (!body || !body.action || !body.path) return send(res, 422, { detail: 'action and path required' })
    switch (body.action) {
      case 'view':
        return send(res, 200, { ok: true, content: '1\tmock file line' })
      case 'str_replace':
        if (body.old_str == null || body.new_str == null) return send(res, 400, { detail: 'str_replace needs old_str and new_str' })
        return send(res, 200, { ok: true, replacements: 1 })
      case 'create':
        if (body.file_text == null) return send(res, 400, { detail: 'create needs file_text' })
        return send(res, 200, { ok: true, created: body.path })
      case 'insert':
        if (body.insert_line == null || body.insert_text == null) return send(res, 400, { detail: 'insert needs insert_line and insert_text' })
        return send(res, 200, { ok: true })
      default:
        return send(res, 400, { detail: `unknown files action: ${body.action}` })
    }
  }

  if (path === '/dev/start' && req.method === 'POST') {
    if (dev.running) return send(res, 409, { detail: 'dev server already running' })
    dev.running = true
    dev.pid = 4242
    dev.logs = ['> next dev', 'mock: compiling…']
    // Flip ready shortly after start, mirroring the "Ready in" marker appearing.
    setTimeout(() => {
      dev.ready = true
      dev.logs.push('✓ Ready in 1049ms')
    }, 50)
    return send(res, 200, { pid: dev.pid })
  }

  if (path === '/dev/status' && req.method === 'GET') {
    return send(res, 200, { running: dev.running, ready: dev.ready && dev.running, port: 3000 })
  }

  if (path === '/dev/logs' && req.method === 'GET') {
    const since = Number(url.searchParams.get('since') || 0)
    return send(res, 200, { lines: dev.logs.slice(since), next: dev.logs.length })
  }

  return send(res, 404, { detail: 'not found' })
})

export function start(port = PORT) {
  return new Promise((resolve) => server.listen(port, '127.0.0.1', () => resolve(server)))
}

// Runnable directly (`node mock-supervisor.mjs`) or importable by the seam driver.
if (import.meta.url === `file://${process.argv[1]}`) {
  start().then(() => console.log(`mock C1 supervisor listening on :${PORT} (token=${TOKEN})`))
}
