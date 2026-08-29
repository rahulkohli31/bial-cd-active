/**
 * Structural invariants over the portal edge config (`portal/nginx.conf`).
 *
 * WHY A JS TEST OVER A CONFIG FILE. nginx.conf has no runtime surface this suite can import, and
 * the CI job that gates every PR has Node and no nginx binary. The BEHAVIOURAL half — that the
 * generated config parses, that a WebSocket upgrade is answered 101, that an unknown key is a 404
 * naming no upstream — lives in `portal/tests/test_nginx_apps_routing.py`, which stands a real
 * nginx up in Docker. Nothing here duplicates that. What is left over is a set of facts about the
 * config's SHAPE whose violations are silent: nginx serves happily, `nginx -t` stays green, and
 * the damage shows up as "the app renders the portal" or "the preview is blank" weeks later.
 *
 * Same pattern as `jsx-deploy-retirement.test.ts`: read the real file, assert the invariant,
 * anchor on `process.cwd()` because `import.meta.url` is a jsdom http URL under this config.
 *
 * The assertions parse the file into blocks rather than grepping it, so reformatting, rewrapping a
 * comment or moving a directive within its block does NOT fail the suite — only a change to what
 * the config MEANS does.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'

const RAW_CONF = readFileSync(path.resolve(process.cwd(), 'nginx.conf'), 'utf8')
const VITE_CONFIG = readFileSync(path.resolve(process.cwd(), 'vite.config.js'), 'utf8')

// ------------------------------------------------------------------------------------------
// A very small nginx reader. Quote-aware on purpose: the apps site's 404 page is an inline HTML
// string full of CSS braces (`body{margin:0}`), so naive brace counting walks straight off the
// end of the apps block and reports one server where there are two.
// ------------------------------------------------------------------------------------------

/** Comment text replaced by nothing, newlines kept. Prose is not configuration: the header
 *  comment alone mentions `Content-Security-Policy` and `location /`, and counting those as
 *  declarations is how a policy audit reports a number nobody wrote. */
function stripComments(text: string): string {
  let out = ''
  let quote: string | null = null
  let inComment = false
  for (const c of text) {
    if (inComment) {
      if (c === '\n') {
        inComment = false
        out += c
      }
      continue
    }
    if (quote) {
      if (c === quote) quote = null
      out += c
      continue
    }
    if (c === '#') {
      inComment = true
      continue
    }
    if (c === "'" || c === '"') quote = c
    out += c
  }
  return out
}

const CODE = stripComments(RAW_CONF)

/** Index just past the `}` matching the `{` at `open`. */
function matchBrace(text: string, open: number): number {
  let depth = 0
  let quote: string | null = null
  for (let i = open; i < text.length; i++) {
    const c = text[i]
    if (quote) {
      if (c === quote) quote = null
      continue
    }
    if (c === "'" || c === '"') {
      quote = c
      continue
    }
    if (c === '{') depth++
    else if (c === '}' && --depth === 0) return i + 1
  }
  throw new Error(`nginx.conf: unbalanced braces from offset ${open}`)
}

interface Block {
  /** Everything between the keyword and the `{` — a server's nothing, a location's match spec. */
  header: string
  /** The block's contents, nested blocks included. */
  body: string
}

/** Top-level blocks opened by `opener` (whose match must end at its `{`), skipping nested ones. */
function blocksOf(text: string, opener: RegExp): Block[] {
  const out: Block[] = []
  let consumedTo = 0
  for (const m of text.matchAll(new RegExp(opener.source, 'gm'))) {
    if (m.index < consumedTo) continue
    const open = m.index + m[0].lastIndexOf('{')
    const end = matchBrace(text, open)
    out.push({ header: (m[1] ?? '').trim(), body: text.slice(open + 1, end - 1) })
    consumedTo = end
  }
  return out
}

