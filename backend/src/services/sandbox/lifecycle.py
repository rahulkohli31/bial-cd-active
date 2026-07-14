"""App-level sandbox-client lifecycle hook (frozen in Stage 0, filled by SESSION-API).

The FastAPI lifespan (main.py) closes the app-global sandbox client on shutdown
alongside the Redis pool and the object store, so no long-lived HTTP pool leaks.
Stage 0 has no concrete client — the C2 `SandboxClient` ABC (`base.py`) is
unimplemented until Track SESSION-API supplies the ACA/helper client in Wave 1 — so
this is a **no-op stub**. Freezing the hook now means main.py's lifespan never needs
re-opening: SESSION-API wires its client's `aclose()` behind this function.
"""

from __future__ import annotations

from src.services.sandbox.client import aclose_sandbox_singleton


async def aclose_sandbox() -> None:
    """Close the app-global sandbox client (its HTTP pool + ACA client/credential) on
    shutdown. Delegates to the concrete client's isolated singleton close (Wave 1).
    Safe to call when no client was ever opened (mirrors `aclose_storage` /
    `aclose_redis`)."""
    await aclose_sandbox_singleton()
