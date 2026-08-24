import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
export default defineConfig({
  plugins: [react()],
  base: '/',
  resolve: {
    // shadcn/ui convention (components.json): `@/` is the src root. Mirrored in
    // tsconfig.json `paths` and vitest.config.js so all three resolvers agree.
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    rollupOptions: {
      output: {
        // The assistant-ui/Streamdown migration (A2) added ~137 kB gzip to the entry chunk
        // (measured: 264 kB → 401 kB gzip, +52%) with zero code splitting — every visitor
        // downloads the markdown renderer + composer chrome before first paint even loads.
        // Splitting them into their own vendor chunk doesn't shrink the total, but lets the
        // browser fetch it in parallel with the entry chunk and cache it independently of
        // app-code churn, instead of it inflating the one chunk everything blocks on.
        manualChunks: {
          markdown: ['streamdown', 'remark-gfm', 'remark-breaks'],
          'assistant-ui': ['@assistant-ui/react'],
        },
      },
    },
  },
  server: {
    // Disable vite's own dev-server CORS. The builder live-preview runs in a
    // sandboxed, opaque-origin iframe (Origin: null) and calls the Data Service at
    // /api/apps/:id/records cross-origin. Vite 6's built-in CORS middleware answers
    // the OPTIONS preflight ITSELF — without an Access-Control-Allow-Origin for the
    // null origin — so the browser blocks the request ("Failed to fetch"). Turning
    // it off lets the preflight proxy through to the FastAPI control-plane, whose
    // cors.py reflects Origin: null on ^/v1/apps/.../(records|files|parse) — the
    // path the /api→/v1 rewrite below produces (matching production, no vite).
    cors: false,
    // Dev parity for the portal's document CSP (prod sets this via nginx envsubst; C8 §2, KTD-3).
    // A concrete, non-empty value so a dev-server load exercises the SAME framing constraint the
    // built SPA ships with — only framing is constrained (no default-src/script-src/connect-src),
    // so vite's HMR client, module graph, and the API proxy below are untouched. The wildcard
    // covers any per-session ACA sandbox FQDN the cockpit frames.
    headers: {
      'Content-Security-Policy': "frame-src 'self' https://*.azurecontainerapps.io; frame-ancestors 'self'",
    },
    proxy: {
      // Entra ID auth is served by the FastAPI control-plane (:8000), NOT Express.
      // This MUST precede the catch-all '/api' so the more-specific prefix wins.
      // The production edge strips /api before FastAPI (which serves /v1/auth/*),
      // so mirror that here with rewrite — the browser-visible path stays
      // /api/v1/auth/* dev↔prod, keeping the refresh cookie's Path and the OIDC
      // redirect_uri consistent (KD-8).
      '/api/v1/auth': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
      // Proxy the rest of the API surface to the FastAPI control-plane (:8000),
      // which serves everything under /v1. The production edge strips /api before
      // FastAPI, so mirror that here by rewriting the leading /api to /v1 — the
      // browser keeps calling /api/* dev↔prod while the backend sees /v1/*.
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, '/v1'),
      },
      // The deployed-app runner. `/apps/:appId` is deliberately NOT an SPA route
      // (App.jsx) — in production nginx sends it to the control-plane. Without this,
      // dev serves index.html for it, React Router matches nothing, and the shareable
      // app URL bounces to /login. Note the ordering: the more specific '/api' rule
      // above already claimed /api/apps/*, so this only catches the runner paths.
      '/apps': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})