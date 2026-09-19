#!/bin/sh
# Sandbox entrypoint. Runs as root (PID 1) so the supervisor can drop children to `appuser`.
# The supervisor token stays in root's env; children get a scrubbed env (see supervisor/app.py).
# LF-only (the image is BUILT on the Windows VM via `az acr build`; a CRLF `#!/bin/sh\r` shebang
# makes the kernel exec `/bin/sh\r` → "no such file or directory").
set -eu

# NO OWNERSHIP RE-ASSERT AT BOOT, and the absence is deliberate. Dockerfile.sandbox bakes the
# template and installs node_modules as appuser, and anything that repopulates the workspace at
# runtime goes through /exec as appuser — so a `chown -R` here would walk the whole tree on the
# boot critical path and mask a build-time ownership bug rather than fix one.

# In-container reverse proxy (fronts the single ACA ingress port 8080).
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &

# Supervisor as root; listens loopback-only (Caddy is the only public face).
cd /supervisor
exec uvicorn app:app --host 127.0.0.1 --port 9000
