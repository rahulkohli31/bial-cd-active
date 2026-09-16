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
          // Same reasoning one dependency later: the animation runtime is imported by the shell,
          // so it would otherwise land in the chunk first paint blocks on.
          motion: ['motion/react'],
        },
      },
    },
  },
  server: {
    // Disable vite's own dev-server CORS. The reason this was ADDED (a null-origin preview
    // iframe hitting a since-removed shared-data-plane endpoint) no longer applies — kept
    // instead because vite's default answers cross-origin requests from ANY origin, so any
    // site could read what this dev server serves while it's running. Off is the safe
    // default and costs nothing: the SPA is same-origin with vite at :5173, so nothing here
    // actually preflights.
    cors: false,
    // Dev parity for the portal's document CSP (prod sets this via nginx envsubst); only
    // framing is constrained, so HMR, the module graph, and the API proxy are untouched.
    //
    // THE FOURTH COPY OF THE FRAMING POLICY — and the one nginx does NOT emit. No envsubst
    // variable means it silently drifts if the apps hostname ever moves: a stale value fails
    // with a console message only, no server-side trace, looking like a broken preview rather
    // than stale config. Literal, not an env var (this value is a fixed BIAL name). The old
    // ACA wildcard is GONE — an internal Container Apps env publishes no public DNS — do not
    // re-add it. Pinned against nginx.conf by src/__tests__/nginx-apps-routing.test.ts.
    //
    // A bare `npm run dev` cannot frame a preview; run the portal CONTAINER instead
    // (portal/tests/ does exactly that) — previews are addressed through the platform router.
    headers: {
      'Content-Security-Policy':
        "frame-src 'self' https://citizenapps.bialairport.com; frame-ancestors 'self'",
    },
    proxy: {
      // Entra ID auth is served by the FastAPI control-plane (:8000), NOT Express.
      // This MUST precede the catch-all '/api' so the more-specific prefix wins.
      // The production edge strips /api before FastAPI (which serves /v1/auth/*),
      // so mirror that here with rewrite — the browser-visible path stays
      // /api/v1/auth/* dev↔prod, keeping the refresh cookie's Path and the OIDC
      // redirect_uri consistent.
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
      // (App.tsx) — in production nginx sends it to the control-plane. Without this,
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