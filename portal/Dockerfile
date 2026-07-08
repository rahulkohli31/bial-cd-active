# FRONTEND image of the split: the React/Vite portal SPA, built with Node and served
# by nginx. Node is used ONLY at BUILD time; the runtime is nginx (no Node, no SSR).
# nginx also proxies the API surface to the backend, mirroring the Vite dev proxy:
# /api/v1/auth strips /api; other /api/* rewrites /api->/v1; /apps and /preview pass
# through. So the SPA keeps calling /api/* unchanged in dev and prod.
#
# Build context = portal/ :
#   az acr build --image bial-frontend:latest --file portal/Dockerfile portal
#   (or:  docker build -t bial-frontend portal/ )
#
# Runtime env:
#   PORT=8080                        # nginx listen port
#   BACKEND_URL=http://backend:8000  # the FastAPI backend (see backend/Dockerfile)

# ── Stage 1: build the SPA (Node at BUILD time only) ─────────────────────────
FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . ./
RUN npm run build          # → /app/dist (index.html, assets/, static root files)

# ── Stage 2: serve with nginx (+ API proxy to the backend) ───────────────────
FROM nginx:1.27-alpine
# nginx:alpine runs envsubst over /etc/nginx/templates/*.template at start (writing
# the result into /etc/nginx/conf.d/), so ${PORT} and ${BACKEND_URL} are substituted
# from the environment while nginx's own $host/$uri variables are left untouched.
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
ENV PORT=8080 \
    BACKEND_URL=http://backend:8000
EXPOSE 8080
# The base image's entrypoint does the envsubst + starts nginx in the foreground.
