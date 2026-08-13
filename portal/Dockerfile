# FRONTEND image of the split: the React/Vite portal SPA, built with Node and served
# by nginx. Node is used ONLY at BUILD time; the runtime is nginx (no Node, no SSR).
# nginx also proxies the API surface to the backend, mirroring the Vite dev proxy:
# /api/v1/auth strips /api; other /api/* rewrites /api->/v1; /apps pass
# through. So the SPA keeps calling /api/* unchanged in dev and prod.
#
# Build context = portal/ :
#   az acr build --image bial-frontend:latest --file portal/Dockerfile portal
#   (or:  docker build -t bial-frontend portal/ )
#
# Runtime env:
#   PORT=8080                        # nginx listen port
#   BACKEND_URL=http://backend:8000  # the FastAPI backend (see backend/Dockerfile)
#   DNS_RESOLVER (REQUIRED)          # request-time DNS for the proxy: 168.63.129.16 on
#                                    # Azure App Service, 127.0.0.11 under local Docker
#   APPS_DOMAIN (REQUIRED)           # CSP frame-src apps domain (C8 §2)

# ── Stage 1: build the SPA (Node at BUILD time only) ─────────────────────────
FROM node:24-trixie-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . ./
RUN npm run build          # → /app/dist (index.html, assets/, static root files)

# ── Stage 2: serve with nginx (+ API proxy to the backend) ───────────────────
FROM nginx:alpine-slim
# THE MAINLINE TAG, NOT THE MINOR-PINNED ONE. `nginx:1.27-alpine` is how this base went 16
# months stale (its manifest was built 2025-04-16): a minor-pinned tag stops moving the moment
# upstream's attention does. `nginx:alpine-slim` tracks mainline, so the next bump arrives by
# rebuild rather than by somebody noticing.
#
# `-slim` over plain `alpine` is a REMOVAL, which is the strongest remediation available — a
# package that is not in the image cannot carry a finding at any version. It drops the image,
# font and XML libraries (libpng, tiff, libxpm, freetype, libwebp, libavif, libgd, libxml2,
# libxslt, …) plus the four dynamic nginx modules that use them. Verified against this config:
# nginx.conf names no `load_module`, njs, image_filter, xslt or geoip directive, so none of it
# is reachable from here.
#
# What -slim KEEPS, checked rather than assumed: `envsubst` at /usr/bin/envsubst, the complete
# /docker-entrypoint.d/ chain in the same order (so the 19- guard below still sorts ahead of
# 20-envsubst-on-templates.sh), byte-identical `nginx -V` configure flags, and the CA trust
# bundle (`ca-certificates-bundle` + libssl3/libcrypto3 remain; /etc/ssl/certs/ca-certificates.crt
# is intact). It drops the `curl` and `openssl` CLIs — neither is used by the entrypoint guard
# or by nginx at runtime.
#
# nginx:alpine-slim runs envsubst over /etc/nginx/templates/*.template at start (writing
# the result into /etc/nginx/conf.d/), so ${PORT}, ${BACKEND_URL}, ${DNS_RESOLVER} and
# ${APPS_DOMAIN} (the CSP frame-src apps domain, C8 §2) are substituted from the environment
# while nginx's own $host/$uri variables are left untouched.
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

# Fail-first guard (KTD-3): APPS_DOMAIN and DNS_RESOLVER are REQUIRED deploy inputs with no
# sensible defaults — this runs at start, before nginx's envsubst, and exits non-zero if
# either is unset/empty (malformed CSP / dead request-time DNS otherwise). It is a shell
# artifact BUILT ON THE WINDOWS VM, so strip any CRLF and make it executable — the exact
# `#!/bin/sh\r` → "no such file or directory" trap CLAUDE.local.md warns about.
COPY docker-entrypoint.d/19-require-apps-domain.sh /docker-entrypoint.d/19-require-apps-domain.sh
# The template gets the same CR strip. It is DEFENCE IN DEPTH, not a fix for a live defect —
# stated precisely because the obvious-sounding claim is false and a reviewer will test it: a
# fully-CRLF nginx.conf was built and served, and its response headers came back BYTE-IDENTICAL
# to the LF build. Every quoted value in this file closes before its line ends, so each CR lands
# after the `;` where nginx treats it as whitespace.
#
# What it guards is the FUTURE directive whose quoted value spans a line break. There, a stray CR
# makes nginx drop the whole header SILENTLY — no error, no warning, just a security header that
# stops arriving. Paired with the `portal/*.conf` rule in the root .gitattributes, which fixes the
# source; this fixes the build.
RUN sed -i 's/\r$//' /etc/nginx/templates/default.conf.template \
    && sed -i 's/\r$//' /docker-entrypoint.d/19-require-apps-domain.sh \
    && chmod +x /docker-entrypoint.d/19-require-apps-domain.sh

# NOTE: APPS_DOMAIN and DNS_RESOLVER have NO ENV defaults here (fail-first) — both are
# deployment-specific (a Docker-correct DNS_RESOLVER default would be silently wrong on App
# Service) and the guard above enforces their presence. PORT/BACKEND_URL keep their
# container-network defaults.
ENV PORT=8080 \
    BACKEND_URL=http://backend:8000
EXPOSE 8080
# The base image's entrypoint runs /docker-entrypoint.d/* (the guard, then envsubst) and
# starts nginx in the foreground.
