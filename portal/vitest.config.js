import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

// Frontend-only: the React SPA's utils + component tests run under jsdom
// (auth.js needs localStorage + navigator.locks; buildLock.ts needs BroadcastChannel).
// Automatic JSX runtime lets component tests render JSX without importing React
// in scope (matches vite.config.js). The former `server` project (Express/Cosmos
// unit tests) was removed with the Express backend — the control-plane is FastAPI.
// `.ts`/`.tsx` are first-class: Vite and Vitest compile them with no extra config,
// and new portal code is TypeScript (the 73 legacy `.js`/`.jsx` files stay as they are).
export default defineConfig({
  esbuild: { jsx: 'automatic' },
  resolve: {
    // Keep the `@/` alias in lockstep with vite.config.js / tsconfig.json (shadcn convention).
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    name: 'frontend',
    environment: 'jsdom',
    include: ['src/**/*.test.{js,jsx,ts,tsx}'],
  },
})
