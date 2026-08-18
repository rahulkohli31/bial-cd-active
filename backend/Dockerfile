# syntax=docker/dockerfile:1
# BACKEND image of the split: the FastAPI control-plane — API only (/v1, /apps,
# /preview). No SPA, no Node. The SPA is a SEPARATE image (portal/Dockerfile) whose
# nginx proxies /api,/apps,/preview here.
#
# Build context = backend/ :
#   az acr build --image bial-backend:latest --file backend/Dockerfile backend
#   (or:  docker build -t bial-backend backend/ )
#
# The production build runs on a Windows VM via `az acr build` (ADR-0015) — keep it
# LF-only and cross-platform (the repo .gitattributes pins Dockerfile* to eol=lf).
# Secrets (DATABASE_URL, AUTH__*, FOUNDRY__*, OBJECT_STORE__*, superadmin_emails, …)
# are injected at runtime, never baked in.
# Alpine 3.24 / musl. The backend's ONLY remaining lever: it is already on Debian 13, so there
# is no Debian move left to make — of its 129 incoming findings, 126 have no fixed version in
# the Debian feed at all and the other 3 are fixed only in a CPython pre-release, which the
# platform does not ship. Rebasing replaces the glibc/Debian package set outright rather than
# waiting on fixes that are not coming.
#
# INDEX digest, not the amd64 child (sha256:0ad7f98a…): the index resolves per host, so this
# one string builds arm64 on a developer machine and amd64 in the registry. Resolved
# 2026-08-13 — ops/CVE-REMEDIATION-ROLLBACK-ANCHOR.md §1.2 records what this moved from.
FROM python:3.14-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc

# RUNTIME git — not a build convenience. The control plane SHELLS OUT to git to restore an
# app's snapshot (`services/storage/snapshot_read.py` runs `git clone` over the bundle), and
# that path is on both the Plan-mode read and the whole publish pipeline
# (`services/deploy/service.py`). `python:3.14-slim` ships none — there is no `/usr/bin/git`
# and no `/usr/lib/git-core` — so publish was broken in any environment running this image.
#
# It must land on the PATH `_git_env` pins for the subprocess (`/usr/local/bin:/usr/bin:/bin`);
# Alpine's package installs to /usr/bin/git, which satisfies that — same as Debian's did.
#
# ca-certificates is already present in the base, so it installs nothing — it is named anyway so
# a future base change cannot drop it silently along with git. `--no-cache` is what makes the
# separate index-cleanup step unnecessary.
#
# KNOWINGLY ACCEPTED: git carries its own CVE findings, so this ADDS to the scanner's count
# during a CVE remediation pass. That trade is deliberate — a live production defect outranks a
# count — and it is recorded as such in the remediation register rather than left to surprise
# the reviewer.
RUN apk add --no-cache git ca-certificates

# Pinned by tag AND index digest: the tag says which version, the digest fixes the bytes. The
# floating `:latest` it replaces meant two builds on different days were not the same build.
# The uv binary is a musl build and runs natively here.
COPY --from=ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc /uv /bin/uv
WORKDIR /app

# Install runtime deps for BOTH roles (api + worker); the dev group (gates + tests) and
# migrations are run out-of-band, never from this image.
#
# `--group worker` is not optional. ONE image serves both run targets, selected by start
# command (ADR-0015 as amended): `fastapi run src/main.py` on App Service, and
# `python -m src.worker_main` on an ingress-less Container App. Omit the group and the image
# ships no broker, so the worker container dies at start — AFTER the human-gated Azure
# provisioning is already done, which is the worst possible moment to discover it.
COPY pyproject.toml uv.lock ./
# `--no-build` is the standing musl guard, not decoration. Every one of the 29 compiled
# dependencies currently resolves to a musllinux wheel; the day one does not, this line fails
# the BUILD instead of silently compiling from source and shipping a fatter image with a
# toolchain-shaped hole in it. Read a failure here as "this dependency has no musl wheel".
#
# Never pipe this RUN through tail/tee — a pipe returns the LAST command's status and would
# swallow exactly the failure the flag exists to raise.
RUN uv sync --locked --no-build --no-group dev --group api --group worker

# Copy only the application package — narrows the image and avoids baking host
# artifacts/secrets in (ADR-0015: secrets are injected at runtime, never baked).
COPY src ./src

# Least privilege: run as an unprivileged user that owns /app.
# `useradd` does not exist on Alpine. The `addgroup` line is NOT optional and its absence is the
# single easiest way to break this build: busybox `adduser -S` with no `-G` drops the user into
# `nogroup` and creates no `app` group at all, so the CHAINED `chown app:app` below dies with
# "unknown user/group" — one clause away from the actual cause.
#
# `-s /bin/sh` keeps Debian's login shell rather than Alpine's nologin default, so the rebase
# stays a base change and not an incidental behaviour change. `-H` stops busybox populating
# /app from /etc/skel (WORKDIR already created it). The GID moves 999 -> 10001 to match the
# uid; nothing in this repo or its deploy config references the numeric group.
RUN addgroup -S -g 10001 app \
    && adduser -S -u 10001 -G app -h /app -H -s /bin/sh -D app \
    && chown -R app:app /app
USER app

# The venv is already correct; never re-sync (and thus re-install dev tooling)
# on container boot.
ENV UV_NO_SYNC=1

EXPOSE 8000

# Health: FastAPI's unauthenticated /v1/health (probes Postgres, fails closed 503).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8000')+'/v1/health', timeout=4).status==200 else 1)"]

# Bind the platform-provided $PORT (App Service / ACA inject it), default 8000.
CMD ["sh", "-c", "exec uv run fastapi run src/main.py --host 0.0.0.0 --port ${PORT:-8000}"]
