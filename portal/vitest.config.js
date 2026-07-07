import { defineConfig } from 'vitest/config'

// Frontend-only: the React SPA's utils + component tests run under jsdom
// (localStorage / navigator.locks / BroadcastChannel are needed by auth.js).
// Automatic JSX runtime lets component tests render JSX without importing React
// in scope (matches vite.config.js). The former `server` project (Express/Cosmos
// unit tests) was removed with the Express backend — the control-plane is FastAPI.
export default defineConfig({
  esbuild: { jsx: 'automatic' },
  test: {
    name: 'frontend',
    environment: 'jsdom',
    include: ['src/**/*.test.{js,jsx}'],
  },
})
