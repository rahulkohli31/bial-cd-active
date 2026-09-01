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
    // jsdom implements neither observer, no `matchMedia`, none of the pointer-capture methods
    // and no `navigator.clipboard`, so the component libraries this portal renders cannot be
    // mounted at all without these. There was no `setupFiles` key here before — which is why
    // `scrollIntoView` ended up hand-stubbed in seventeen files.
    //
    // This single line has no compiler and no linter behind it. `src/__tests__/test-setup.test.tsx`
    // is its canary: it renders a real Radix component that stubs nothing, so losing this key
    // fails one obvious test instead of a dozen obscure ones three units away.
    setupFiles: ['./src/test-setup.ts'],
    // Vitest's default is 5s, and that is a WALL-CLOCK budget the whole suite competes for.
    // The heavy BuilderPage specs finish in ~300ms each when their file runs alone, but the
    // full 80-file run executes them in parallel, so on a loaded machine (or a small CI
    // runner) they get starved of CPU and cross 5s while doing nothing wrong. That produced
    // red runs naming a DIFFERENT set of tests each time and going green on re-run — a
    // timeout measuring the machine, not the code, which is the least useful kind of failure.
    //
    // Raised rather than papered over: nothing here hangs, and a genuine hang still fails,
    // just fifteen seconds later. If a test ever legitimately needs more than this, that is a
    // signal about the test, not a reason to raise the number again.
    testTimeout: 15_000,
    // Same reasoning for setup/teardown, which pay the jsdom environment cost.
    hookTimeout: 15_000,
  },
})
