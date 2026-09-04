# `sandbox/` — the empty Next.js starter + pre-baked base image

This is the **golden scaffold** every generated BIAL app is built from: a minimal, modern Next.js
starter plus the in-container **supervisor + Caddy + entrypoint** that run it, baked into one
Docker image. Authored in Stage 0 (unit U10); snapshot/restore and the real Azure acceptance test
have since shipped (see *Ownership boundary*), and PTY-backed `/exec` is still deferred.

**It is a STARTER, not a CRUD template.** `app/page.tsx` is a placeholder heading that says to
replace it, and `db/schema.ts` is `export {};` with an empty migration journal beside it — there is
deliberately no demonstration data model to work around or delete. Read "CRUD template" anywhere in
this file's history as the thing it stopped being.

The design goal is **a fast, predictable starting point**, not a fixed one. The container plumbing,
the error-capture shim and the pinned base ARE fixed, and that is what makes two sessions start
alike. What the agent may do inside is NOT: `run_command` is unrestricted in a Build chat (the
open-sandbox model, R8), it may `npm install` on demand, and the restore path reconciles whatever
lockfile the snapshot carries. This paragraph used to promise "the AI writes *feature code only* …
every build session starts byte-identical"; `Dockerfile.sandbox`'s own header retires that claim by
name.

## Layout

```
sandbox/
├── Dockerfile.sandbox     # node:24-trixie-slim base; bakes node_modules + the template
├── Caddyfile              # ingress :8080 → /_sup/* supervisor, /* → next dev (+ C8 framing)
├── entrypoint.sh          # PID 1: caddy + supervisor (root; children demoted to appuser)
├── supervisor/
│   └── app.py             # the C1 supervisor HTTP API (forked from the spike, long since grown)
└── template/              # the golden Next.js app (App Router + TS + Tailwind v4 + shadcn/ui)
    ├── package.json / package-lock.json   # latest-stable-then-pinned deps (see below)
    ├── app/               # layout.tsx · page.tsx
    ├── components/bial/error-capture.tsx  # window.onerror/unhandledrejection/console capture (C7)
    ├── components/ui/     # the shadcn/ui set (button, dialog, table, form, select, …) — editable
    ├── db/                 # schema.ts (Drizzle schema) · index.ts (server-only pooled client)
    ├── drizzle.config.ts / drizzle/  # drizzle-kit config + an EMPTY migration journal (the app
    │                                  # generates its own; nothing ships pre-migrated)
    ├── next.config.ts      # PLATFORM-OWNED — basePath, Server-Actions origins; do not edit
    ├── .env.example        # the seven injected vars, documented one per block
    ├── scripts/db-migrate.mjs        # non-fatal migrate-on-boot, run by `npm run dev`
    └── lib/bial-config.ts  # the injected-config type + the window.__BIAL_CONFIG declaration
```

## Stack — latest-stable-then-pinned (D11)

Resolved to the newest stable at authoring (2026-07-13) and pinned into `package.json` + a real
`package-lock.json` for byte-identical installs.

| Layer            | Pinned version            |
|------------------|---------------------------|
| Node (image)     | **24 LTS** (`node:24-trixie-slim`) — Debian 13; bookworm left security support 2026-07-12 |
| Next.js          | **16.3.1** (App Router)   |
| React / react-dom| **19.2.7**                |
| TypeScript       | **5.9.3**  (the `5.x` line C6/D11 froze — TS 7.x is out but the contract pins the 5.x line) |
| Tailwind CSS     | **4.3.2** (+ `@tailwindcss/postcss` 4.3.2, `tw-animate-css` 1.4.0) |
| shadcn/ui        | hand-vendored `new-york` set on Radix (`react-dialog` 1.1.19, `react-select` 2.3.3, `react-label` 2.1.11, `react-slot` 1.3.0), `lucide-react` 1.24.0, `sonner` 2.0.7 |
| Forms            | `react-hook-form` 7.81.0, `@hookform/resolvers` 5.4.0, `zod` 4.4.3 |

