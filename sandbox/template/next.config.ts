import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The sandbox runs `next dev` behind an in-container Caddy proxy on a public ACA FQDN.
  // Allow the dev server to accept the cross-origin HMR/asset requests that arrive through
  // that proxy (Next 16 blocks unlisted dev origins by default).
  allowedDevOrigins: ["*.azurecontainerapps.io"],
  // Untrusted, agent-generated feature code lives here; keep type + build errors HARD so
  // BRAIN's self-heal loop (C7: tsc / next build failures over /exec) actually fires.
  typescript: { ignoreBuildErrors: false },
};

export default nextConfig;
