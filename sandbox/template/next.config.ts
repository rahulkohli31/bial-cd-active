// ============================================================================================
// PLATFORM-OWNED FILE. DO NOT MODIFY THIS FILE UNDER ANY CONDITION.
//
// If you are an AI agent building an app in this workspace: this file is not yours. Read it,
// never edit it, never delete it, never replace it, and never "clean it up". Every key below
// is load-bearing infrastructure that the BIAL platform depends on to serve your app to a real
// person. Nothing you are asked to build requires changing this file. If a task seems to need
// a change here, it does not — solve it in your own code instead. Add app configuration to
// your own files; do not add it here.
//
// WHAT THIS FILE IS
//
// The Next.js configuration for an app running inside a BIAL sandbox container. The app you
// are writing is NOT served the way `next dev` normally serves one. It runs inside a container
// that a BIAL employee reaches through the company network, framed inside the BIAL portal.
// Several layers sit between your code and the person looking at it, and the keys below are
// what make those layers work.
//
// HOW IT WORKS
//
//   browser (BIAL desk)
//     -> Application Gateway  (the single public entry point for the whole company)
//     -> portal edge          (matches /a/<your-app-key>/ and routes it to this container)
//     -> Caddy, in this container, on port 8080
//          /_sup/*  -> the platform supervisor (never framed, never yours)
//          /*       -> `next dev` on port 3000  <- your app
//
// YOUR APP DOES NOT LIVE AT `/`. It lives under `/a/<your-app-key>/`, because BIAL serves every
// generated app from ONE hostname and tells them apart by that key in the path. Each app does
// not get its own hostname: that would need a wildcard TLS certificate, which BIAL refused.
// `basePath` below is what makes the framework generate every link, asset and route under your
// key. You do not need to write the key anywhere — Next adds it for you.
//
// Three consequences follow, and they are why the keys below exist:
//
//   1. `next dev` never sees the browser's real address. Every request reaches it through
//      Caddy, so from its point of view every request is cross-origin. Next 16 blocks dev
//      origins it has not been told about, and the failure is not a visible error: hydration
//      stalls, the injected window config never publishes, and the app sits on a loading
//      skeleton forever while still returning HTTP 200. `allowedDevOrigins` is what prevents
//      that. Removing or narrowing it produces an app that looks alive to every automated
//      check and is dead to the user.
//
//   2. Because the app is served under a path rather than at a hostname root, the framework has
//      to be TOLD that path. It is injected by the platform as an environment variable and read
//      below; it is never hard-coded, because it differs for every app and changes when an app
//      is published. Remove it and the app answers at `/` while the platform asks for
//      `/a/<key>/` — a preview that loads a blank page while every automated check still passes.
//
//   3. The person looking at this app is a non-technical BIAL employee, not a developer. What
//      the platform shows them, and what it reports back to you when something breaks, is
//      decided here and in the supervisor. Changing it does not change what the user sees for
//      the better; it changes what the platform can tell you when your code is wrong.
//
// WHAT BREAKS IF YOU EDIT IT
//
//   basePath            Removing it, or hard-coding a different value, takes the app off the
//                       address the platform routes to. The preview then loads a 404 page
//                       instead of your app, and a published link a colleague already holds
//                       stops working. The platform detects this and reports `config_tampered`
//                       rather than letting you debug a route that was never wrong.
//   serverActions       `allowedOrigins` is what lets a Server Action accept a form post that
//                       arrived through BIAL's gateway. Without it Next compares the browser's
//                       origin against this container's own name, sees a mismatch, and aborts
//                       the action — every form in the app fails, with a CSRF error the user
//                       cannot act on.
//   allowedDevOrigins   Removing an entry, or replacing the `**.` glob with `*.`, blocks the
//                       live preview. A real ACA hostname is multi-label and a single `*` does
//                       not span dots. This exact mistake shipped once and cost a full debug
//                       cycle: the preview returned 200 and never rendered.
//   typescript          Setting `ignoreBuildErrors: true` disables the only gate that catches
//                       your own type errors before the app is published. The platform's
//                       self-heal loop depends on those failures being real.
//   devIndicators       Turning the badge back on shows a red error counter to a user who
//                       cannot act on it and reads it as "my app is broken".
//
// If this file has been changed, the correct action is to restore it, not to work around it.
// ============================================================================================

import type { NextConfig } from "next";

