# `sandbox/` — golden Next.js CRUD template + pre-baked base image

This is the **golden scaffold** every generated BIAL app is built from: a fixed, modern Next.js
CRUD template plus the in-container **supervisor + Caddy + entrypoint** that run it, baked into one
Docker image. Authored in Stage 0 (unit U10). **Track SANDBOX owns hardening this tree in Wave 1**
(PTY-backed `/exec`, snapshot/restore, and the real Azure acceptance test) — Stage 0 only authors it.

The design goal is **hallucination reduction**: the AI writes *feature code only*. The stack, the data
module, the error-capture shim, and the container plumbing are fixed so every build session starts
byte-identical.

## Layout

```
sandbox/
├── Dockerfile.sandbox     # node:24-bookworm-slim base; bakes node_modules + the template
├── Caddyfile              # ingress :8080 → /_sup/* supervisor, /* → next dev (+ C8 framing)
├── entrypoint.sh          # PID 1: caddy + supervisor (root; children demoted to appuser)
├── supervisor/
│   └── app.py             # the C1 supervisor HTTP API — forked VERBATIM from the proven spike
└── template/              # the golden Next.js app (App Router + TS + Tailwind v4 + shadcn/ui)
    ├── package.json / package-lock.json   # latest-stable-then-pinned deps (see below)
    ├── app/               # layout.tsx · page.tsx · records/page.tsx (the one example CRUD screen)
    ├── components/bial/error-capture.tsx  # window.onerror/unhandledrejection/console capture (C7)
    ├── components/ui/     # the FIXED shadcn/ui set (button, dialog, table, form, select, …)
    └── lib/bial-data.ts   # THE single swappable data-access module (HTTP client, NOT an ORM)
```

## Stack — latest-stable-then-pinned (D11)

Resolved to the newest stable at authoring (2026-07-13) and pinned into `package.json` + a real
`package-lock.json` for byte-identical installs.

| Layer            | Pinned version            |
|------------------|---------------------------|
| Node (image)     | **24 LTS** (`node:24-bookworm-slim`) |
| Next.js          | **16.2.10** (App Router)  |
| React / react-dom| **19.2.7**                |
| TypeScript       | **5.9.3**  (the `5.x` line C6/D11 froze — TS 7.x is out but the contract pins the 5.x line) |
| Tailwind CSS     | **4.3.2** (+ `@tailwindcss/postcss` 4.3.2, `tw-animate-css` 1.4.0) |
| shadcn/ui        | hand-vendored `new-york` set on Radix (`react-dialog` 1.1.19, `react-select` 2.3.3, `react-label` 2.1.11, `react-slot` 1.3.0), `lucide-react` 1.24.0, `sonner` 2.0.7 |
| Forms            | `react-hook-form` 7.81.0, `@hookform/resolvers` 5.4.0, `zod` 4.4.3 |

`node_modules` is **baked into the image** — there is **no per-session `npm install`**. Regenerate the
lockfile with `npm install --package-lock-only` in `template/` only when intentionally bumping versions.

## The three runtime env-vars (C6 / C9)

SESSION-API injects **exactly these three** at provision (and re-injects them on snapshot restore):

| Env-var               | Value                                                                 |
|-----------------------|-----------------------------------------------------------------------|
| `BIAL_APP_ID`         | `app_registry.id` — the appId, used as `…/apps/<BIAL_APP_ID>/records`  |
| `BIAL_APP_CREDENTIAL` | the app's `app_key` (`bial_…`), sent as `X-App-Key`                    |
| `BIAL_DATA_BASE_URL`  | the platform data-service base **including the `/v1` prefix**          |

**The `/v1` prefix is load-bearing.** `lib/bial-data.ts` builds the URL by raw concatenation —
`BIAL_DATA_BASE_URL + '/apps/' + BIAL_APP_ID + '/records'` — so `BIAL_DATA_BASE_URL` must be e.g.
`https://<platform-host>/v1` (no trailing slash), landing on the mounted route
`/v1/apps/{app_id}/records`.

**Why these exact names (D5):** the supervisor scrubs any child-env var ending in `_TOKEN`, `_SECRET`,
or `_KEY` (plus `SUPERVISOR_TOKEN`) before spawning `next dev`, so the untrusted app can never read the
supervisor's bearer token. None of the three names ends in a scrubbed suffix, so the app's own data
credential **survives the scrub** and reaches `next dev`. Renaming the credential to `*_KEY`/`*_SECRET`/
`*_TOKEN` would make it invisible and break every data call.

