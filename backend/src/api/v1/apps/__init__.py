"""The `apps` domain: owner-facing app lifecycle (provision/submit/status) and the
per-app data-service (records). The old-JSX serving surface (files, parse, and the
runner HTML shell) was retired with the open-sandbox pivot — deployed apps are served
from the sandbox's own Caddy, not this control plane.
"""