`node_modules` is **baked into the image** as a SPEED BASE, not a frozen set: a build agent may
`npm install` more at runtime and the restore path reconciles the snapshotted lockfile. (This line
read "there is **no per-session `npm install`**", which the open-sandbox pivot reversed —
`Dockerfile.sandbox`'s header says so in the same words.) Regenerate the lockfile with
`npm install --package-lock-only` in `template/` only when intentionally bumping versions.

## The injected runtime env-vars (C6 / C9)

SESSION-API injects **exactly these seven** at provision (and re-injects them on snapshot restore).
The list is `_INJECTED_ENV` in `supervisor/app.py`; `template/.env.example` documents the same seven
for a reader inside the app. This table carried only the first five for two releases:

| Env-var                   | Value                                                                  |
|---------------------------|------------------------------------------------------------------------|
| `BIAL_APP_ID`             | `app_registry.id` — the app's identity on the platform                 |
| `BIAL_PORTAL_ORIGIN`      | the portal origin: Caddy's `frame-ancestors`, the error shim's `targetOrigin` |
| `BIAL_BLOB_CONTAINER_URL` | the app's own per-app Blob container URL                               |
| `BIAL_BLOB_SAS`           | the container-scoped SAS (secret — server-only, redacted from output)  |
| `BIAL_DATABASE_URL`       | the project's own PostgreSQL connection string (secret, server-only)   |
| `BIAL_BASE_PATH`          | the path this app is served under, e.g. `/a/sbx-<28 hex>` — read by `next.config.ts` |
| `BIAL_APPS_HOSTNAME`      | the public hostname every generated app is served from (Server Actions origin) |

The last two are set by the control plane at the provision seam only
(`backend/src/services/sandbox/client.py`), never in `build_app_env` — a base path added there
would ship an `sbx-` value into a `pub-` published container.

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

> **Which build is authoritative depends on the subscription, and this file used to state only half
> of it.** For org/prod the answer is unchanged: the image is built and deployed from a **Windows
> VM** via `az acr build` (ADR-0015 / D9) → ACR `bialgenaicr01` → Azure App Service / ACA, so a green
> macOS/Linux `docker build` says nothing about the Windows/CRLF path and the cross-platform guards
> below are what stand between us and a repeat. **On the current free-trial subscription that path is
> unavailable** — ACR Tasks / `az acr build` are blocked — so the shipped artifact is produced by
> `docker buildx build --platform linux/amd64 --push` from macOS/Linux instead.
> `Dockerfile.sandbox`'s own header is the authority on this and says the same.
>
> Either way, a plain same-arch `docker build` below is a dev-loop convenience and not a verification
> of what ships; Track SANDBOX's Azure acceptance test is the real gate.

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

# Prove the D5 scrub-survival: the injected BIAL_* names reach next dev (seven when the platform
# sets them all; this local run sets one), SUPERVISOR_TOKEN does not.
curl -s -XPOST localhost:8080/_sup/exec -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' -d '{"cmd":["printenv"]}' | grep -o 'BIAL_[A-Z_]*\|SUPERVISOR_TOKEN'
```

## Cross-platform rules (ADR-0015 — CRLF has burned BIAL twice)

- `*.sh`, `Dockerfile*`, `Caddyfile`, and the template are pinned to **LF** via the root `.gitattributes`
  (`sandbox/** text eol=lf`), so a Windows checkout does not ship a `#!/bin/sh\r` shebang.
- `Dockerfile.sandbox` runs `sed -i 's/\r$//'` on `entrypoint.sh`, `Caddyfile`, `snapshot.sh` and
  `restore.sh` **before** `chmod`, as a belt-and-braces guard for the Windows build host. (The two
  scripts joined that list when `scripts/` was baked in; this line named only the first two.)

## Ownership boundary

Stage 0 authored this tree; **Track SANDBOX (Wave 1) has now proven it "known-good"** by running both
acceptance gates against the real pre-baked image (local Docker + Azurite, per ADR-0015 — for which
build host is authoritative today, see the note under *Build / run locally*):

- **Acceptance (b):** a local-disk → Azure-Blob snapshot/restore round-trip resumes the workspace,
  driven through a C2-ABC-conforming client (`tests/test_acceptance_snapshot_roundtrip.py`).
- The three supervisor guards + the full C1 surface + the C8 framing are pinned by regression tests.
- **PTY-backed `/exec` is DEFERRED** (no Phase-1 consumer) — C1 keeps it reserved; build it when a real
  TTY consumer + a matching C2 PTY method exist.

### Two argv refusals inside `/exec` (undocumented until now)

`/exec` is otherwise unrestricted in a Build chat — that is the open-sandbox model — but it carries
two narrow argv denylists, and a build agent that trips one gets a plain-language refusal rather
than an error. Both live in `supervisor/app.py` and are applied together at the top of `exec_cmd`;
both answer **200 with `exit: 1` and the reason on stderr**, never a 4xx, because the caller is a
model and an HTTP error is an opaque failure it cannot learn from.

- **`_refuse_a_manufactured_tty`** — refuses `script` / `expect` / `unbuffer`, and `pty.spawn` /
  `pty.fork` / `openpty` appearing in an INLINE program argument (`-c`, `-e`, `--eval`, …). Scoped
  to inline text on purpose: `grep -rn pty.spawn src/` is ordinary work and still runs. It exists
  because `stdin=DEVNULL` was not enough — an agent told to run a bare `npx drizzle-kit generate`
  manufactured its own terminal with `python3 -c "import pty; pty.spawn(...)"` and sat at the
  resulting prompt for 4m09s until the timeout fired. **This is the opposite of the deferred PTY
  feature above:** it refuses a pty, it does not provide one.
- **`_refuse_a_process_kill`** — refuses `kill` / `pkill` / `killall` / `fuser`, which an agent
  reaches for to "restart" the dev server the platform is already running.

The no-TTY posture they back up is three things together: `CI=1` in the child env, `stdin=DEVNULL`
on `/exec` and on `/dev/start`, and these two denylists.

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
