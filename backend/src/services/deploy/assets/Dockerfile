# syntax=docker/dockerfile:1
#
# PLATFORM-OWNED. Written into the build context by `services/deploy/context.py`; any
# Dockerfile in the citizen's workspace is EXCLUDED when the context is packed, never
# overwritten — so there is no ordering bug that could let an agent-authored one win.
#
# Two hardening choices below are load-bearing, because `package.json` is agent-editable
# and is therefore the last remaining arbitrary-code-execution vector in this build:
#
#   * `npm ci --ignore-scripts` — a `postinstall` hook would otherwise run whatever the
#     agent wrote, inside the build agent, with network access.
#   * `npx --no-install next build` rather than `npm run build` — the `build` script is
#     agent-editable and could be replaced with anything at all.
#
# LF line endings only: this file ships inside the backend image, which is built on a
# Windows VM. A CRLF here becomes a broken build on the host that produces the artifact.

# Debian 12 (bookworm) left regular security support on 12 July 2026, so the base moves to
# Debian 13 (trixie) — the same base the sandbox uses, deliberately: letting the environment
# citizens BUILD in diverge from the one their apps RUN in trades a CVE count for a class of
# works-here-fails-there defects.
#
# DIGEST-PINNED, and it is the INDEX digest, not a per-architecture child. The index resolves
# per host, so this same string builds arm64 locally and amd64 on the registry; pinning the
# amd64 child would build fine in ACR and break every local build. Resolved 2026-08-13 — see
# ops/CVE-REMEDIATION-ROLLBACK-ANCHOR.md §1.2, which is the record of what this moved from and
# how to reproduce it. Do not record a competing digest here.
#
# This default alone ships NOTHING: services/deploy/config.py's `node_base_image` becomes the
# NODE_IMAGE build arg on every platform build, so it always wins. This line is the hand-build
# fallback, and the two are pinned to the identical string by a test.
ARG NODE_IMAGE=node:24-trixie-slim@sha256:0711b541c1c33a8a530ac4f0d391baa9a15b3d804695b1b24a47daa5fb60e74d

# ── deps ─────────────────────────────────────────────────────────────────────
FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# A drifted lockfile fails HERE, loudly, with npm's own "not in sync" message — which is
# the honest outcome. The sandbox can hide that drift (its node_modules is baked into the
# image and already satisfies the import), so this is often the first place it surfaces.
RUN npm ci --ignore-scripts --no-audit --no-fund --loglevel=error

# ── build ────────────────────────────────────────────────────────────────────
FROM ${NODE_IMAGE} AS builder
WORKDIR /app
# The app's own address (`/a/pub-<28 hex>`) and the single hostname every generated app is
# served from. `images.py` sends both on every platform build.
#
# RE-DECLARED INSIDE THE STAGE, and that is the whole point: `ARG NODE_IMAGE` above sits before
# the first `FROM`, which scopes it to `FROM` lines only. Declared up there, these two would be
# accepted by the registry and expand to the empty string here — a build that looks fine and
# bakes root-relative URLs into an app the router can only reach under its key.
#
# NO DEFAULTS, deliberately. The next.config wrapper throws when either is empty, which is the
# only reason a missing value fails at build time instead of shipping. A default here would
# satisfy that check with a value that is correct for no app at all.
ARG BIAL_BASE_PATH
ARG BIAL_APPS_HOSTNAME
# Promoted to the environment because the next.config wrapper reads them from `process.env`
# when `next build` evaluates it below, and because an ENV is where a reader expects to find
# what the build actually consumed. The runtime stage sets neither: it is AFTER the build, so
# it could not influence the baked URLs, and the standalone output carries its own resolved
# copy of the config anyway.
ENV NEXT_TELEMETRY_DISABLED=1 \
    BIAL_BASE_PATH=${BIAL_BASE_PATH} \
    BIAL_APPS_HOSTNAME=${BIAL_APPS_HOSTNAME}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# No BIAL_DATABASE_URL is present at build time, by design: the app must not need a live
# database to compile. Code that reaches for one at module scope fails here rather than
# at 3am in production.
RUN npx --no-install next build
# Fail with one legible line instead of an opaque COPY error three instructions later if
# the standalone output is missing (the platform next.config wrapper forces it on, so this
# firing means the wrapper did not apply).
RUN test -d .next/standalone || (echo "next build produced no .next/standalone" && exit 1)
# The migrator runs at container start from outside the Next module graph, so tracing never
# saw its dependencies. This walks the REAL installed tree and copies their closure in —
# replacing a hand-written package list that shipped an image missing `xtend` and died on
# start. Runs here, in the builder, where the full node_modules still exists.
RUN node copy-runtime-deps.mjs

# ── runtime ──────────────────────────────────────────────────────────────────
FROM ${NODE_IMAGE} AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# The standalone bundle carries its own minimal node_modules, traced from the import graph.
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public
# Migrations are DATA plus a script that lives OUTSIDE the Next module graph, so tracing
# cannot see either. The platform next.config wrapper adds them via
# `outputFileTracingIncludes`; these copies are the belt to that braces.
COPY --from=builder --chown=node:node /app/drizzle ./drizzle
COPY --from=builder --chown=node:node /app/scripts/db-migrate.mjs ./scripts/db-migrate.mjs

# node:24-trixie-slim ships an unprivileged `node` user (uid 1000) — no useradd needed.
# Verified identical to bookworm's: `docker run --rm node:24-trixie-slim id node` → uid=1000(node).
USER node
EXPOSE 3000

# `--strict` is what makes this safe to run in production: the platform's copy of the
# migrator exits non-zero on any failure, so a half-applied schema fails the container
# START rather than serving a healthy-looking app against a broken database. `&&` (never
# `;`) so a failed migration can never reach `server.js`.
CMD ["sh", "-c", "node scripts/db-migrate.mjs --strict && exec node server.js"]
