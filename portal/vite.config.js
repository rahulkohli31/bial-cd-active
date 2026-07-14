import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  plugins: [react()],
  base: '/',
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
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
      // The builder preview renderer is served by the FastAPI control-plane (with
      // its own relaxed CSP); proxy it so the live preview works in dev too.
      '/preview': {
        target: 'http://localhost:8000',
        changeOrigin: true,
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