A fourth var, `BIAL_PORTAL_ORIGIN`, is read by the **Caddyfile** (the C8 `frame-ancestors` value) and by
the error-capture shim (the `postMessage` `targetOrigin`); it is not part of the data credential.

## Data access — one swappable module (D4)

`lib/bial-data.ts` is an **HTTP client to the existing platform data-service** (mirrors the wire shape of
`backend/src/services/appserving/assets/bial_data_client.js`), **not** Drizzle/Prisma/any ORM. Its method
surface is `save / list / query / distinct / get / update / remove` (+ `seedFromUpload`), and the response
envelopes are **asymmetric** exactly as the server returns them (`save` → bare record at 201; `get`/
`update` → `{record}`; `list` → `{records}`; `query` → `{items,total,page,pageSize,totalPages}`;
`distinct` → `{values}`; `remove` → `{ok:true}` at 200). Swapping to the LAST-stage per-app database later
means replacing **this one file** — nothing else in the tree touches data.

## Framing (C8)

The `Caddyfile` fronts one ingress port `:8080`: `/_sup/*` → supervisor (`127.0.0.1:9000`, fenced with
`frame-ancestors 'none'`), everything else → `next dev` (`127.0.0.1:3000`). The `next dev` block emits
`Content-Security-Policy: frame-ancestors {$BIAL_PORTAL_ORIGIN}` and **removes `X-Frame-Options`**, so
only the portal origin may frame the live app (XFO cannot express a cross-origin ancestor; CSP can). If
`BIAL_PORTAL_ORIGIN` is unset the ancestor-list is empty → **fail closed** (no origin may frame).

## Build / run locally — a DEV-LOOP CONVENIENCE, NOT the shipped-image verification

> **The authoritative image build is the Windows `az acr build` (ADR-0015 / D9).** The image is built and
> deployed from a **Windows VM** → ACR `bialgenaicr01` → Azure App Service / ACA. A local macOS/Linux
> `docker build` here is only a fast dev-loop check; **it does NOT verify the artifact that ships**, and a
> green local build says nothing about the Windows/CRLF path. That is exactly why the cross-platform
> guards below exist. Track SANDBOX's Azure acceptance test is the real gate.

Local smoke (from `sandbox/`):

```sh
# Build the base image (downloads deps + bakes node_modules — first build is slow).
docker build -f Dockerfile.sandbox -t bial-sandbox-test .

# Run it. SUPERVISOR_TOKEN is required (fail-fast). BIAL_PORTAL_ORIGIN enables the C8 frame header.
docker run --rm -p 8080:8080 \
  -e SUPERVISOR_TOKEN=dev-secret \
  -e BIAL_PORTAL_ORIGIN=http://localhost:5173 \
  --name bial-sandbox bial-sandbox-test

# In another shell: start next dev via the supervisor and watch it come up.
TOK=dev-secret
curl -s localhost:8080/_sup/health
curl -s -XPOST localhost:8080/_sup/dev/start -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -d '{"cmd":["npm","run","dev"]}'
curl -s localhost:8080/_sup/dev/status -H "Authorization: Bearer $TOK"   # {"running":true,"ready":true,...}

# Prove the C8 framing header is on the next dev response and XFO is absent:
curl -sI localhost:8080/ | grep -i 'content-security-policy\|x-frame-options'
#  → content-security-policy: frame-ancestors http://localhost:5173   (and NO x-frame-options)

# Prove the D5 scrub-survival: the three BIAL_* names reach next dev, SUPERVISOR_TOKEN does not.
curl -s -XPOST localhost:8080/_sup/exec -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -d '{"cmd":["printenv"]}' | grep -o 'BIAL_[A-Z_]*\|SUPERVISOR_TOKEN'
```

## Cross-platform rules (ADR-0015 — CRLF has burned BIAL twice)

- `*.sh`, `Dockerfile*`, `Caddyfile`, and the template are pinned to **LF** via the root `.gitattributes`
  (`sandbox/** text eol=lf`), so a Windows checkout does not ship a `#!/bin/sh\r` shebang.
- `Dockerfile.sandbox` runs `sed -i 's/\r$//'` on `entrypoint.sh` + `Caddyfile` **before** `chmod`, as a
  belt-and-braces guard for the Windows build host.

## Ownership boundary

Stage 0 authored this tree; it is **not** "known-good" until **Track SANDBOX** runs it in a real Azure
sandbox (its first acceptance test). Track SANDBOX owns all Wave-1 hardening here: PTY-backed `/exec`
(C1 reserves it — deliberately **not** added in Stage 0), snapshot/restore (C4), and the Azure CRUD
round-trip + framing acceptance.
