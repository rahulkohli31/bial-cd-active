#!/bin/sh
# Sandbox entrypoint. Runs as root (PID 1) so the supervisor can drop children to `appuser`.
# The supervisor token stays in root's env; children get a scrubbed env (see supervisor/app.py).
# LF-only per ADR-0015 (the image is BUILT on the Windows VM via `az acr build`; a CRLF
# `#!/bin/sh\r` shebang makes the kernel exec `/bin/sh\r` → "no such file or directory").
set -eu

WORKSPACE="${WORKSPACE:-/workspace/app}"

# The golden template + its node_modules are baked into the image at build time and already
# owned by appuser. This is a best-effort re-assert for the case where a snapshot/restore
# (Track SANDBOX, Wave 1) repopulates the workspace on local disk at runtime — keep it
# writable by the unprivileged app user that runs `next dev`.
chown -R appuser:appuser "$WORKSPACE" 2>/dev/null || true
chmod -R u+rwX "$WORKSPACE" 2>/dev/null || true

# In-container reverse proxy (fronts the single ACA ingress port 8080).
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &

# Supervisor as root; listens loopback-only (Caddy is the only public face).
cd /supervisor
exec uvicorn app:app --host 127.0.0.1 --port 9000
