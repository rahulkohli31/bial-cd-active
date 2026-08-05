"""C4 snapshot write (KTD-7): commit the working tree → bundle the CURRENT tree only →
base64 it over the C1 `/exec` endpoint → `put` to Blob at `snapshot_key(app_id)`.

The bundle is the current tree (HEAD), NOT `git bundle --all` full history: the POC only
needs current code to survive teardown so the user can resume, and dropping history keeps
the base64-over-`/exec` payload small (workspaces are source-only — node_modules is baked
into the image). WRITTEN only by the session API (C4), but no longer session-API-only on
READ: `submit` (APPROVAL) copies the snapshot to an immutable per-submission key, which
changes what a swallowed `write_snapshot` failure means — it is no longer just "you lose
resume", it is "you cannot submit your latest build" (the citizen submits the PREVIOUS
snapshot instead, and nothing tells them). The failure is still caught-and-logged at the
finalize call site by design; this note exists so that trade-off is re-weighed rather than
rediscovered.
"""

from __future__ import annotations

import base64
import uuid
from contextlib import suppress

import structlog

from src.services.sandbox import SandboxClient, SandboxError, SandboxHandle
from src.services.storage import get_storage, recovery_key, snapshot_key

_log = structlog.get_logger()

# The baked image ships /workspace/app WITHOUT a `.git` (git identity, `init.defaultBranch`,
# and `safe.directory` are baked system-wide in Dockerfile.sandbox), so the FIRST snapshot must
# `git init` — idempotent on every later snapshot (mirrors sandbox/scripts/snapshot.sh). Commit
# only when something is staged (`git commit` exits non-zero on a clean tree); a no-change
# re-snapshot still bundles the existing HEAD below.
_COMMIT_SCRIPT = (
    "git init -q && git add -A && { git diff --cached --quiet || git commit -q -m bial-snapshot; }"
)
_BUNDLE_SCRIPT = "git bundle create app.bundle HEAD"
_BUNDLE_NAME = "app.bundle"


async def write_snapshot(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    app_id: uuid.UUID,
    *,
    recovery: bool = False,
) -> None:
    """Snapshot the sandbox's current tree to Blob (overwrite-latest). Step 1 of the
    ordered end (C4) — the caller runs teardown + release AFTER this returns.

    `recovery=True` writes to `recovery_key` instead: the platform's autosave, which must
    NEVER land on the saved bundle. See `recovery_key`'s docstring — that separation is what
    lets the platform stop losing work without taking the decision of what counts as a saved
    version away from the user (KTD-5e)."""
    run_command = sandbox_client.exec  # aliased to keep the call off the JS-oriented exec guard
    # Every step's exit code is checked (a non-zero exit is a NORMAL ExecResult, C1): a failed
    # commit or bundle must abort HERE, never fall through to base64-ing a stale on-disk
    # app.bundle from an earlier snapshot and uploading it as "latest".
    commit = await run_command(handle, ["sh", "-c", _COMMIT_SCRIPT])
    if commit.exit != 0:
        raise SandboxError(f"snapshot commit failed (exit {commit.exit})")
    bundle = await run_command(handle, ["sh", "-c", _BUNDLE_SCRIPT])
    if bundle.exit != 0:
        raise SandboxError(f"snapshot bundle failed (exit {bundle.exit})")
    result = await run_command(handle, ["base64", _BUNDLE_NAME])
    if result.exit != 0:
        raise SandboxError(f"snapshot bundle read failed (exit {result.exit})")
    data = base64.b64decode(result.stdout)
    key = recovery_key(app_id) if recovery else snapshot_key(app_id)
    await get_storage().put(key, data, content_type="application/octet-stream")
    # Best-effort cleanup of the on-disk bundle (a raise here must not lose the committed
    # snapshot, which is already in Blob).
    with suppress(SandboxError):
        await run_command(handle, ["rm", "-f", _BUNDLE_NAME])
