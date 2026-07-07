/**
 * Client-side JSX → JS compile for the deploy-submit path (R19 client-compile shift).
 *
 * The FastAPI control-plane no longer runs Babel: `POST /v1/apps/{id}/submit` carries
 * the browser-produced compiled artifact, which the runner frame serves VERBATIM inside
 * its React-destructuring IIFE (`shells.render_frame`). This reproduces the EXACT
 * transform the live preview (`/preview`) uses — same import/export stripping, same
 * classic-runtime preset — so the JS the runner executes is byte-for-byte what the
 * builder previewed.
 *
 * `@babel/standalone` is loaded LAZILY (dynamic import → its own chunk) so its weight
 * only ever downloads when a user actually submits a build, never on first paint. The
 * transform is a pure string→string operation (it does not execute the code it
 * compiles), so it needs no `unsafe-eval` — it runs under the SPA's `script-src 'self'`.
 */

// Memoise the (heavy) standalone bundle across submits within a session.
let babelPromise = null
function loadBabel() {
  if (!babelPromise) babelPromise = import('@babel/standalone')
  return babelPromise
}

// Strip the module syntax a classic-runtime <script> can't run — React/Recharts are
// globals in both the preview and runner frames, so `import`/`export` lines are dead
// weight that would break the IIFE. Mirrors the /preview shell's cleaning exactly.
function stripModuleSyntax(code) {
  return String(code)
    .replace(/import\s+[^;]*?from\s*['"][^'"]+['"];?/g, '')
    .replace(/import\s*['"][^'"]+['"];?/g, '')
    .replace(/export\s+default\s+/g, '')
    .replace(/export\s+/g, '')
}

/**
 * Compile builder JSX into the runner's compiled artifact. Returns the transformed JS
 * string (defines `PreviewApp` in the frame's IIFE scope). Throws a user-ready Error on
 * empty input or a syntax error so the submit flow can surface it as a toast — never a
 * silent drop.
 */
export async function compileJsx(source) {
  const src = String(source ?? '').trim()
  if (!src) throw new Error('Nothing to submit — generate an app first.')
  const mod = await loadBabel()
  const Babel = mod.default ?? mod // dynamic-import namespace vs esbuild CJS-interop default
  try {
    const { code } = Babel.transform(stripModuleSyntax(src), {
      presets: [['react', { runtime: 'classic' }]],
    })
    if (!code || !code.trim()) throw new Error('the compiler produced no output')
    return code
  } catch (err) {
    throw new Error(`Could not compile your app for deployment: ${err?.message || err}`)
  }
}
