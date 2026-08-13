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
FROM python:3.14-slim

# RUNTIME git — not a build convenience. The control plane SHELLS OUT to git to restore an
# app's snapshot (`services/storage/snapshot_read.py` runs `git clone` over the bundle), and
# that path is on both the Plan-mode read and the whole publish pipeline
# (`services/deploy/service.py`). `python:3.14-slim` ships none — there is no `/usr/bin/git`
# and no `/usr/lib/git-core` — so publish was broken in any environment running this image.
#
# It must land on the PATH `_git_env` pins for the subprocess (`/usr/local/bin:/usr/bin:/bin`);
# Debian's package installs to /usr/bin/git, which satisfies that.
#
# ca-certificates is already present in the slim base, but is named explicitly so a future base
# change cannot drop it silently along with git.
#
# KNOWINGLY ACCEPTED: git carries its own CVE findings, so this ADDS to the scanner's count
# during a CVE remediation pass. That trade is deliberate — a live production defect outranks a
# count — and it is recorded as such in the remediation register rather than left to surprise
# the reviewer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
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
RUN uv sync --locked --no-group dev --group api --group worker

# Copy only the application package — narrows the image and avoids baking host
# artifacts/secrets in (ADR-0015: secrets are injected at runtime, never baked).
COPY src ./src

# Least privilege: run as an unprivileged user that owns /app.
RUN useradd --system --uid 10001 --home-dir /app app && chown -R app:app /app
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
