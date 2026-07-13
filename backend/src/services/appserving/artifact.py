"""Client-compiled artifact validation (R19, AE4).

JSX compilation moved fully to the browser (`@babel/standalone`): the client submits
the compiled JS artifact and the backend validates presence/shape and stores it —
**the server never runs Babel**. The real
containment boundary is the runner's sandboxed opaque-origin iframe + CSP
(unchanged), not this validation; a non-compiling validator can only assert the
artifact is a present, non-empty, size-bounded string. A missing/malformed
(empty / oversized) artifact is rejected fail-closed so an app can never reach the
runner with nothing to serve.
"""

from __future__ import annotations

# Ceiling on the compiled JS artifact. A generated app's compiled bundle is
# comfortably under this; the cap bounds a request-body DoS (Starlette applies no
# body-size limit of its own). Generous relative to the ~256 KB app-generation
# output cap, with headroom for the compiled expansion.
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024  # 2 MiB


class ArtifactError(ValueError):
    """The submitted compiled artifact is missing or malformed. Rendered by the
    caller to a 4xx with a stable, non-leaking message."""


def validate_artifact(compiled: object) -> str:
    """Validate a client-produced compiled artifact and return it unchanged.

    Fail-closed: raise `ArtifactError` unless `compiled` is a present, non-empty,
    size-bounded string. The server performs NO compilation — this only asserts the
    artifact is well-shaped enough to store and later serve verbatim inside the
    runner frame's IIFE wrapper.
    """
    if not isinstance(compiled, str):
        raise ArtifactError("A compiled artifact is required.")
    if not compiled.strip():
        raise ArtifactError("The compiled artifact is empty.")
    if len(compiled.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise ArtifactError("The compiled artifact exceeds the size limit.")
    return compiled
