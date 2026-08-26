#!/bin/sh
# Fail-first guard for REQUIRED deploy inputs (KTD-3). Without these, the nginx envsubst that
# runs next (20-envsubst-on-templates.sh) would emit a silently-broken config — and "silently"
# is the operative word: every failure below leaves `nginx -t` green and the container serving.
#
#   - APPS_DOMAIN composes the ROUTER'S UPSTREAM. `sbx-<key>.${APPS_DOMAIN}` /
#     `pub-<key>.${APPS_DOMAIN}` is the container app's real FQDN, so a wrong value here is not
#     a cosmetic error: EVERY generated app becomes unreachable while the portal itself looks
#     perfectly healthy. This used to be a CSP-only input, which is why its old failure text
#     talked about frame-src; that is no longer its main job. It is the Container Apps
#     environment's OWN default domain (`<env-id>.<region>.azurecontainerapps.io`) and it must
#     NOT be changed to the public apps hostname — those are different names for different hops,
#     and swapping them produces an upstream that does not resolve.
#   - APPS_HOSTNAME is the PUBLIC name a BIAL employee's browser uses, and the `server_name` of
#     the apps site. Missing -> the apps `server` block gets an empty server_name and nginx
#     refuses to load; malformed -> the block never matches the Host the gateway forwards, so
#     every app request falls through to the portal site and gets the SPA's index.html back
#     instead of the app. That failure looks like "the app renders the portal", not like a
#     routing error.
#   - PORTAL_ORIGIN is the link target on the apps site's 404 page — the way back for an
#     employee who followed a stale link. Missing -> envsubst emits `href=""`, a dead button.
#   - DNS_RESOLVER feeds `resolver ${DNS_RESOLVER}` so nginx re-resolves upstreams at request
#     time (App Service private endpoints; a boot-time-pinned IP caused the "Web App -
#     Unavailable" 403 loop). It is now load-bearing for BOTH sites: without it the apps site's
#     variable proxy_pass fails at request time and every app request is a 502. Use
#     168.63.129.16 on Azure, 127.0.0.11 in local Docker. Missing -> a literal `${DNS_RESOLVER}`
#     in the config, which nginx rejects with a far less actionable parse error.
#
# That is exactly the env-difference-on-the-Windows-build class CLAUDE.local.md warns about, so
# refuse to start rather than serve a broken config. Runs before 20-envsubst (nginx sorts
# /docker-entrypoint.d/ lexically) and before nginx starts; a non-zero exit aborts startup.
#
# PRESENCE IS NOT THE BAR for the three name-shaped inputs. A value carrying a scheme, a `*.`
# wildcard prefix, a path or a trailing slash produces a config that is WRONG rather than
# BROKEN, and wrong survives every automated check. Reject the shape, not merely the absence.
set -e

# POSIX sh, no bashisms, no `local` — this file is checked out and built on the Windows VM and
# runs under BusyBox ash in nginx:alpine-slim.

if [ -z "${APPS_DOMAIN:-}" ]; then
  echo "FATAL: APPS_DOMAIN is unset or empty." >&2
  echo "       Set it to the Container Apps environment's OWN default domain, e.g." >&2
  echo "       <env-id>.<region>.azurecontainerapps.io — it composes the apps router's" >&2
  echo "       upstream (sbx-<key>.\$APPS_DOMAIN). It is NOT the public apps hostname." >&2
  echo "       Refusing to start." >&2
  exit 1
fi

case "${APPS_DOMAIN}" in
  *://*|*/*|\**|.*|*.)
    echo "FATAL: APPS_DOMAIN must be a bare domain (got: ${APPS_DOMAIN})." >&2
    echo "       No scheme, no '*.' wildcard, no path or slash, no leading/trailing dot." >&2
    echo "       Good: blackbush-e9745742.centralindia.azurecontainerapps.io" >&2
    echo "       It is concatenated as sbx-<key>.\$APPS_DOMAIN to reach the app container;" >&2
    echo "       anything else silently produces an upstream that never resolves, and every" >&2
    echo "       generated app 404s while the portal stays healthy. Refusing to start." >&2
    exit 1
    ;;
esac

if [ -z "${APPS_HOSTNAME:-}" ]; then
  echo "FATAL: APPS_HOSTNAME is unset or empty." >&2
  echo "       Set it to the PUBLIC hostname generated apps are served on, e.g." >&2
  echo "       citizenapps.bialairport.com — it is the apps site's server_name and the" >&2
  echo "       origin the portal is permitted to frame. Refusing to start." >&2
  exit 1
fi

case "${APPS_HOSTNAME}" in
  *://*|*/*|\**|.*|*.|*:*)
    echo "FATAL: APPS_HOSTNAME must be a bare hostname (got: ${APPS_HOSTNAME})." >&2
    echo "       No scheme, no '*.' wildcard, no port, no path or slash." >&2
    echo "       Good: citizenapps.bialairport.com" >&2
    echo "       A malformed value still loads: the apps server block simply never matches" >&2
    echo "       the Host the gateway forwards, so every app request is served the portal's" >&2
    echo "       index.html instead of the app. Refusing to start." >&2
    exit 1
    ;;
esac

if [ -z "${PORTAL_ORIGIN:-}" ]; then
  echo "FATAL: PORTAL_ORIGIN is unset or empty." >&2
  echo "       Set it to the portal's own public origin, e.g." >&2
  echo "       https://blrcitizen.bialairport.com — it is the link back to the portal on the" >&2
  echo "       apps site's 404 page. Refusing to start." >&2
  exit 1
fi

case "${PORTAL_ORIGIN}" in
  https://*/|http://*/)
    echo "FATAL: PORTAL_ORIGIN must have no trailing slash (got: ${PORTAL_ORIGIN})." >&2
    echo "       Use scheme://host, e.g. https://blrcitizen.bialairport.com." >&2
    echo "       Refusing to start." >&2
    exit 1
    ;;
  https://*|http://*)
    case "${PORTAL_ORIGIN#*://}" in
      */*)
        echo "FATAL: PORTAL_ORIGIN must have no path (got: ${PORTAL_ORIGIN})." >&2
        echo "       Use scheme://host, e.g. https://blrcitizen.bialairport.com." >&2
        echo "       Refusing to start." >&2
        exit 1
        ;;
    esac
    ;;
  *)
    echo "FATAL: PORTAL_ORIGIN must start with https:// or http:// (got: ${PORTAL_ORIGIN})." >&2
    echo "       It is emitted as an href on the apps 404 page; a bare hostname would be" >&2
    echo "       resolved relative to the APPS hostname and link back to itself." >&2
    echo "       Refusing to start." >&2
    exit 1
    ;;
esac

if [ -z "${DNS_RESOLVER:-}" ]; then
  echo "FATAL: DNS_RESOLVER is unset or empty." >&2
  echo "       nginx needs it to re-resolve BACKEND_URL and every app upstream at request" >&2
  echo "       time. Set it to 168.63.129.16 on Azure App Service (VNet/private-endpoint DNS)" >&2
  echo "       or 127.0.0.11 under local Docker. Refusing to start." >&2
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