// The quoted-span alternative is load-bearing, not defensive: the key-shape locations are regexes
// containing `{28}`, so a plain `[^{]*?` stops at the repetition count and takes it for the block
// opener — which silently produces a "location" whose body is the rest of the file.
const LOCATION = /^[ \t]*location[ \t]+((?:"[^"]*"|[^{"])*?)[ \t]*\{/m

/** The block's own directives, with every `location` body removed — the difference between
 *  "declared in this server block" and "present somewhere inside it". Sibling server blocks
 *  inherit nothing from each other, so which level a directive sits at IS the invariant. */
function serverLevel(block: Block): string {
  let rest = block.body
  for (const loc of blocksOf(block.body, LOCATION)) rest = rest.replace(loc.body, '')
  return rest
}

function directiveValue(body: string, name: string): string | null {
  return body.match(new RegExp(`(?:^|\\n)[ \\t]*${name}[ \\t]+([^;]+);`))?.[1]?.trim() ?? null
}

const SERVERS = blocksOf(CODE, /^server[ \t]*\{/m)
const PORTAL = SERVERS[0]!
const APPS = SERVERS[1]!

// A key of the exact shape the router accepts, and the near-misses that must not be accepted.
const HEX28 = '0123456789abcdef0123456789ab'

describe('nginx.conf — the two sites are told apart, and the portal is still the default', () => {
  it('declares exactly two server blocks, portal first, apps second, on the same listen address', () => {
    // ORDERING IS THE INVARIANT, and it is invisible in the file. `server_name _` matches no real
    // Host; the portal works only because nginx serves an unmatched Host from the FIRST block on
    // the listen address. Put the apps block above and every portal request with an unexpected
    // Host is served the apps site — the portal goes dark with `nginx -t` green.
    expect(SERVERS).toHaveLength(2)
    expect(directiveValue(serverLevel(PORTAL), 'server_name')).toBe('_')
    expect(directiveValue(serverLevel(APPS), 'server_name')).toBe('${APPS_HOSTNAME}')
    // Same listener: the gateway routes both hostnames into one container, so a block that
    // invents its own port is simply unreachable.
    expect(directiveValue(serverLevel(PORTAL), 'listen')).toBe('${PORT}')
    expect(directiveValue(serverLevel(APPS), 'listen')).toBe('${PORT}')
  })
})

describe('nginx.conf — the apps site routes /a/<key>/ by composing the upstream', () => {
  const appsLocations = blocksOf(APPS.body, LOCATION)
  // The keyed arm identified by what it DOES (captures the key into $app_key) rather than by its
  // position or its literal regex — the sibling `^/a/` catch-all and the two `_sup` denials look
  // similar enough that an index would silently retarget these assertions.
  const keyed = appsLocations.find((l) => /set[ \t]+\$app_key[ \t]+\$1[ \t]*;/.test(l.body))!

  /** The keyed location's match spec, compiled. nginx matches `location ~ "…"` with PCRE; every
   *  construct used here means the same thing in JS, so the shape can be exercised directly
   *  instead of eyeballed. */
  function keyPattern(): RegExp {
    const source = keyed.header.match(/^~\s*"(.*)"$/)?.[1]
    if (!source) throw new Error(`keyed arm is not a quoted regex location: ${keyed.header}`)
    return new RegExp(source)
  }

  it('matches the exact key shape — prefix plus 28 lowercase hex — and captures the key', () => {
    const re = keyPattern()
    for (const prefix of ['sbx', 'pub']) {
      const key = `${prefix}-${HEX28}`
      expect(`/a/${key}/`.match(re)?.[1]).toBe(key)
      expect(`/a/${key}`.match(re)?.[1]).toBe(key) // no trailing slash is still the app root
      expect(`/a/${key}/api/items?q=1`.match(re)?.[1]).toBe(key)
    }
  })

  it('does NOT match a key of the wrong length, the wrong case, or the wrong alphabet', () => {
    // A looser match does not merely mis-route: it turns a mistyped key into a DNS lookup for an
    // attacker-named host from inside the VNet. The near-misses are the whole point of the shape.
    const re = keyPattern()
    for (const key of [
      `sbx-${HEX28.slice(0, 27)}`, // 27 hex
      `sbx-${HEX28}c`, // 29 hex
      `sbx-${HEX28.toUpperCase()}`, // uppercase hex
      `sbx-${HEX28.slice(0, 27)}g`, // non-hex character
      `xyz-${HEX28}`, // unknown prefix
      HEX28, // no prefix at all
    ]) {
      expect(re.test(`/a/${key}/`)).toBe(false)
      expect(re.test(`/a/${key}`)).toBe(false)
    }
  })

  it('composes the upstream from the captured key and the apps domain — no registry, no state', () => {
    expect(keyed.body).toMatch(/set[ \t]+\$app_host[ \t]+"\$app_key\.\$\{APPS_DOMAIN\}"[ \t]*;/)
    expect(keyed.body).toMatch(/proxy_pass[ \t]+https:\/\/\$app_host[ \t]*;/)
  })

  it('carries NO URI part on any proxy_pass — a stray slash collapses every request to /', () => {
    // Inside a regex location nginx cannot know which part of the URI the location matched, so a
    // URI on a variable `proxy_pass` REPLACES the request path outright. The keyed arm and the
    // keyless arm sit one character apart on this, with a total routing collapse on the wrong
    // side — which is the same failure the BACKEND_URL boot guard exists to prevent.
    const passes = [...APPS.body.matchAll(/proxy_pass[ \t]+([^;]+);/g)].map((m) => m[1]!.trim())
    expect(passes.length).toBeGreaterThan(0)
    for (const pass of passes) expect(pass).toBe('https://$app_host')
  })

  it('re-declares proxy_http_version 1.1 and its OWN resolver — neither is inherited', () => {
    // nginx inherits http -> server -> location and NEVER between sibling server blocks, and both
    // omissions pass `nginx -t`: without the version the block answers a WebSocket upgrade as an
    // ordinary request (live reload dies quietly), without its own resolver every variable
    // proxy_pass 502s at request time. Asserted at SERVER level, not merely "somewhere in here".
    const level = serverLevel(APPS)
    expect(directiveValue(level, 'proxy_http_version')).toBe('1.1')
    expect(directiveValue(level, 'resolver')).toBe('${DNS_RESOLVER} valid=30s ipv6=off')
    expect(directiveValue(level, 'resolver_timeout')).toBe('5s')
  })

  it('proxies NOTHING to the backend upstream — an app must not reach the control plane', () => {
    expect(APPS.body).not.toMatch(/backend_upstream/)
    expect(APPS.body).not.toMatch(/BACKEND_URL/)
    // …and the portal site is where that upstream is named, so the absence above is a boundary
    // rather than an accident of the backend having moved somewhere else entirely.
    expect(serverLevel(PORTAL)).toMatch(/set[ \t]+\$backend_upstream[ \t]+\$\{BACKEND_URL\}[ \t]*;/)
  })
})

describe('nginx.conf — the portal site survived the apps site (regression)', () => {
  // Asserted AFTER the apps block exists, on purpose: the risk this guards is not that the portal
  // routes were written wrong, it is that a later edit to the apps site reorders or deletes them.
  it('still declares every route the SPA and its API proxy depend on', () => {
    expect(SERVERS).toHaveLength(2)
    const headers = blocksOf(PORTAL.body, LOCATION).map((l) => l.header)
    for (const route of ['/api/v1/auth/', '/api/', '/apps/', '/assets/', '= /index.html', '/']) {
      expect(headers).toContain(route)
    }
  })

  it('still ends in the SPA history fallback, so a client-side route is not a 404', () => {
    const fallback = blocksOf(PORTAL.body, LOCATION).find((l) => l.header === '/')
    expect(fallback?.body).toMatch(/try_files[ \t]+\$uri[ \t]+\$uri\/[ \t]+\/index\.html[ \t]*;/)
  })
})

describe('nginx.conf — the framing policy names the apps host without revoking the old one', () => {
  const CSP = /add_header[ \t]+Content-Security-Policy[ \t]+"([^"]*)"[ \t]+always[ \t]*;/g
  const declared = [...CODE.matchAll(CSP)].map((m) => m[1]!)

  it('declares the SAME policy everywhere it declares one', () => {
    // A location-level add_header REPLACES every inherited one, so a declaration that drifts does
    // not warn — that one route just serves a weaker policy. Counted dynamically and compared as
    // a set, so this keeps holding at four declarations, or at six.
    expect(declared.length).toBeGreaterThanOrEqual(3)
    expect(new Set(declared).size).toBe(1)
  })

  it('permits the apps hostname and NOT the retired Container Apps wildcard', () => {
    // The wildcard was kept only while the portal was still handing the browser a
    // `*.${APPS_DOMAIN}` preview URL. That address has moved, so the wildcard now permits an
    // origin nothing produces — and re-adding it to "support direct ACA previews" would be
    // permitting an origin that does not resolve from a BIAL desk at all, which is the entire
    // reason this design exists. ${APPS_DOMAIN} is still a required input; its job is composing
    // the router's upstream, not this header.
    const policy = declared[0]!
    expect(policy).toContain('https://${APPS_HOSTNAME}')
    expect(policy).not.toContain('${APPS_DOMAIN}')
    expect(policy).toMatch(/frame-src 'self'/)
    // The portal itself stays un-frameable; this policy constrains framing and nothing else, so a
    // default-src/script-src creeping in here is a change of kind, not of degree.
    expect(policy).toMatch(/frame-ancestors 'self'/)
    expect(policy).not.toMatch(/default-src|script-src|connect-src/)
  })

  it('declares it once per header-overriding portal location, plus once at server level', () => {
    // THE FUTURE-PROOF FORM, and the reason this is not `toBe(3)`. Both halves have to fail on a
    // fourth declaration that was not kept in step: a new value fails the byte-identity test
    // above, and a new header-overriding location that FORGOT its policy fails this count — which
    // is the actually-dangerous version, because the route it weakens is the one nobody looks at.
    // It also fails if the apps site starts adding security headers, which it must not: the app's
    // own frame-ancestors is the single authority on who may frame an app.
    const overriding = blocksOf(PORTAL.body, LOCATION).filter((l) => /add_header/.test(l.body))
    expect(overriding.length).toBeGreaterThan(0)
    for (const loc of overriding) expect(loc.body).toMatch(/Content-Security-Policy/)
    expect(serverLevel(PORTAL)).toMatch(/Content-Security-Policy/)
    expect(declared).toHaveLength(overriding.length + 1)
  })
})

