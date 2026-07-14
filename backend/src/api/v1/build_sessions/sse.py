"""The C3 GET-SSE progress feed — byte-framing COPIED from `claude/router.py` (D6:
copy into your own router, never shared-edit the chat relay), adapted for the C7
envelope + `Last-Event-ID` resume (KTD-5).

Each frame is `id: {seq}\\n` + `data: {compact-envelope-json}\\n\\n`; the terminal
`ended` envelope is followed by `data: [DONE]\\n\\n`. Unlike the chat relay we do NOT
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

# Copied verbatim from claude/router.py.
_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
_DONE = b"data: [DONE]\n\n"
# Bounded per-connection queue: a slow/dead subscriber is dropped by on_progress
# (per-subscriber isolation), never allowed to grow unbounded.
_SSE_QUEUE_MAXSIZE = 1000


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
        seen: set[int] = set()
        try:
            # 1. Replay the buffered envelopes with seq > replay_after (snapshot the buffer
            #    AFTER registering the queue, so a concurrent append lands in the queue and
            #    is deduped by seq — a small replay overlap is idempotent-to-render, C3 §4.2).
            for env in list(session.envelopes):
                if env.seq <= replay_after:
                    continue
                seen.add(env.seq)
                yield _frame(env)
                if isinstance(env, EndedEvent):
                    yield _DONE
                    return
            # 2. Already-ended AND the caller is caught up past the terminal (its seq was
            #    <= replay_after, so nothing new is coming) → close cleanly, don't hang.
            if session.terminal_emitted and session.last_seq <= replay_after:
                yield _DONE
                return
            # 3. Drain live until the terminal ended.
            while True:
                env = await queue.get()
                if env.seq in seen:
                    continue
                seen.add(env.seq)
                yield _frame(env)
                if isinstance(env, EndedEvent):
                    yield _DONE
                    return
        finally:
            # Client disconnect (GeneratorExit) or normal close: drop this subscriber. The
            # run_build task keeps running — the SessionManager owns it, decoupled from the
            # SSE lifecycle (the chat-relay drain analogue).
            session.subscribers.discard(queue)

    return StreamingResponse(generator(), media_type="text/event-stream", headers=_SSE_HEADERS)
