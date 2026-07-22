#!/bin/sh
# Fail-first guard for REQUIRED deploy inputs (KTD-3). Without these, the nginx envsubst that
# runs next (20-envsubst-on-templates.sh) would emit a silently-broken config:
#   - APPS_DOMAIN fills the portal's document CSP `frame-src https://*.${APPS_DOMAIN}` (C8 §2)
#     so the cockpit may frame the per-session sandbox preview origin. Missing -> a MALFORMED
#     `frame-src https://*.` that silently blocks the sandbox frame with no boot failure.
#   - DNS_RESOLVER feeds `resolver ${DNS_RESOLVER}` so nginx re-resolves the backend hostname
#     at request time (App Service private endpoints; a boot-time-pinned IP caused the
#     "Web App - Unavailable" 403 loop). Use 168.63.129.16 on Azure, 127.0.0.11 in local Docker.
#     Missing -> a literal `${DNS_RESOLVER}` in the config, which nginx rejects with a far less
#     actionable parse error.
# That is exactly the env-difference-on-the-Windows-build class CLAUDE.local.md warns about, so
# refuse to start rather than serve a broken config. Runs before 20-envsubst (nginx sorts
# /docker-entrypoint.d/ lexically) and before nginx starts; a non-zero exit aborts startup.
set -e

if [ -z "${APPS_DOMAIN:-}" ]; then
  echo "FATAL: APPS_DOMAIN is unset or empty." >&2
  echo "       Set it to the ACA environment's apps domain (e.g." >&2
  echo "       <env-id>.<region>.azurecontainerapps.io) so the portal CSP frame-src can" >&2
  echo "       permit the per-session sandbox preview origin (C8 §2). Refusing to start." >&2
  exit 1
fi

if [ -z "${DNS_RESOLVER:-}" ]; then
  echo "FATAL: DNS_RESOLVER is unset or empty." >&2
  echo "       nginx needs it to re-resolve BACKEND_URL at request time. Set it to" >&2
  echo "       168.63.129.16 on Azure App Service (VNet/private-endpoint DNS) or" >&2
  echo "       127.0.0.11 under local Docker. Refusing to start." >&2
  exit 1
fi

# BACKEND_URL must be scheme://host[:port] with NO path and NO trailing slash. With a
# variable proxy_pass (request-time DNS), nginx treats any URI part on the upstream as a
# replacement URI — a trailing slash silently collapses EVERY proxied request to "/",
# a total routing outage with nginx still "up". Reject at boot instead.
case "${BACKEND_URL:-}" in
  "")
    echo "FATAL: BACKEND_URL is unset or empty. Refusing to start." >&2
    exit 1
    ;;
  *://*/*)
    echo "FATAL: BACKEND_URL must have no path or trailing slash (got: ${BACKEND_URL})." >&2
    echo "       Use scheme://host[:port], e.g. https://<backend-app>.azurewebsites.net —" >&2
    echo "       with request-time proxy_pass, any URI part would silently replace every" >&2
    echo "       proxied request's path with '/'. Refusing to start." >&2
    exit 1
    ;;
esac
