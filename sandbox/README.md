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
    ├── app/               # layout.tsx · page.tsx
    ├── components/bial/error-capture.tsx  # window.onerror/unhandledrejection/console capture (C7)
    ├── components/ui/     # the shadcn/ui set (button, dialog, table, form, select, …) — editable
    ├── db/                 # schema.ts (Drizzle schema) · index.ts (server-only pooled client)
    ├── drizzle.config.ts / drizzle/  # drizzle-kit config + the CHECKED-IN generated migrations
    ├── scripts/db-migrate.mjs        # non-fatal migrate-on-boot, run by `npm run dev`
    └── lib/bial-config.ts  # the injected-config type + the window.__BIAL_CONFIG declaration
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

## The injected runtime env-vars (C6 / C9)

SESSION-API injects **exactly these** at provision (and re-injects them on snapshot restore):

| Env-var                   | Value                                                                  |
|---------------------------|------------------------------------------------------------------------|
| `BIAL_APP_ID`             | `app_registry.id` — the app's identity on the platform                 |
| `BIAL_PORTAL_ORIGIN`      | the portal origin: Caddy's `frame-ancestors`, the error shim's `targetOrigin` |
| `BIAL_BLOB_CONTAINER_URL` | the app's own per-app Blob container URL                               |
| `BIAL_BLOB_SAS`           | the container-scoped SAS (secret — server-only, redacted from output)  |
| `BIAL_DATABASE_URL`       | the project's own PostgreSQL connection string (secret, server-only)   |

**Why these exact names (D5):** the supervisor's child-env scrub is a fail-closed **allowlist** —
the child env is built from an empty dict and copies only the names in `_INJECTED_ENV`. A var that
is not on that list never reaches `next dev`. Adding an injected var means adding a row to that
table in `supervisor/app.py`; there is no suffix rule to satisfy or avoid.

## Data access — Drizzle owns the app's schema (ADR-0028)

Each project owns a PostgreSQL database, injected as `BIAL_DATABASE_URL`. The app defines its
schema in `db/schema.ts`, generates versioned migrations into `drizzle/`, and queries through
`db/index.ts`. `npm run dev` applies pending migrations first, non-fatally.

The old shared platform data-service — its `lib/bial-data.ts` HTTP client, its per-request app-key
header, and the two env-vars that addressed it — is **deleted**. Nothing in the template, the build
prompt, or the supervisor references it.

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

Stage 0 authored this tree; **Track SANDBOX (Wave 1) has now proven it "known-good"** by running both
acceptance gates against the real pre-baked image (local Docker + Azurite, per ADR-0015 — the
definitive *artifact build* remains the Windows `az acr build`, a documented handoff):

- **Acceptance (b):** a local-disk → Azure-Blob snapshot/restore round-trip resumes the workspace,
  driven through a C2-ABC-conforming client (`tests/test_acceptance_snapshot_roundtrip.py`).
- The three supervisor guards + the full C1 surface + the C8 framing are pinned by regression tests.
- **PTY-backed `/exec` is DEFERRED** (no Phase-1 consumer) — C1 keeps it reserved; build it when a real
  TTY consumer + a matching C2 PTY method exist.

## Verifying this tree

The Python harness runs under the **backend `uv` env** (it subclasses the frozen C2 ABC + the
ObjectStorage port read-only via a `sys.path` bridge — no `backend/` file is edited). Run from `sandbox/`:

```sh
# Offline lane (no Docker): the fail-closed scrub, /files, auth, the 400-vs-422 split, and the
# git-bundle round-trip mechanics.
cd sandbox && uv run --project ../backend pytest

# Integration lane (Docker + Azurite): in-container guards + framing + the snapshot/restore round-trip.
docker compose -f ../backend/docker-compose.test.yml up -d          # Azurite on 127.0.0.1:10000
cd sandbox && uv run --project ../backend pytest -m integration     # builds the image once, or set
                                                                    # BIAL_SANDBOX_IMAGE to reuse a tag
```
