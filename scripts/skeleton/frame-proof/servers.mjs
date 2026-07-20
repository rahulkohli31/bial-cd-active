// Frame-proof servers — the REAL cross-origin framing rig (no docker/Caddy needed).
//
// Emulates the sandbox's Caddy (docs/engineering/contracts/C8-preview-transport-framing.md)
// in front of a real `next dev`, plus a portal framer and a rogue poster, so a browser can
// prove — for real — the two facts the decomposition doc flagged as the genuine risks:
//   1. a genuinely cross-origin frame with `frame-ancestors <portal-origin>` + origin-validated
//      postMessage (accepts the sandbox origin, rejects everything else);
//   2. the real golden template rendering inside that cross-origin frame.
//
// Origins (distinct ports = distinct origins):
//   :4310  SANDBOX  — `/echo` (a controlled C8-handshake page) + proxy of everything else to
//                     `next dev` (:3000); every response carries `frame-ancestors <PORTAL>` and
//                     NO X-Frame-Options (the Caddyfile's next-dev block).
//   :4315  ROGUE    — `/rogue`, an untrusted cross-origin page that posts to window.top; the
//                     portal's origin guard MUST reject it.
//   :4300  PORTAL   — the ALLOWED framer (frame-ancestors permits it).
//   :4320  PORTAL'  — a DISALLOWED framer (frame-ancestors must block the sandbox frame in it).

import http from 'node:http'

export const PORTS = { sandbox: 4310, rogue: 4315, portalAllowed: 4300, portalDisallowed: 4320, nextDev: 3000 }
export const ORIGIN = {
  sandbox: `http://localhost:${PORTS.sandbox}`,
  rogue: `http://localhost:${PORTS.rogue}`,
  portalAllowed: `http://localhost:${PORTS.portalAllowed}`,
  portalDisallowed: `http://localhost:${PORTS.portalDisallowed}`,
}

// ---- HTML for the controlled pages (kept inline so the rig is one file) --------------------

// The trusted preview echo page (served at SANDBOX:/echo). Implements the C8 handshake with
// EXPLICIT targetOrigin, an origin guard on inbound messages, a live JS counter (proves the
// cross-origin frame runs script), and the containment probes for the WITHHELD sandbox tokens.
const ECHO_HTML = `<!doctype html><meta charset="utf-8"><title>echo</title>
<div id="app">count: <span id="v">0</span></div>
<script>
  const parentOrigin = new URLSearchParams(location.search).get('parentOrigin');
  // Prove JS runs in the cross-origin frame: bump a counter into the DOM.
  let n = 1; document.getElementById('v').textContent = String(n);
  // C8 handshake: announce readiness to the parent with an EXPLICIT targetOrigin (never '*').
  window.addEventListener('message', (e) => {
    if (e.origin !== parentOrigin) return;                 // origin-validate inbound too
    if (e.data && e.data.ping) window.parent.postMessage({ pong: true }, parentOrigin);
  });
  if (parentOrigin) window.parent.postMessage({ echoReady: true }, parentOrigin);
  // Containment probes: the withheld tokens must block top-nav + popups.
  try { window.top.location = 'http://evil.example/'; } catch (_) {}
  try { window.__popup = window.open('http://evil.example/'); } catch (_) { window.__popup = null; }
</script>`

// The rogue poster (served at ROGUE:/rogue): posts to window.top immediately. The portal's
// origin guard must REJECT it (its origin is not the sandbox origin).
const ROGUE_HTML = `<!doctype html><meta charset="utf-8"><title>rogue</title>
<script>
  try { window.top.postMessage({ echoReady: true, rogue: true }, '*'); } catch (_) {}
  try { window.parent.postMessage({ echoReady: true, rogue: true }, '*'); } catch (_) {}
</script>`

