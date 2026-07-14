"""C4 snapshot write (KTD-7): commit the working tree → bundle the CURRENT tree only →
base64 it over the C1 `/exec` endpoint → `put` to Blob at `snapshot_key(app_id)`.

The bundle is the current tree (HEAD), NOT `git bundle --all` full history: the POC only
needs current code to survive teardown so the user can resume, and dropping history keeps
the base64-over-`/exec` payload small (workspaces are source-only — node_modules is baked
into the image). The snapshot is an OPAQUE, SESSION-API-only artifact (C4) that no other
track reads, so it round-trips within this track.
"""

from __future__ import annotations

import base64
import uuid
from contextlib import suppress

import structlog

from src.services.sandbox import SandboxClient, SandboxError, SandboxHandle
from src.services.storage import get_storage, snapshot_key

_log = structlog.get_logger()

# `--allow-empty` so a no-change turn still snapshots cleanly; bundle HEAD only (KTD-7).
_COMMIT_SCRIPT = "git add -A && git commit -q -m bial-snapshot --allow-empty"
_BUNDLE_SCRIPT = "git bundle create app.bundle HEAD"
_BUNDLE_NAME = "app.bundle"


async def write_snapshot(
    sandbox_client: SandboxClient, handle: SandboxHandle, app_id: uuid.UUID
) -> None:
    """Snapshot the sandbox's current tree to Blob (overwrite-latest). Step 1 of the
    ordered end (C4) — the caller runs teardown + release AFTER this returns."""
    await sandbox_client.exec(handle, ["sh", "-c", _COMMIT_SCRIPT])
    await sandbox_client.exec(handle, ["sh", "-c", _BUNDLE_SCRIPT])
    result = await sandbox_client.exec(handle, ["base64", _BUNDLE_NAME])
    if result.exit != 0:
        raise SandboxError(f"snapshot bundle read failed (exit {result.exit})")
    data = base64.b64decode(result.stdout)
    await get_storage().put(snapshot_key(app_id), data, content_type="application/octet-stream")
    # Best-effort cleanup of the on-disk bundle (a raise here must not lose the committed
    # snapshot, which is already in Blob).
    with suppress(SandboxError):
        await sandbox_client.exec(handle, ["rm", "-f", _BUNDLE_NAME])
