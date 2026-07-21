"""App-serving service: admin governance danger-ops. (The old-JSX runner HTML
shells, per-route CSP builders, runner-token mint/verify wrappers, the
client-compiled artifact gate, and the per-app data quota were retired with the
open-sandbox pivot, the APPROVAL re-shape, and the shared-data-plane removal;
deployed apps are served from the sandbox's own Caddy, submissions are server-side
git bundles, and app data lives in the project's own database.)
Consumers import the submodule directly (`governance`)."""
