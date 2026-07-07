"""Killable process governor for untrusted-file parsing (R26, bound 4).

A thread cannot preempt a CPU-bound C-extension parse (openpyxl/lxml) or contain its
OOM, so each parse runs in a FRESH `multiprocessing` process (spawn — never fork the
async server's state) that can be hard-`terminate()`d. The child sets an address-space
rlimit (best-effort; enforced on the Linux runtime) and reports its result over a
Queue; the parent enforces a wall-clock timeout and maps failures:

* wall-clock exceeded, child still alive → terminate → 413 `PARSE_TIMEOUT`
* child died with no result (OS OOM-kill / crash) → 413 `FILE_TOO_LARGE`
* child caught `MemoryError` (rlimit) → 413 `FILE_TOO_LARGE`
* child raised `FileParseError` → that status/code
* any other child error → 500 `PARSE_FAILED`

Contained OOM/timeout NEVER takes down the API. The blocking wait runs in a thread
executor so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import queue as queue_mod
import time
from typing import Any

from src.services.extract.zip_safety import FileParseError
from src.services.parse.parsers import parse_dispatch

# Wall-clock timeout (Express PARSE_TIMEOUT_MS = 10_000).
PARSE_TIMEOUT_S = 10.0
# Address-space ceiling (best-effort; maps Express's 1024 MB old-gen). Generous vs a
# legit ≤18 MB file, tight enough to bound a runaway inflate. RLIMIT_AS can spuriously
# kill legit numpy/lxml on Linux at a tight value, so it is set generously here.
MEM_LIMIT_BYTES = 1536 * 1024 * 1024


def _set_memory_limit(limit_bytes: int) -> None:
    # `resource` is Unix-only (the Linux runtime); a no-op elsewhere (dev on other OS).
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    except ImportError, ValueError, OSError:
        pass


def _child_worker(
    result_q: Any, buffer: bytes, kind: str, filename: str, sheet: str | None, mem_limit: int
) -> None:
    _set_memory_limit(mem_limit)
    try:
        result_q.put(("ok", parse_dispatch(buffer, kind, filename, sheet)))
    except FileParseError as exc:
        result_q.put(("err", (exc.status, exc.code, str(exc))))
    except MemoryError:
        result_q.put(
            ("err", (413, "FILE_TOO_LARGE", "File is too large or complex to parse safely."))
        )
    except Exception:
        result_q.put(("err", (500, "PARSE_FAILED", "Could not parse the file.")))


def _run_blocking(
    buffer: bytes, kind: str, filename: str, sheet: str | None, timeout: float, mem_limit: int
) -> dict[str, Any]:
    ctx = multiprocessing.get_context("spawn")
    result_q = ctx.Queue()
    proc = ctx.Process(
        target=_child_worker, args=(result_q, buffer, kind, filename, sheet, mem_limit)
    )
    proc.start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                # get() (not join-then-get) so a large result never deadlocks the
                # child's feeder thread. Poll so a fast crash is detected promptly.
                status, payload = result_q.get(timeout=0.1)
                break
            except queue_mod.Empty:
                if not proc.is_alive():
                    # Child died with no result → OS OOM-kill / crash.
                    raise FileParseError(
                        "File is too large or complex to parse safely.",
                        status=413,
                        code="FILE_TOO_LARGE",
                    ) from None
                if time.monotonic() >= deadline:
                    proc.terminate()
                    raise FileParseError(
                        "Parsing took too long and was stopped. Try a smaller file.",
                        status=413,
                        code="PARSE_TIMEOUT",
                    ) from None
    finally:
        if proc.is_alive():
            proc.terminate()
        proc.join(5)

    if status == "ok":
        result: dict[str, Any] = payload
        return result
    http_status, code, message = payload
    raise FileParseError(message, status=http_status, code=code)


async def run_parse(
    buffer: bytes,
    kind: str,
    filename: str,
    sheet: str | None,
    *,
    timeout: float = PARSE_TIMEOUT_S,
    mem_limit: int = MEM_LIMIT_BYTES,
) -> dict[str, Any]:
    """Parse `buffer` in a killable subprocess (off the event loop)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _run_blocking, buffer, kind, filename, sheet, timeout, mem_limit
    )
