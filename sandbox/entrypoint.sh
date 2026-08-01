#!/bin/sh
# Sandbox entrypoint. Runs as root (PID 1) so the supervisor can drop children to `appuser`.
# The supervisor token stays in root's env; children get a scrubbed env (see supervisor/app.py).
# LF-only per ADR-0015 (the image is BUILT on the Windows VM via `az acr build`; a CRLF
# `#!/bin/sh\r` shebang makes the kernel exec `/bin/sh\r` → "no such file or directory").
set -eu

# NOTE: this script deliberately no longer touches the workspace. It used to `chown -R` +
# `chmod -R u+rwX` it on every boot; ownership is now established IN THE IMAGE instead
# (Dockerfile.sandbox bakes the template and installs node_modules as appuser), so that re-assert
# was measured to change nothing — `docker diff` on a booted container reported 0 changes — while
# still walking ~20.8k entries on the boot critical path, the exact path this change set exists to
# shorten. Worse, it MASKED any build-time ownership bug, which made the in-container ownership
# tests self-fulfilling; without it they — and the image-level
# test_image_workspace_is_appuser_owned_before_the_entrypoint_runs — are honest.
# Anything that repopulates the workspace at runtime (the Track SANDBOX snapshot/restore path)
# already runs as appuser via /exec, so it writes appuser-owned files by construction.
# The supervisor reads WORKSPACE from the image ENV, not from this script (the old local
# assignment here was never exported, so it is gone with its only consumer).

# In-container reverse proxy (fronts the single ACA ingress port 8080).
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &

# Supervisor as root; listens loopback-only (Caddy is the only public face).
cd /supervisor
exec uvicorn app:app --host 127.0.0.1 --port 9000
