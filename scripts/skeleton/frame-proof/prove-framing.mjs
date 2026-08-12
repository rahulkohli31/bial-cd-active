// Frame-proof driver (REAL leg) — drives a real Chromium to prove the two facts the
// decomposition doc flagged as the genuine risks, plus the deepening-finding containment
// checks. Requires a real `next dev` already serving the golden template on :3000
// (see README). Playwright is resolved from portal/node_modules (where it is installed).
//
//   A. REAL RENDER, FRAMED: the golden template's `use client` + useState CRUD page mounts
//      inside a genuinely cross-origin iframe (portal :4300 → sandbox :4310 → next dev :3000).
//   B. ORIGIN-VALIDATED postMessage: the handshake round-trips from the sandbox origin and a
//      WRONG-origin (rogue :4315) message is REJECTED by the e.origin guard.
//   C. frame-ancestors ENFORCED: the same sandbox frame is BLOCKED inside a disallowed
//      parent origin (:4320).
//   D. SANDBOX CONTAINMENT: the framed app cannot navigate window.top nor open a popup
//      (the deliberately-withheld C8 sandbox tokens).

import { createRequire } from 'node:module'
import { startServers, ORIGIN } from './servers.mjs'

const require = createRequire(new URL('../../../portal/package.json', import.meta.url))
const { chromium } = require('playwright')

const checks = []
const check = (name, cond, detail = '') => {
  checks.push({ name, ok: !!cond, detail })
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`)
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function crossOriginFrame(page, urlPart) {
  for (let i = 0; i < 60; i++) {
    const f = page.frames().find((fr) => fr.url().includes(urlPart))
    if (f && f.url() !== 'about:blank') return f
    await sleep(250)
  }
  return null
}

async function main() {
  const stop = await startServers()
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage()

    // --- A. real golden template renders inside the cross-origin frame ---------------------
    await page.goto(`${ORIGIN.portalAllowed}/?target=/records`, { waitUntil: 'load' })
    const tframe = await crossOriginFrame(page, ':4310')
    let rendered = ''
    if (tframe) {
      // next dev's FIRST hit compiles the route — poll the framed DOM for the useState page.
      for (let i = 0; i < 60; i++) {
        rendered = await tframe.evaluate(() => document.body.innerText).catch(() => '')
        if (rendered.includes('Records') && rendered.includes('New record')) break
        await sleep(500)
      }
    }
    check('A cross-origin frame is genuinely cross-origin', tframe && new URL(tframe.url()).origin === ORIGIN.sandbox, tframe ? new URL(tframe.url()).origin : 'no frame')
    check('A real golden-template use-client+useState CRUD page rendered in the frame', rendered.includes('Records') && rendered.includes('New record'))

    // --- B. origin-validated postMessage round-trip + wrong-origin rejection ---------------
    await page.goto(`${ORIGIN.portalAllowed}/?target=/echo`, { waitUntil: 'load' })
    let res = {}
    for (let i = 0; i < 40; i++) {
      res = await page.evaluate(() => window.__skeletonResult)
      if (res.echoReady && res.pongReceived && res.rejectedOrigins.length) break
      await sleep(150)
    }
    check('B cross-origin postMessage round-trips (echoReady + pong)', res.echoReady && res.pongReceived)
    check('B the sandbox origin is accepted by the e.origin guard', res.acceptedOrigins.includes(ORIGIN.sandbox))
    check('B a WRONG-origin (rogue) message is REJECTED', res.rejectedOrigins.includes(ORIGIN.rogue) && !res.acceptedOrigins.includes(ORIGIN.rogue))

    // The echo frame ran JS (count bumped to 1) — a real cross-origin render.
    const echoFrame = await crossOriginFrame(page, ':4310/echo')
    const echoText = echoFrame ? await echoFrame.evaluate(() => document.getElementById('v').textContent) : ''
    check('B the cross-origin echo frame runs JS (counter rendered)', echoText === '1', `count=${echoText}`)

    // --- D. sandbox containment (withheld tokens) ------------------------------------------
    const popup = echoFrame ? await echoFrame.evaluate(() => window.__popup) : 'no-frame'
    check('D framed app cannot open a popup (allow-popups withheld)', popup === null || popup === undefined, `window.open → ${popup}`)
    check('D framed app did not navigate window.top (allow-top-navigation withheld)', res.topUnchanged === true)

    // --- C. frame-ancestors blocks a DISALLOWED parent origin ------------------------------
    await page.goto(`${ORIGIN.portalDisallowed}/?target=/echo`, { waitUntil: 'load' })
    let blocked = {}
    // Give it the same budget the allowed case needed (40 × 150ms ≈ 6000ms); echoReady must NEVER arrive.
    for (let i = 0; i < 40; i++) {
      blocked = await page.evaluate(() => window.__skeletonResult)
      await sleep(150)
    }
    check('C frame-ancestors BLOCKS the sandbox frame in a disallowed parent origin', blocked.echoReady === false)

    await page.close()
  } finally {
    await browser.close()
    stop()
  }

  const failed = checks.filter((c) => !c.ok)
  console.log(`\n${checks.length - failed.length}/${checks.length} real-frame checks passed`)
  if (failed.length) {
    console.error('FAILED:', failed.map((c) => c.name).join('; '))
    process.exit(1)
  }
  console.log('OK — the two real risks are proven once (cross-origin frame-ancestors + origin-validated postMessage, and a real golden-template next dev render in the frame).')
}

main().catch((err) => {
  console.error('FATAL', err)
  process.exit(1)
})
