// ESLint v9 flat config for the portal (U9/R9).
//
// `package.json` has shipped a `lint` script since before this file existed, but with no config
// on disk `eslint .` could not run AT ALL — it died on the v9 migration error, so "lint passes"
// has been vacuously true and the local gate has been running on three legs. This restores the
// fourth. The rule set mirrors what the code was already written against (React + hooks, the
// vibe-coded .jsx surface alongside the typed .ts/.tsx one), deliberately NOT a stricter bar:
// the point is to make the command runnable again, not to relitigate the codebase's style.
//
// Type-AWARE linting is deliberately off. It needs a tsconfig project per file, and the portal's
// .jsx majority isn't in one — `tsc --noEmit` already owns type truth here (and runs in the gate),
// so the duplicate would cost minutes for nothing.

import js from '@eslint/js'
import globals from 'globals'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    // Build output, deps, and coverage are not ours to lint. Playwright's report dir is
    // generated too. Anything ignored here is invisible to `eslint .` — keep it to artifacts.
    ignores: ['dist/**', 'node_modules/**', 'coverage/**', 'playwright-report/**', 'test-results/**'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{js,jsx,ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.es2021 },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    settings: { react: { version: 'detect' } },
    plugins: { react, 'react-hooks': reactHooks },
    rules: {
      ...react.configs.flat.recommended.rules,
      // The CLASSIC two hooks rules, listed explicitly rather than spreading this plugin's
      // `recommended-latest`. That preset now also turns on the React-Compiler-era rules
      // (refs / set-state-in-effect / immutability / preserve-manual-memoization), which flag
      // 67 findings across a codebase written years before that bar existed. Adopting them is
      // a real refactor with real regression risk — a separate, deliberate piece of work, not
      // a side effect of making `npm run lint` executable again (U9 defers the burn-down).
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      // The new JSX transform: no `import React` needed, and React is not a runtime global.
      'react/react-in-jsx-scope': 'off',
      'react/jsx-uses-react': 'off',
      // The .jsx surface is untyped by choice (ADR: portal stays JS-first outside the typed
      // utils/hooks); prop-types would be noise on every component in the tree.
      'react/prop-types': 'off',
      // Off by choice, not by fatigue. It fires on ordinary apostrophes in user-visible copy
      // ("you're", "app's") — which JSX renders correctly — and the fix is to bury readable
      // English under `&apos;`. Its genuine catch (a stray `>` or `}` escaping into markup) is
      // caught by review and by the rendered tests that assert on that copy.
      'react/no-unescaped-entities': 'off',
      // `_`-prefixed args are the repo's existing convention for a deliberate discard.
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      'no-unused-vars': 'off', // superseded by the TS-aware rule above; leaving both on double-reports
    },
  },
  {
    // Node-side config/tooling files: these run in Node, not the browser, so browser globals
    // (and only those) would be wrong here.
    files: ['*.config.{js,ts}', 'vite.config.*', 'playwright.config.*', 'postcss.config.*', 'tailwind.config.*'],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    // Tailwind's config is loaded by Tailwind's own CJS-era loader, where `require()` for a
    // plugin is the documented form — not a lapse to migrate away from.
    files: ['tailwind.config.js', 'postcss.config.js'],
    rules: { '@typescript-eslint/no-require-imports': 'off' },
  },
  {
    // Tests get the test-runner globals plus Node's — `npm test` runs them under vitest/jsdom.
    files: ['**/__tests__/**', '**/*.test.{js,jsx,ts,tsx}', '**/*.spec.{js,jsx,ts,tsx}', 'e2e/**'],
    languageOptions: { globals: { ...globals.node, ...globals.vitest } },
  },
)
