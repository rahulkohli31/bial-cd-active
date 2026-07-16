"""App-serving service: client-compiled artifact validation, admin governance
danger-ops, per-app data quota, and runner-token verify. (The old-JSX runner
HTML shells + per-route CSP builders — and the runner-token mint wrapper — were
retired with the open-sandbox pivot; deployed apps are served from the sandbox's
own Caddy.) Public surface via explicit `from .x import Y as Y` re-exports.
"""

from src.services.appserving.artifact import MAX_ARTIFACT_BYTES as MAX_ARTIFACT_BYTES
from src.services.appserving.artifact import ArtifactError as ArtifactError
from src.services.appserving.artifact import validate_artifact as validate_artifact
