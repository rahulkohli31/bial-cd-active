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

// The two things the platform must know at BUILD time. `services/deploy/images.py` sends
// both as registry build arguments and the platform Dockerfile promotes them to the
// environment of `next build`, which is the only channel available: `next build` BAKES the
// base path into every generated link, asset URL and route, so a container environment
// variable would arrive after the whole output was already written — and a file cannot carry
// them either, because the context builder strips `.env*` when it packs and the shipped
// `.dockerignore` strips them again inside Docker.
const basePath = process.env.BIAL_BASE_PATH ?? "";
const appsHostname = process.env.BIAL_APPS_HOSTNAME ?? "";

// FAIL THE BUILD RATHER THAN SHIP A DEAD APP. Neither value has a fallback anywhere — not
// here, and deliberately not as an ARG default in the Dockerfile — because every wrong value
// is wrong in the same silent way:
//
//   * no base path: the image builds clean and serves at `/`, while every request the router
//     forwards arrives at `/a/pub-<28 hex>/…`. Build green, deploy green, 404 for the first
//     person who opens the app.
//   * no apps hostname: `allowedOrigins: [""]` matches nothing, so Next aborts every Server
//     Action as a CSRF attempt. Forms fail and nothing else does.
//
// The throw surfaces at `RUN npx --no-install next build` in the builder stage: the ACR run
// reports Failed and `images.py` fetches the build log, so this message is what a person
// actually reads. Nothing re-evaluates this file at run time — the standalone output carries
// its own resolved copy of the config, which is why the runtime stage never receives either
// variable.
if (!basePath) {
  throw new Error(
    "the BIAL_BASE_PATH build argument is missing or empty. Every generated app is served " +
      "from one shared hostname under /a/<app key>, so a build without it produces an image " +
      "that answers at / and 404s behind the platform's router.",
  );
}
if (!appsHostname) {
  throw new Error(
    "the BIAL_APPS_HOSTNAME build argument is missing or empty. It is the browser origin " +
      "every generated app is reached on, and Next checks a Server Action's Origin against " +
      "it — without it every form post in this app is aborted as a CSRF attempt.",
  );
}

export default {
  ...appConfig,

  // A containerised deploy needs the standalone server bundle. The golden template does
  // not set this (its job is `next dev`), and the agent can edit that file, so the
  // platform asserts it here rather than hoping.
  output: "standalone",

  // The app's assigned address. ASSERTED, never merged — the same shape as `output` above and
  // for the same reason: the app's config is agent-written, and an agent that sets `basePath`
  // for its own reasons would otherwise move the app off the only path the router forwards to
  // it, which reads as a 404 rather than as a bad config. The value is composed from the
  // container's own name in `deploy/images.py` (`/a/pub-<28 hex>`, leading slash, no trailing
  // one — Next 308-redirects the slashed form and rejects a malformed value outright).
  basePath,

  // SERVER ACTIONS ARE GUARDED SEPARATELY from everything else here. Next compares the
  // browser's `Origin` against the forwarded `Host` and aborts on a mismatch — and once
  // traffic arrives through the platform's router those two differ by construction: the
  // browser is on the apps hostname, the upstream Host is this container's own name. Without
  // this key every form post fails its CSRF check, with no other symptom, and the platform
  // steers app code toward Server Actions as a mainstream data path.
  //
  // SPREAD AT BOTH LEVELS, unlike `basePath`. `experimental` is a bag of unrelated keys and
  // the one the platform needs is two levels down, so a bare `experimental: { serverActions:
  // … }` would silently delete every other experimental setting the app had — and a bare
  // `serverActions: { allowedOrigins: … }` would do the same to its siblings (`bodySizeLimit`
  // is the one an upload-handling app plausibly sets).
  experimental: {
    ...(appConfig?.experimental ?? {}),
    serverActions: {
      ...(appConfig?.experimental?.serverActions ?? {}),
      allowedOrigins: [appsHostname],
    },
  },

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
