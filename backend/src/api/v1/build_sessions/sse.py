"""The build session's GET-SSE progress feed — the streaming response its route returns.

WHY THIS EXISTS

Byte-framing COPIED from the legacy chat relay (copy into your own router, never
shared-edit someone else's), adapted for the envelope and `Last-Event-ID` resume. The
relay has since retired; this copy is why the router did not have to change when it went.

Each frame is `id: {seq}\\n` + `data: {compact-envelope-json}\\n\\n`; the terminal `ended`
envelope is followed by `data: [DONE]\\n\\n`. Unlike that relay, this does NOT await a
first queued item before committing to the StreamingResponse: the producer already ran by
the time anyone subscribes and this GET is a pure CONSUMER, so a freshly-registered
subscriber queue receives only future puts — awaiting it would hang a quiet-but-live or
already-ended session (whose terminal lives in the replay BUFFER, not the queue). The only
synchronous pre-stream failure is the 404 ownership check.

HISTORICAL SESSIONS ONLY. The route this serves survives the deletion of the standalone build
stack as the reader for `build_started` transcript rows that are permanent in production — a
citizen reloading such a thread still gets a `build_in_progress` anchor carrying a session id.
Nothing produces a NEW one (a Write chat turn registers its workspace without ever serialising a
session id), and nothing emits the six BRAIN envelope members any more, so in practice the
ownership check answers 404 and this generator never runs. Kept because the alternative was
deleting the portal's reattach path and the projection's `BuildInProgressItem` with live rows
still in the database.
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
    # id: {seq} carries the SSE resume cursor; data: is the full envelope (snake_case,
    # compact via Pydantic model_dump_json), `seq` preserved verbatim (never renumbered).
    return b"id: " + str(env.seq).encode() + b"\ndata: " + env.model_dump_json().encode() + b"\n\n"


def build_sse_response(session: BuildSession, last_event_id: int | None) -> StreamingResponse:
    """Register a subscriber, replay `seq > last`, then stream live until the terminal
    `ended` → `[DONE]`. Resume semantics: an explicit `Last-Event-ID: n` replays
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
                # Caught up to the buffer. A buffered terminal `ended` is the ONLY thing that
                # closes this feed, and the inner loop above returns on it; a client that goes
                # away closes it from the other end. So: wait for the next live push (instant on
                # a normal frame), with the timeout as a fallback that re-scans the buffer if the
                # queue dropped an envelope.
                try:
                    await asyncio.wait_for(queue.get(), timeout=_BUFFER_RESCAN_SECONDS)
                except TimeoutError:
                    pass
        finally:
            # Client disconnect (GeneratorExit) or normal close: drop this subscriber.
            # Whatever is working in the session keeps working — the SessionManager owns it,
            # decoupled from the SSE lifecycle (the chat-relay drain analogue).
            session.subscribers.discard(queue)

    return StreamingResponse(generator(), media_type="text/event-stream", headers=_SSE_HEADERS)
