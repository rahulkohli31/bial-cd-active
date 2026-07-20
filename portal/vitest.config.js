import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { defineConfig } from 'vitest/config'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// Frontend-only: the React SPA's utils + component tests run under jsdom
// (auth.js needs localStorage + navigator.locks; buildLock.ts needs BroadcastChannel).
// Automatic JSX runtime lets component tests render JSX without importing React
// in scope (matches vite.config.js). The former `server` project (Express/Cosmos
// unit tests) was removed with the Express backend — the control-plane is FastAPI.
// `.ts`/`.tsx` are first-class: Vite and Vitest compile them with no extra config,
// and new portal code is TypeScript (the 73 legacy `.js`/`.jsx` files stay as they are).
export default defineConfig({
  esbuild: { jsx: 'automatic' },
  // Matches the `@` alias in vite.config.js — the assistant-ui components
  // (thread.jsx etc.) import via `@/components/...`.
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    name: 'frontend',
    environment: 'jsdom',
    include: ['src/**/*.test.{js,jsx,ts,tsx}'],
    setupFiles: ['./src/test/setup.js'],
    // react-shiki (a dep of the assistant-ui chat UI) has an internal chunk
    // with an unconditional `import './style.css'` side effect. Left as an
    // external Node dependency, Vitest loads it via Node's own ESM loader
    // (which errors on a bare .css import) instead of through Vite's
    // transform pipeline. Inlining forces it through Vite's pipeline, where
    // CSS imports are handled normally.
    server: {
      deps: {
        inline: [/react-shiki/],
      },
    },
  },
})
