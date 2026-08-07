// PLATFORM-OWNED. `services/deploy/context.py` renames the app's own config to
// `next.config.app.ts` and writes this wrapper in its place.
//
// A wrapper rather than an overwrite: the citizen's app may legitimately have added
// `images`, `redirects`, `env`, `transpilePackages` and so on, and those must survive.
// What the platform takes back are the keys that decide whether the artifact is
// deployable at all.
//
// This is also WHY publishing needs no rebuild of the golden sandbox image: the build
// context is assembled server-side, so the platform can own the build configuration
// without touching `sandbox/template/next.config.ts` — the template keeps its dev-tuned
// config, and the published build gets these keys layered on top.

// `next.config.app` is written next to this file by the context builder. No suppression
// directive: the golden template's tsconfig sets `allowJs`, so this resolves whether the
// app's own config was `.ts`, `.js` or `.mjs`. An earlier `@ts-expect-error` here FAILED
// the build — with nothing to suppress, the directive is itself the error.
import appConfig from "./next.config.app";

export default {
  ...appConfig,

  // A containerised deploy needs the standalone server bundle. The golden template does
  // not set this (its job is `next dev`), and the agent can edit that file, so the
  // platform asserts it here rather than hoping.
  output: "standalone",

  // Non-negotiable. An agent that hits an unfixable type error WILL eventually discover
  // `ignoreBuildErrors: true`, and a build that ignores its own errors is not a gate.
  typescript: { ...(appConfig?.typescript ?? {}), ignoreBuildErrors: false },

  // Next traces the import graph to decide what enters `.next/standalone`. Two things it
  // cannot see, both required at container start:
  //   * `drizzle/**` — generated migration SQL. Data, imported by nothing.
  //   * `scripts/db-migrate.mjs` — run by the CMD, outside the module graph entirely.
  //
  // NODE_MODULES ARE DELIBERATELY NOT LISTED HERE. An earlier version named `pg` and each
  // of its dependencies by hand and shipped an image that died at start on
  // `Cannot find module 'xtend/mutable'` — because `outputFileTracingIncludes` copies the
  // files it is given and does NOT follow their dependencies, so every entry silently
  // promises a closure it cannot deliver. `copy-runtime-deps.mjs` walks the real installed
  // tree after the build instead. Do not reintroduce package globs here; they look like
  // they work right up until a lockfile resolves differently.
  outputFileTracingIncludes: {
    ...(appConfig?.outputFileTracingIncludes ?? {}),
    "/**": ["./drizzle/**", "./scripts/db-migrate.mjs"],
  },
};
