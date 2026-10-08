/**
 * Structural invariants over the portal edge config (`portal/nginx.conf`).
 *
 * A JS test over a config file because nginx.conf has no runtime surface to import and CI has no
 * nginx binary — the BEHAVIOURAL half (parses, WebSocket upgrades answer 101, an unknown key
 * 404s) lives in `portal/tests/test_nginx_apps_routing.py` against a real nginx. What's left is
 * the config's SHAPE, whose violations are silent: `nginx -t` stays green and the damage shows up
 * weeks later as "the preview is blank".
 *
 * Reads the real file, anchored on `process.cwd()` (matches `jsx-deploy-retirement.test.ts`).
 * Assertions parse into blocks rather than grep, so reformatting or moving a directive does NOT
 * fail the suite — only a change to what it MEANS does.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'

const RAW_CONF = readFileSync(path.resolve(process.cwd(), 'nginx.conf'), 'utf8')
const VITE_CONFIG = readFileSync(path.resolve(process.cwd(), 'vite.config.js'), 'utf8')

// A very small nginx reader. Quote-aware on purpose: the apps site's 404 page is an inline HTML
// string full of CSS braces (`body{margin:0}`), so naive brace counting walks straight off the
// end of the apps block and reports one server where there are two.

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
const PORTAL_LOCATIONS = blocksOf(PORTAL.body, LOCATION)
const APPS_LOCATIONS = blocksOf(APPS.body, LOCATION)

/** Every header a block hides from its upstream, sorted. */
function hiddenHeaders(body: string): string[] {
  return [...body.matchAll(/proxy_hide_header[ \t]+([^;\s]+)[ \t]*;/g)].map((m) => m[1]!).sort()
}

// A key of the exact shape the router accepts, and the near-misses that must not be accepted.
const HEX28 = '0123456789abcdef0123456789ab'

describe('nginx.conf — the two sites are told apart, and the portal is still the default', () => {
  it('declares exactly two server blocks, portal first, apps second, on the same listen address', () => {
    // ORDERING IS THE INVARIANT, and it is invisible in the file: `server_name _` matches no real
    // Host, so nginx serves an unmatched Host from the FIRST block on the listen address. Apps
    // above portal means every unexpected-Host request goes to apps — with `nginx -t` green.
    expect(SERVERS).toHaveLength(2)
    expect(directiveValue(serverLevel(PORTAL), 'server_name')).toBe('_')
    expect(directiveValue(serverLevel(APPS), 'server_name')).toBe('${APPS_HOSTNAME}')
    // Same listener: the gateway routes both hostnames into one container, so a block that
    // invents its own port is simply unreachable.
    expect(directiveValue(serverLevel(PORTAL), 'listen')).toBe('${PORT}')
    expect(directiveValue(serverLevel(APPS), 'listen')).toBe('${PORT}')
  })
})

