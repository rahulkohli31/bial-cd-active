import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The sandbox runs `next dev` behind an in-container Caddy proxy (:8080 → :3000). EVERY dev
  // resource (the HMR websocket, the RSC/flight payload, chunks) then arrives cross-origin to next
  // dev's own localhost:3000 — and Next 16 blocks unlisted dev origins by default, which not only
  // kills HMR but STALLS HYDRATION (window.__BIAL_CONFIG never publishes, the CRUD screen sticks on
  // its loading skeleton). So allow every host the app is actually served on: the ACA FQDN in
  // production, and 127.0.0.1/localhost for the local dev-loop acceptance (proven by Track SANDBOX
  // U13 — the acceptance run is how this gap surfaced).
  allowedDevOrigins: ["*.azurecontainerapps.io", "127.0.0.1", "localhost"],
  // Untrusted, agent-generated feature code lives here; keep type + build errors HARD so
  // BRAIN's self-heal loop (C7: tsc / next build failures over /exec) actually fires.
  typescript: { ignoreBuildErrors: false },
};

export default nextConfig;