describe('vite.config.js — the dev server states the same framing policy as the edge', () => {
  // THE FOURTH COPY, in a different file and with no envsubst variable to follow, which is why it
  // gets left behind: when the edge learns a new framed origin and this does not, `npm run dev`
  // refuses to frame the preview with no nginx involved and no server-side trace — just a console
  // message. Pinned to the edge's SHAPE rather than its text, because the two legitimately spell
  // the same origins differently (`${APPS_HOSTNAME}` is a literal BIAL name on this side).
  const devPolicy = VITE_CONFIG.match(/'Content-Security-Policy':\s*\n?\s*"([^"]*)"/)?.[1]
  const edgePolicy = CODE.match(/add_header[ \t]+Content-Security-Policy[ \t]+"([^"]*)"/)?.[1]

  /** `frame-src 'self' https://x; frame-ancestors 'self'` -> { 'frame-src': [...], … }. */
  function directives(policy: string): Map<string, string[]> {
    return new Map(
      policy
        .split(';')
        .map((d) => d.trim())
        .filter(Boolean)
        .map((d) => {
          const [name, ...values] = d.split(/\s+/)
          return [name!, values] as const
        }),
    )
  }

  it('permits the apps hostname and NOT the retired Container Apps wildcard', () => {
    expect(devPolicy).toBeTruthy()
    expect(devPolicy).toContain('https://citizenapps.bialairport.com')
    expect(devPolicy).not.toContain('azurecontainerapps.io')
    expect(devPolicy).toMatch(/frame-ancestors 'self'/)
  })

  it('carries the same directives and the same number of framed origins as nginx.conf', () => {
    // Not a text comparison — a parity one. Add an origin to one and this goes red until the
    // other learns about it, which is the only mechanism keeping dev honest about production.
    expect(edgePolicy).toBeTruthy()
    const dev = directives(devPolicy!)
    const edge = directives(edgePolicy!)
    expect([...dev.keys()]).toEqual([...edge.keys()])
    expect(dev.get('frame-src')).toHaveLength(edge.get('frame-src')!.length)
  })
})