describe('nginx.conf — the apps site routes /a/<key>/ by alias lookup or by composing the upstream', () => {
  const appsLocations = blocksOf(APPS.body, LOCATION)
  // The keyed arms identified by what they DO (capture the key into $app_key), told apart by whether
  // they ask the lookup — the sibling `^/a/` catch-all and the `_sup` denials look similar enough
  // that an index would silently retarget these assertions.
  const keyedArms = appsLocations.filter((l) => /set[ \t]+\$app_key[ \t]+\$1[ \t]*;/.test(l.body))
  const aliasArm = keyedArms.find((l) => /auth_request[ \t]/.test(l.body))!
  const pubArm = keyedArms.find((l) => !/auth_request[ \t]/.test(l.body))!

  /** A keyed location's match spec, compiled. nginx matches `location ~ "…"` with PCRE; every
   *  construct used here means the same thing in JS, so the shape can be exercised directly
   *  instead of eyeballed. */
  function keyPattern(arm: Block): RegExp {
    const source = arm.header.match(/^~\s*"(.*)"$/)?.[1]
    if (!source) throw new Error(`keyed arm is not a quoted regex location: ${arm.header}`)
    return new RegExp(source)
  }
  const HEX32 = HEX28 + 'cdef'

  it('matches the exact key shape — an alias of 32 lowercase hex, or `pub-` plus 28 — and captures the key', () => {
    for (const [arm, key] of [
      [aliasArm, HEX32],
      [pubArm, `pub-${HEX28}`],
    ] as const) {
      const re = keyPattern(arm)
      expect(`/a/${key}/`.match(re)?.[1]).toBe(key)
      expect(`/a/${key}`.match(re)?.[1]).toBe(key) // no trailing slash is still the app root
      expect(`/a/${key}/api/items?q=1`.match(re)?.[1]).toBe(key)
    }
  })

  it('does NOT match a key of the wrong length, case or alphabet — and never a preview container name', () => {
    // A looser match does not merely mis-route: it turns a mistyped key into a DNS lookup for an
    // attacker-named host from inside the VNet. A container name (`sbx-`, `shr-`) matching either arm
    // would be the preview's identifier working in the address again.
    const nearMisses = [
      HEX32.slice(0, 31), // 31 hex
      `${HEX32}c`, // 33 hex
      HEX32.toUpperCase(), // uppercase hex
      `${HEX32.slice(0, 31)}g`, // non-hex character
      `xyz-${HEX28}`, // unknown prefix
      `sbx-${HEX28}`, // a preview's container name
      `shr-${HEX28}`, // a shared view's container name
      `pub-${HEX28.slice(0, 27)}`, // 27 hex
      `pub-${HEX28}c`, // 29 hex
    ]
    for (const arm of [aliasArm, pubArm]) {
      const re = keyPattern(arm)
      for (const key of nearMisses) {
        expect(re.test(`/a/${key}/`)).toBe(false)
        expect(re.test(`/a/${key}`)).toBe(false)
      }
    }
  })

  it('composes the upstream from a validated map, never from a server-level `set` — and a published app asks no lookup', () => {
    // A server-level `set` runs again inside the lookup subrequest and gives every alias the same
    // value: every app routed to whichever container was looked up first.
    expect(CODE).toMatch(/map[ \t]+"\$app_key\|\$route_container"[ \t]+\$app_host[ \t]*\{/)
    expect(serverLevel(APPS)).not.toMatch(/(?:^|\n)[ \t]*set[ \t]/)
    for (const arm of keyedArms) expect(arm.body).toMatch(/proxy_pass[ \t]+https:\/\/\$app_host[ \t]*;/)
    expect(aliasArm.body).toMatch(/auth_request[ \t]+\/__bial_route[ \t]*;/)
    expect(pubArm.body).not.toMatch(/auth_request|BACKEND_URL/)
  })

  it('carries NO URI part on any app proxy_pass — a stray slash collapses every request to /', () => {
    // Inside a regex location nginx cannot know which part of the URI the location matched, so a
    // URI on a variable `proxy_pass` REPLACES the request path outright — a total routing collapse,
    // the same failure the BACKEND_URL boot guard exists to prevent. The two backend passes are the
    // deliberate exception: each names its backend route in full.
    const passes = [...APPS.body.matchAll(/proxy_pass[ \t]+([^;]+);/g)].map((m) => m[1]!.trim())
    expect(passes.length).toBeGreaterThan(0)
    for (const pass of passes.filter((p) => !p.startsWith('${BACKEND_URL}'))) {
      expect(pass).toBe('https://$app_host')
    }
  })

  it('re-declares proxy_http_version 1.1 and its OWN resolver — neither is inherited', () => {
    // nginx inherits http -> server -> location and NEVER between sibling server blocks, and both
    // omissions pass `nginx -t`: without the version, WebSocket upgrades answer as ordinary
    // requests (live reload dies quietly); without its own resolver, proxy_pass 502s at request time.
    const level = serverLevel(APPS)
    expect(directiveValue(level, 'proxy_http_version')).toBe('1.1')
    expect(directiveValue(level, 'resolver')).toBe('${DNS_RESOLVER} valid=30s ipv6=off')
    expect(directiveValue(level, 'resolver_timeout')).toBe('5s')
  })

  it('reaches the backend only through the alias lookup and the preview entry — an app must not reach the control plane', () => {
    expect(APPS.body).not.toMatch(/backend_upstream/)
    const naming = appsLocations.filter((l) => /BACKEND_URL/.test(l.body))
    expect(naming.map((l) => l.header).sort()).toEqual(['= /__bial_enter', '= /__bial_route'])
    // Neither forwards a browser header, and the secret travels in a header the edge itself sets.
    for (const loc of naming) {
      expect(loc.body).toMatch(/proxy_pass_request_headers[ \t]+off[ \t]*;/)
    }
    // The lookup is unreachable from a browser; the entry route is reachable by design.
    const lookup = naming.find((l) => l.header === '= /__bial_route')
    expect(lookup?.body).toMatch(/(?:^|\n)[ \t]*internal[ \t]*;/)
    expect(serverLevel(APPS)).not.toMatch(/BACKEND_URL|INTERNAL_ROUTE_TOKEN/)
    // …and the portal site is where the backend is otherwise named, so the above is a boundary
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

describe('nginx.conf — each site declares its own request-body ceiling', () => {
  it('sets the portal above the attachment route and leaves the apps site where it was', () => {
    // The portal's sits above the attachment route's own request ceiling, so the route's refusal,
    // which names the limit for the file's type, is what a citizen reads instead of nginx's bare
    // 413. Sibling server blocks inherit nothing, so each has to carry its own.
    expect(directiveValue(serverLevel(PORTAL), 'client_max_body_size')).toBe('45m')
    expect(directiveValue(serverLevel(APPS), 'client_max_body_size')).toBe('40m')
  })
})

describe('nginx.conf — the portal serves two policies: framing everywhere, full on the document', () => {
  const CSP = /add_header[ \t]+Content-Security-Policy[ \t]+"([^"]*)"[ \t]+always[ \t]*;/g
  const policyOf = (body: string) => [...body.matchAll(CSP)].map((m) => m[1]!)
  const documentLocation = PORTAL_LOCATIONS.find((l) => l.header === '= /index.html')!
  const framing = [
    ...policyOf(serverLevel(PORTAL)),
    ...PORTAL_LOCATIONS.filter((l) => l !== documentLocation).flatMap((l) => policyOf(l.body)),
  ]
  const documentPolicy = policyOf(documentLocation.body)

  it('declares the SAME framing policy everywhere except the document', () => {
    // A location-level add_header REPLACES every inherited one, so a declaration that drifts does
    // not warn — that route just serves a weaker policy. Compared as a set, holding at any count.
    expect(framing.length).toBeGreaterThanOrEqual(2)
    expect(new Set(framing).size).toBe(1)
  })

  it('keeps the framing policy to framing, because the sign-in page brings its own', () => {
    // The callback page carries the backend's nonce policy and browsers enforce every CSP on a
    // response, so a script-src here would block that nonce script and every sign-in with it.
    const policy = framing[0]!
    expect(policy).toContain('https://${APPS_HOSTNAME}')
    expect(policy).not.toContain('${APPS_DOMAIN}')
    expect(policy).toMatch(/frame-src 'self'/)
    expect(policy).toMatch(/frame-ancestors 'self'/)
    expect(policy).not.toMatch(/default-src|script-src|connect-src/)
  })

  it('serves the full policy on the document, once, framing the same origins', () => {
    expect(documentPolicy).toHaveLength(1)
    const policy = documentPolicy[0]!
    for (const directive of [
      "default-src 'self'",
      "script-src 'self';",
      "object-src 'none'",
      "base-uri 'self'",
      "frame-ancestors 'self'",
    ]) {
      expect(policy).toContain(directive)
    }
    expect(policy).toContain(framing[0]!.split(';')[0]!.trim())
    expect(policy).not.toMatch(/unsafe-eval/)
    expect(policy).not.toMatch(/script-src[^;]*unsafe-inline/)
  })

  it('declares a policy in every header-overriding portal location, plus once at server level', () => {
    // A new header-overriding location that FORGOT its policy fails only this check. Also fails
    // if the apps site starts adding security headers, which it must not.
    const overriding = PORTAL_LOCATIONS.filter((l) => /add_header/.test(l.body))
    expect(overriding.length).toBeGreaterThan(0)
    for (const loc of overriding) expect(loc.body).toMatch(/Content-Security-Policy/)
    expect(policyOf(serverLevel(PORTAL))).toHaveLength(1)
    expect(policyOf(APPS.body)).toHaveLength(0)
  })
})

describe('nginx.conf — one copy of each security header, and no version', () => {
  const HSTS = 'add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;'

  it('never names the nginx version, on either site', () => {
    // http context, outside both servers, so neither site can forget it.
    const outsideServers = SERVERS.reduce((rest, block) => rest.replace(block.body, ''), CODE)
    expect(directiveValue(outsideServers, 'server_tokens')).toBe('off')
  })

  it('hides the backend copies of the four headers on every proxied response', () => {
    // Declared at server level and inherited; a location that declared its own proxy_hide_header
    // would silently drop all four, so none may.
    expect(hiddenHeaders(serverLevel(PORTAL))).toEqual([
      'Referrer-Policy',
      'Strict-Transport-Security',
      'X-Content-Type-Options',
      'X-Frame-Options',
    ])
    for (const loc of PORTAL_LOCATIONS) expect(loc.body).not.toMatch(/proxy_hide_header/)
  })

  it('sends HSTS from the server level and every header-overriding location', () => {
    expect(serverLevel(PORTAL)).toContain(HSTS)
    for (const loc of PORTAL_LOCATIONS.filter((l) => /add_header/.test(l.body))) {
      expect(loc.body).toContain(HSTS)
    }
  })

  it('keeps the referrer policy that sign-in depends on', () => {
    // `no-referrer` makes the callback's form post arrive with `Origin: null`, which the backend
    // refuses as a cross-origin write.
    const policies = [...CODE.matchAll(/add_header[ \t]+Referrer-Policy[ \t]+"([^"]*)"/g)].map(
      (m) => m[1],
    )
    expect(new Set(policies)).toEqual(new Set(['strict-origin-when-cross-origin']))
  })
})

describe('nginx.conf — the apps site serves no Next source and no Next dev endpoint', () => {
  const blockIndex = APPS_LOCATIONS.findIndex((l) => l.header.includes('__nextjs_'))

  it('answers 404 before either proxy arm can match', () => {
    // Regex locations are tried in file order and the first match wins; the keyless prefix arm
    // loses to any regex. So the block must precede the keyed arm, and must also match keyless
    // paths, because Next's dev client asks for some of these root-relative.
    const keyedIndex = APPS_LOCATIONS.findIndex((l) => /\$app_key \$1/.test(l.body))
    expect(blockIndex).toBeGreaterThanOrEqual(0)
    expect(blockIndex).toBeLessThan(keyedIndex)
    const block = APPS_LOCATIONS[blockIndex]!
    expect(block.body).toMatch(/return[ \t]+404[ \t]*;/)
    expect(block.body).not.toMatch(/proxy_pass/)
  })

  it('matches the dev surface at the app root only, so an app keeps its own routes', () => {
    const pattern = new RegExp(APPS_LOCATIONS[blockIndex]!.header.replace(/^~\s*"|"$/g, ''))
    expect(pattern.test(`/a/sbx-${HEX28}/_next/static/chunks/main.js.map`)).toBe(true)
    expect(pattern.test('/_next/static/chunks/main.js.map')).toBe(true)
    expect(pattern.test(`/a/sbx-${HEX28}/__nextjs_source-map`)).toBe(true)
    expect(pattern.test('/__nextjs_original-stack-frames')).toBe(true)
    expect(pattern.test('/__nextjs_restart_dev')).toBe(true)
    expect(pattern.test(`/a/sbx-${HEX28}/_next/mcp`)).toBe(true)
    expect(pattern.test('/_next/mcp')).toBe(true)
    expect(pattern.test(`/a/pub-${HEX28}/_next/development/request-insights`)).toBe(true)
    expect(pattern.test(`/a/pub-${HEX28}/data/route.map`)).toBe(false)
    expect(pattern.test(`/a/sbx-${HEX28}/_next/static/chunks/main.js`)).toBe(false)
    expect(pattern.test(`/a/sbx-${HEX28}/_next/hmr`)).toBe(false)
    expect(pattern.test(`/a/sbx-${HEX28}/mcp`)).toBe(false)
    // Both branches are anchored: an unanchored one would match these deeper paths, and its
    // `.*` would restart at every `/_next/` of a crafted URI.
    expect(pattern.test(`/a/pub-${HEX28}/docs/_next/x.map`)).toBe(false)
    expect(pattern.test(`/a/pub-${HEX28}/data/__nextjs_x`)).toBe(false)
    expect(pattern.test(`/a/sbx-${HEX28}` + '/_next/'.repeat(1100) + 'x')).toBe(false)
  })

  it('hides the headers that name the software behind an app or widen a service worker', () => {
    expect(hiddenHeaders(serverLevel(APPS))).toEqual(['Service-Worker-Allowed', 'Via', 'X-Powered-By'])
    // A location that declared its own would drop all three.
    for (const loc of APPS_LOCATIONS) expect(loc.body).not.toMatch(/proxy_hide_header/)
  })
})

describe('vite.config.js — the dev server states the same framing policy as the edge', () => {
  // THE FOURTH COPY, in a different file with no envsubst variable to follow — when the edge
  // learns a new framed origin and this file doesn't, `npm run dev` silently refuses to frame the
  // preview with no server-side trace. Pinned to the edge's SHAPE, since the two spell origins differently.
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
