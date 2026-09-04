# Walking skeleton — the Phase-2 Stage-0 exit gate

This is the cross-cutting walking skeleton for the agentic-build harness. It wires the
frozen contracts end-to-end over **mocks**, and — to avoid a false-green — it proves the
**two facts the decomposition doc flagged as the genuine risks** *for real*, in a browser,
against the real golden template. It lives under `scripts/` (not `sandbox/`, which Track
SANDBOX owns) because it is cross-cutting.

It is deliberately minimal: no session server, no activity feed, no reload logic — those
are Wave-1 (SESSION-API / PORTAL-PREVIEW). This is the seam that everything else builds on.

## What it proves

### Mocked seam (`run-seam.mjs`) — the contracts are buildable-to-the-doc
`node scripts/skeleton/run-seam.mjs`

A mock **C1** supervisor (`mock-supervisor.mjs`, built from
`docs/engineering/contracts/C1-supervisor-http-api.md` alone) and a mock **C7** brain
(`mock-brain.mjs`, mirroring `backend/src/api/v1/build_sessions/schemas.py`) are driven
end-to-end: the brain calls the supervisor over C1 (`/exec`, `/dev/start`, `/dev/status`,
bearer auth, the `Bearer {TOKEN}` 401), and emits the C7 progress envelope stream. The
driver (the SESSION-API relay stand-in) asserts the stream is a monotonic `seq`, every
`type` is a frozen member, and it ends `preview_ready → ended`. This is the
anti-"both-sides-invent-the-shape" check — a Wave-1 track can build a faithful mock from
the doc alone.

### Real risks (`frame-proof/prove-framing.mjs`) — proven in a real browser
```
# 1. start the real golden template (deps already `npm ci`-installed):
cd sandbox/template && node_modules/.bin/next dev -p 3000
# 2. in another shell, run the proof (Playwright is resolved from portal/node_modules):
node scripts/skeleton/frame-proof/prove-framing.mjs
```

`frame-proof/servers.mjs` emulates the sandbox's **Caddy** (C8) in front of the real
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
   popup (the deliberately-withheld C8 `sandbox=` tokens `allow-top-navigation*` /
   `allow-popups`).

These are the exact facts a mocked-away skeleton would false-green on. The portal seam that
Wave-1 PORTAL-PREVIEW builds on is `portal/src/components/LivePreview.jsx` (the C8
cross-origin `src` + `sandbox=` list + `e.origin` guard + explicit `targetOrigin`).

## What is mocked vs. real vs. deferred

| | Here | Real / authoritative |
|---|---|---|
| C1 supervisor | `mock-supervisor.mjs` (contract shape) | `sandbox/supervisor/app.py` (forked byte-identical from the proven spike) |
| C7 brain | `mock-brain.mjs` (envelope shape) | Wave-1 BRAIN (`services/orchestrator/`) |
| Sandbox Caddy (C8) | `frame-proof/servers.mjs` (a Node stand-in) | `sandbox/Caddyfile` |
| Golden template render | **REAL** — the actual `sandbox/template` on `next dev` | same, on Azure (Track SANDBOX) |
| Cross-origin framing (C8) | **REAL** — proven in Chromium | same, on public ACA ingress |

## Environment note (honest scope — D9 / ADR-0015)

The **local Docker build is NOT the shipped-image verification.** The authoritative image
build is the **Windows `az acr build`** (ADR-0015), and the full Azure cloud validation —
the pre-baked image booting, snapshot/restore, and HMR on real ACA — is **Track SANDBOX's
acceptance gate**, not a Stage-0 claim. At Stage-0 authoring the local Docker Desktop
content store was corrupted (image-commit I/O errors), so the image was **not** built/run
locally; instead the template was proven via a direct `next dev` (this harness) and the
supervisor env-scrub was verified against `app.py`'s predicate. The frame-proof above uses a
Node stand-in for Caddy so the **C8 framing mechanism** is proven for real without Docker —
the header/token values are byte-identical to `sandbox/Caddyfile`.

## Status

AUDIT-2026-09-03 · verified-alive: intentionally retained pending verification — see the audit record.