// The portal framer (served at PORTAL / PORTAL'). Frames SANDBOX + a rogue, runs the
// origin-validated postMessage handshake, and records everything on window.__skeletonResult.
function portalHtml() {
  return `<!doctype html><meta charset="utf-8"><title>portal</title>
<body>
<script>
  const S = ${JSON.stringify(ORIGIN.sandbox)}, R = ${JSON.stringify(ORIGIN.rogue)};
  const target = new URLSearchParams(location.search).get('target') || '/echo';
  const initialHref = location.href;
  const result = { frameLoaded:false, echoReady:false, pongReceived:false,
                   acceptedOrigins:[], rejectedOrigins:[], topUnchanged:true };
  window.__skeletonResult = result;
  window.addEventListener('message', (e) => {
    // C8 origin validation: accept ONLY the sandbox origin; reject all others (incl. the rogue).
    if (e.origin !== S) { result.rejectedOrigins.push(e.origin); return; }
    result.acceptedOrigins.push(e.origin);
    if (e.data && e.data.echoReady) {
      result.echoReady = true;
      frame.contentWindow.postMessage({ ping: 'from-portal' }, S);   // explicit targetOrigin
    }
    if (e.data && e.data.pong) result.pongReceived = true;
  });
  const frame = document.createElement('iframe');
  frame.id = 'preview';
  frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-downloads');
  frame.src = S + target + (target === '/echo' ? '?parentOrigin=' + encodeURIComponent(location.origin) : '');
  frame.onload = () => { result.frameLoaded = true; };
  document.body.appendChild(frame);
  const rogue = document.createElement('iframe');
  rogue.src = R + '/rogue'; rogue.style.display = 'none';
  document.body.appendChild(rogue);
  // Top-nav hijack detector: a framed top-navigation would change our href.
  setInterval(() => { if (location.href !== initialHref) result.topUnchanged = false; }, 100);
</script>`
}

// ---- servers -------------------------------------------------------------------------------

function reply(res, status, headers, body) {
  res.writeHead(status, headers)
  res.end(body)
}

// The C8 frame-ancestors header the sandbox Caddy emits on the next-dev block (+ no XFO).
function frameAncestorsHeaders(portalOrigin) {
  return { 'content-security-policy': `frame-ancestors ${portalOrigin}` }
}

function proxyToNextDev(req, res, portalOrigin) {
  const opts = {
    host: '127.0.0.1',
    port: PORTS.nextDev,
    method: req.method,
    path: req.url,
    headers: { ...req.headers, host: `127.0.0.1:${PORTS.nextDev}` },
  }
  const upstream = http.request(opts, (up) => {
    const headers = { ...up.headers, ...frameAncestorsHeaders(portalOrigin) }
    delete headers['x-frame-options'] // the Caddyfile's `header -X-Frame-Options`
    res.writeHead(up.statusCode || 502, headers)
    up.pipe(res)
  })
  upstream.on('error', () => reply(res, 502, { 'content-type': 'text/plain' }, 'next dev unreachable'))
  req.pipe(upstream)
}

function makeSandboxServer(portalOrigin) {
  return http.createServer((req, res) => {
    const path = new URL(req.url, ORIGIN.sandbox).pathname
    if (path === '/echo') {
      return reply(res, 200, { 'content-type': 'text/html; charset=utf-8', ...frameAncestorsHeaders(portalOrigin) }, ECHO_HTML)
    }
    return proxyToNextDev(req, res, portalOrigin)
  })
}

function makeStaticServer(html) {
  return http.createServer((_req, res) =>
    reply(res, 200, { 'content-type': 'text/html; charset=utf-8' }, typeof html === 'function' ? html() : html),
  )
}

/** Start all rig servers. `next dev` is expected to be running separately on :3000. */
export async function startServers() {
  const servers = [
    [makeSandboxServer(ORIGIN.portalAllowed), PORTS.sandbox],
    [makeStaticServer(ROGUE_HTML), PORTS.rogue],
    [makeStaticServer(portalHtml), PORTS.portalAllowed],
    [makeStaticServer(portalHtml), PORTS.portalDisallowed],
  ]
  await Promise.all(
    servers.map(([s, port]) => new Promise((resolve) => s.listen(port, '127.0.0.1', resolve))),
  )
  return () => servers.forEach(([s]) => s.close())
}

if (import.meta.url === `file://${process.argv[1]}`) {
  startServers().then(() => console.log('frame-proof servers up: sandbox 4310, rogue 4315, portal 4300/4320'))
}
