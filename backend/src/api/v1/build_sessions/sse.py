"""The C3 GET-SSE progress feed — byte-framing COPIED from the legacy chat relay (D6: copy
into your own router, never shared-edit someone else's), adapted for the C7 envelope +
`Last-Event-ID` resume (KTD-5). The relay has since been retired; the copy is why this
router did not have to change when it went.

Each frame is `id: {seq}\\n` + `data: {compact-envelope-json}\\n\\n`; the terminal
`ended` envelope is followed by `data: [DONE]\\n\\n`. Unlike that relay, this does NOT
await a first queued item before committing to the StreamingResponse (verified minor):
the producer (`run_build`) already ran at `start` and this GET is a pure CONSUMER, so a
freshly-registered subscriber queue receives only future puts — awaiting it would hang a
quiet-but-live or already-ended session (whose terminal lives in the replay BUFFER, not
the queue). The only synchronous pre-stream failure is the 404 ownership check.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse

from src.api.v1.build_sessions.schemas import EndedEvent, ProgressEnvelope
from src.services.build_sessions import BuildSession

# Copied verbatim from the retired relay's router (see the module docstring: copy, never
# shared-edit).
_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
_DONE = b"data: [DONE]\n\n"
# Bounded per-connection queue: a slow/dead subscriber is dropped by on_progress
# (per-subscriber isolation), never allowed to grow unbounded.
_SSE_QUEUE_MAXSIZE = 1000
# The queue is only a low-latency WAKEUP; the append-only `session.envelopes` buffer is the
# source of truth. This fallback re-scan interval bounds close-latency when the queue drops
# an envelope (a slow client on a chatty build) — including the terminal `ended`, which would
# otherwise hang the feed forever. Normal frames wake instantly via the queue.
_BUFFER_RESCAN_SECONDS = 10.0


def _frame(env: ProgressEnvelope) -> bytes:
    # id: {seq} carries the SSE resume cursor; data: is the full C7 envelope (snake_case,
    # compact via Pydantic model_dump_json), `seq` preserved verbatim (never renumbered).
    return b"id: " + str(env.seq).encode() + b"\ndata: " + env.model_dump_json().encode() + b"\n\n"


def build_sse_response(session: BuildSession, last_event_id: int | None) -> StreamingResponse:
    """Register a subscriber, replay `seq > last`, then stream live until the terminal
    `ended` → `[DONE]`. Resume semantics (C3 §4): an explicit `Last-Event-ID: n` replays
    `seq > n` (`0` = full backlog); no header → live-from-current-position on a LIVE
    session, or the full story on an already-ended one (so a fresh connect never hangs)."""
    if last_event_id is not None:
        replay_after = last_event_id
    elif session.terminal_emitted:
        replay_after = 0  # ended: give a fresh connect the whole story + [DONE]
    else:
        replay_after = session.last_seq  # live: from the current position

    queue: asyncio.Queue[ProgressEnvelope] = asyncio.Queue(maxsize=_SSE_QUEUE_MAXSIZE)
    session.subscribers.add(queue)

    async def generator() -> AsyncIterator[bytes]:
        # BUFFER-AUTHORITATIVE: `session.envelopes` is append-only and holds EVERY envelope
        # (on_progress buffers unconditionally). The queue is only a wakeup, so a dropped
        # envelope — even the terminal `ended` on a full queue — is always recovered from the
        # buffer on the next scan. `idx` walks the buffer once; the timeout bounds recovery.
        idx = 0
        try:
            while True:
                # Emit every not-yet-sent buffered frame with seq > replay_after, in order.
                while idx < len(session.envelopes):
                    env = session.envelopes[idx]
                    idx += 1
                    if env.seq <= replay_after:
                        continue
                    yield _frame(env)
                    if isinstance(env, EndedEvent):
                        yield _DONE
                        return
                # Caught up to the buffer. Close only once the end sequence has FULLY run
                # (finalize_task done) — so a still-pending synthesized terminal `ended` is
                # not dropped, yet a synthesize that failed to buffer an `ended` still closes
                # (never hangs). A buffered terminal already returned via the inner loop above.
                ft = session.finalize_task
                if session.terminal_committed and ft is not None and ft.done():
                    yield _DONE
                    return
                # Wait for the next live push (instant on a normal frame); the timeout is a
                # fallback that re-scans the buffer if the queue dropped an envelope.
                try:
                    await asyncio.wait_for(queue.get(), timeout=_BUFFER_RESCAN_SECONDS)
                except TimeoutError:
                    pass
        finally:
            # Client disconnect (GeneratorExit) or normal close: drop this subscriber. The
            # run_build task keeps running — the SessionManager owns it, decoupled from the
            # SSE lifecycle (the chat-relay drain analogue).
            session.subscribers.discard(queue)

    return StreamingResponse(generator(), media_type="text/event-stream", headers=_SSE_HEADERS)
