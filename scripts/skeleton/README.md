# Walking skeleton — the cross-origin framing proof

This proves the two genuinely risky framing facts *for real*, in a browser, against the real
golden template. It lives under `scripts/` (not `sandbox/`) because it is cross-cutting.

## What it proves

### Real risks (`frame-proof/prove-framing.mjs`) — proven in a real browser
```
# 1. start the real golden template (deps already `npm ci`-installed):
cd sandbox/template && node_modules/.bin/next dev -p 3000
# 2. in another shell, run the proof (Playwright is resolved from portal/node_modules):
node scripts/skeleton/frame-proof/prove-framing.mjs
```

`frame-proof/servers.mjs` emulates the sandbox's **Caddy** in front of the real
`next dev`: a sandbox origin (`:4310`) that serves everything with
`Content-Security-Policy: frame-ancestors <portal-origin>` and **no** `X-Frame-Options`,
plus a portal framer (`:4300`), a **disallowed** framer (`:4320`), and a rogue
cross-origin poster (`:4315`). A real Chromium then proves **9/9 checks**:

1. **Real cross-origin render** — the golden template's `use client` + `useState` CRUD page
   (`Records` / `New record`) mounts inside a genuinely cross-origin iframe
   (`http://localhost:4300` frames `http://localhost:4310` → proxies `next dev` :3000).
2. **Origin-validated `postMessage`** — the handshake round-trips from the sandbox origin
   (`echoReady → ping → pong`) with an **explicit `targetOrigin`** (never `'*'`), and a
   **wrong-origin** message (the rogue `:4315`) is **rejected** by the `e.origin` guard.
3. **`frame-ancestors` enforced** — the same sandbox frame is **blocked** by the browser
   inside a disallowed parent origin (`:4320`).
4. **Sandbox containment** — the framed app **cannot** navigate `window.top` nor open a
   popup (the deliberately-withheld `sandbox=` tokens `allow-top-navigation*` /
   `allow-popups`).

These are the exact facts a mocked-away skeleton would false-green on. The portal seam this
proves out is `portal/src/components/LivePreview.tsx` (the cross-origin `src` + `sandbox=`
list + `e.origin` guard + explicit `targetOrigin`).

## What is emulated vs. real

| | Here | Real / authoritative |
|---|---|---|
| Sandbox Caddy | `frame-proof/servers.mjs` (a Node stand-in) | `sandbox/Caddyfile` |
| Golden template render | **REAL** — the actual `sandbox/template` on `next dev` | same, on Azure |
| Cross-origin framing | **REAL** — proven in Chromium | same, on public ACA ingress |

## Environment note (honest scope)

The **local Docker build is NOT the shipped-image verification.** The authoritative image
build is the **Windows `az acr build`**, and the full Azure cloud validation — the pre-baked
image booting, snapshot/restore, and HMR on real ACA — is the real acceptance gate, not a
claim this skeleton makes. At authoring time the local Docker Desktop content store was
corrupted (image-commit I/O errors), so the image was **not** built/run locally; instead the
template was proven via a direct `next dev` (this harness) and the supervisor env-scrub was
verified against `app.py`'s predicate. The frame-proof above uses a Node stand-in for Caddy
so the **framing mechanism** is proven for real without Docker — the header/token values are
byte-identical to `sandbox/Caddyfile`.
