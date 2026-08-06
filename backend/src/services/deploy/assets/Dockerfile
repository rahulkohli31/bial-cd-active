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

ARG NODE_IMAGE=node:24-bookworm-slim

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
ENV NEXT_TELEMETRY_DISABLED=1
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

# node:24-bookworm-slim ships an unprivileged `node` user (uid 1000) — no useradd needed.
USER node
EXPOSE 3000

# `--strict` is what makes this safe to run in production: the platform's copy of the
# migrator exits non-zero on any failure, so a half-applied schema fails the container
# START rather than serving a healthy-looking app against a broken database. `&&` (never
# `;`) so a failed migration can never reach `server.js`.
CMD ["sh", "-c", "node scripts/db-migrate.mjs --strict && exec node server.js"]
