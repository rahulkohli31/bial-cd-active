"""App-serving service: admin governance danger-ops, per-app data quota, and
runner-token verify. (The old-JSX runner HTML shells, per-route CSP builders,
runner-token mint wrapper, and the client-compiled artifact gate were retired
with the open-sandbox pivot + the APPROVAL re-shape; deployed apps are served
from the sandbox's own Caddy and submissions are server-side git bundles.)
Consumers import the submodules directly (`governance`, `quota`, `runner_token`)."""
