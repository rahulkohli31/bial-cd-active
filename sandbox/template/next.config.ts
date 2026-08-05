import type { NextConfig } from "next";

const nextConfig: NextConfig = {
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
  allowedDevOrigins: ["**.azurecontainerapps.io", "127.0.0.1", "localhost"],
  // Untrusted, agent-generated feature code lives here; keep type + build errors HARD so
  // BRAIN's self-heal loop (C7: tsc / next build failures over /exec) actually fires.
  typescript: { ignoreBuildErrors: false },
  // The app is served by `next dev` to NON-TECHNICAL BIAL users, who cannot act on the dev-tools
  // badge and read its red issue counter as "my app is broken". Hiding it costs the platform no
  // signal, because the badge only DISPLAYS errors that already reach us by another path: Next 16
  // defaults `logging.browserToTerminal` to "warn", forwarding browser console output (React's
  // hydration-mismatch errors included) to the dev server's own stdout — the stream the self-heal
  // verify already tails. Keep those two facts together: re-enabling the terminal-logging opt-out
  // would make this line a real loss of observability rather than a cosmetic one.
  devIndicators: false,
};

export default nextConfig;
