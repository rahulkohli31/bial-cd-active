#!/bin/sh
# Fail-first CSP guard (KTD-3). APPS_DOMAIN is a REQUIRED deploy input: it fills the portal's
# document CSP `frame-src https://*.${APPS_DOMAIN}` (C8 §2) so the cockpit may frame the
# per-session sandbox preview origin. Without it, the nginx envsubst that runs next
# (20-envsubst-on-templates.sh) would emit a MALFORMED `frame-src https://*.` — or leave a
# literal `${APPS_DOMAIN}` — which SILENTLY blocks the sandbox frame with no boot failure. That is
# exactly the env-difference-on-the-Windows-build class CLAUDE.local.md warns about, so refuse to
# start rather than serve a broken CSP. Runs before 20-envsubst (nginx sorts /docker-entrypoint.d/
# lexically) and before nginx starts; a non-zero exit aborts container startup.
set -e

if [ -z "${APPS_DOMAIN:-}" ]; then
  echo "FATAL: APPS_DOMAIN is unset or empty." >&2
  echo "       Set it to the ACA environment's apps domain (e.g." >&2
  echo "       <env-id>.<region>.azurecontainerapps.io) so the portal CSP frame-src can" >&2
  echo "       permit the per-session sandbox preview origin (C8 §2). Refusing to start." >&2
  exit 1
fi
