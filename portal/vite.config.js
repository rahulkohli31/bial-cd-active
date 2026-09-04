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
    // Disable vite's own dev-server CORS. EVERY REASON THIS LINE ORIGINALLY HAD IS NOW
    // FALSE, and it is kept on a different one — recorded here so the next reader does not
    // delete it as residue. It was added for the shared data plane: an opaque-origin
    // (Origin: null) preview iframe called /api/apps/:id/records, and vite's built-in CORS
    // middleware answered the preflight itself, without an Access-Control-Allow-Origin for
    // null, so the browser blocked it. Turning it off let the preflight proxy through to the
    // control-plane's null-reflecting branch. That branch is gone with that plane
    // (services/cors/middleware.py says so), the records routes with it, and a generated app
    // now reaches its own database rather than a shared endpoint.
    //
    // WHY IT STAYS. Vite's default is to answer cross-origin requests for any origin, so a
    // page on any site can read what this dev server serves while a developer has it running.
    // Off is the safe default and costs nothing: the SPA is same-origin with vite at :5173,
    // so no /api call preflights, and the one CORS layer that matters is the control-plane's,
    // which reflects FRONTEND_URL alone.
    cors: false,
    // Dev parity for the portal's document CSP (prod sets this via nginx envsubst; C8 §2, KTD-3).
    // A concrete, non-empty value so a dev-server load exercises the SAME framing constraint the
    // built SPA ships with — only framing is constrained (no default-src/script-src/connect-src),
    // so vite's HMR client, module graph, and the API proxy below are untouched.
    //
    // THIS IS THE FOURTH COPY OF THE FRAMING POLICY and the only one nginx does not emit, which is
    // exactly why it gets forgotten: it has no envsubst variable to follow, so it silently keeps
    // whatever host it was born with while the edge moves on. Left behind, a dev-server load
    // refuses to frame the preview once its address moves to the shared apps hostname, with
    // nothing but a console message and no server-side trace at all — the failure looks like a
    // broken preview, not like a stale config. The ACA wildcard that used to sit beside the apps
    // hostname is GONE: it covered the per-session sandbox FQDN the cockpit used to be handed,
    // and that address has moved. Do not re-add it — an internal Container Apps environment
    // publishes no public DNS, so nothing produces that origin any more. Literal rather than an
    // env var: the deployed value is a fixed BIAL name, and a dev-only knob with a fallback would
    // just be a second thing to forget. Pinned against nginx.conf by
    // src/__tests__/nginx-apps-routing.test.ts.
    //
    // CONSEQUENCE FOR THE LOCAL DEV LOOP, stated here because it is not obvious: a preview is now
    // addressed through the platform's router, so `npm run dev` alone cannot frame one. Running
    // the portal CONTAINER (which carries the apps vhost) is what makes a local preview work —
    // that is exactly what portal/tests/ stands up.
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