// Platform-injected, per app, through the supervisor's fail-closed child-env allowlist. Both are
// absent on a plain local dev loop, and the app then serves at `/` exactly as an ordinary Next
// app does — so nothing here changes how this template behaves outside a BIAL sandbox.
//
// `BIAL_BASE_PATH` is the app's own address, e.g. `/a/sbx-<28 hex>`. Next requires it to start
// with `/` and to carry NO trailing slash; the platform sends it in exactly that shape, and the
// supervisor refuses to pass on a value that is not.
const basePath = process.env.BIAL_BASE_PATH ?? "";
// `BIAL_APPS_HOSTNAME` is the single public hostname every generated app is served from.
const appsHostname = process.env.BIAL_APPS_HOSTNAME ?? "";

const nextConfig: NextConfig = {
  // Serve the whole app under the key the platform routes on. Spread conditionally rather than
  // assigned as `""`: an absent `basePath` and an empty one are not the same to Next, and the
  // local dev loop has to stay byte-identical to what it was before this key existed.
  ...(basePath ? { basePath } : {}),
  // SERVER ACTIONS ARE GUARDED SEPARATELY FROM DEV ASSETS. `allowedDevOrigins` below governs
  // the dev asset fetches and the reload socket; it does NOT govern Server Actions, which
  // compare the browser's `Origin` against the forwarded host and abort on a mismatch. Once
  // traffic arrives through BIAL's router those two differ by construction — the browser is on
  // the apps hostname, the upstream Host is this container's own name — so a form post would
  // fail its CSRF check with no other symptom. The platform steers app code toward Server
  // Actions as a mainstream data path, which makes this a common path, not an exotic one.
  ...(appsHostname
    ? { experimental: { serverActions: { allowedOrigins: [appsHostname] } } }
    : {}),
  // The sandbox runs `next dev` behind an in-container Caddy proxy (:8080 → :3000). EVERY dev
  // resource (the HMR websocket, the RSC/flight payload, chunks) then arrives cross-origin to next
  // dev's own localhost:3000 — and Next 16 blocks unlisted dev origins by default, which not only
  // kills HMR but STALLS HYDRATION (window.__BIAL_CONFIG never publishes, the CRUD screen sticks on
  // its loading skeleton). So allow every host the app is actually served on: the ACA FQDN in
  // production, and 127.0.0.1/localhost for the local dev-loop acceptance (proven by Track SANDBOX
  // U13 — the acceptance run is how this gap surfaced).
  //
  // The glob MUST be `**.` — a real ACA FQDN is MULTI-LABEL
  // (sbx-<id>.<env-domain>.<region>.azurecontainerapps.io) and a single `*` does not span
  // label dots: `*.azurecontainerapps.io` matched only single-label hosts, so next dev 403'd
  // the HMR upgrade and hydration never ran on real ACA (2026-07-16 browser E2E finding).
  //
  // `**.bialairport.com` is the BIAL-hosted name. BIAL prod runs the ACA environment INTERNAL
  // (one public App Gateway, everything else private), so its `*.azurecontainerapps.io` domain
  // has no public DNS and does not resolve from a BIAL desk at all — the apps are reached
  // through a `bialairport.com` hostname instead. Matched at the APEX deliberately: the exact
  // subdomain is BIAL's to choose, and pinning a guess here would bake a wrong value into the
  // image, where being wrong costs a 200-that-hydrates-dead rather than a visible error. Both
  // entries stay — the ACA name is still how dev and E2E environments serve.
  allowedDevOrigins: ["**.bialairport.com", "**.azurecontainerapps.io", "127.0.0.1", "localhost"],
  // Untrusted, agent-generated feature code lives here; keep type + build errors HARD so
  // BRAIN's self-heal loop (C7: tsc / next build failures over /exec) actually fires.
  typescript: { ignoreBuildErrors: false },
  // BADGE-ONLY (measured, not assumed): this hides the small floating dev-tools indicator in the
  // corner. It does NOT touch the full-viewport compile/runtime error overlay — Next still
  // surfaces every compile and runtime error through that overlay regardless of this key. The app
  // is served by `next dev` to NON-TECHNICAL BIAL users, who cannot act on the badge and read its
  // red issue counter as "my app is broken", so hiding it costs the platform no signal: the badge
  // only DISPLAYS errors that already reach us by another path — Next 16 defaults
  // `logging.browserToTerminal` to "warn", forwarding browser console output (React's
  // hydration-mismatch errors included) to the dev server's own stdout, the stream the self-heal
  // verify already tails. Keep those two facts together: re-enabling the terminal-logging opt-out
  // would make this line a real loss of observability rather than a cosmetic one. (The overlay
  // itself is suppressed separately, outside this file — see the supervisor's `dev_start`.)
  devIndicators: false,
};

export default nextConfig;
