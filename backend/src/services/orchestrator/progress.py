"""The single C7 `seq` source + the only path to `on_progress` (C7 §2/§3/§4, KD-5, KD-12).

One `ProgressEmitter` per run owns one counter behind one `_emit` coroutine: every envelope's
`seq` is assigned there and nowhere else, so the stream is strictly `+1` and gap-free (KD-12).
Nothing calls `on_progress` directly. Typed helpers construct the SEVEN C7 members BRAIN may emit;
the `log` helper redacts raw stdout/stderr before it egresses (C7 §3.2 relays it to the portal
verbatim — the same egress `error.cleaned_stack` is redacted for, KD-5), and `error` carries a
`BuildError` already de-noised + redacted by `errors.declutter`.

There is deliberately NO `ended` helper — the seventh member is SESSION-API's alone (R7). It emits
the single terminal frame from `_do_finalize`, AFTER its C4 snapshot, continuing this run's stream
at `last_seq + 1` (the emitter's final `last_seq` is handed over on `BuildResult.last_seq`), so the
`seq` chain stays gap-free ACROSS the handoff. Withholding the helper is what makes "BRAIN cannot
emit a terminal" structural: there is no method to call, so no BRAIN path can race SESSION-API's
frame or ship a `snapshot_committed` that predates the snapshot.

The sink is contractually non-throwing (an unbounded `asyncio.Queue.put`, open-Q H); a raising
sink is swallowed-and-logged so a lost frame never breaks the loop (KD-12).

CONCURRENT EMITTERS ARE SEQ-SAFE (F8/U5). The build loop is no longer the ONLY emitter: the early
readiness watcher (`harness._watch_preview`) is a second coroutine that emits `preview_ready` /
`preview_reconnecting` on the SAME emitter while the loop runs. This stays gap-free because `_emit`
assigns `seq` and reaches the sink with NO `await` between the assignment and the sink call, and
the manager sink buffers + bumps its `last_seq` synchronously before its own first await — so
two coroutines interleave only at `await` boundaries that fall AFTER each `seq` is already fixed.
The watcher is torn down (cancelled + awaited) before the terminal funnel reads `last_seq`, so no
concurrent emit can land after the verdict's `seq` baton is captured.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Literal

import structlog

from src.api.v1.build_sessions.schemas import (
    BuildError,
    ErrorEvent,
    EscalationEvent,
    LogEvent,
    PreviewReadyEvent,
    PreviewReconnectingEvent,
    ProgressEnvelope,
    ProgressSink,
    QuotaExceededEvent,
    StepEvent,
)
from src.services.orchestrator.errors import redact_secrets

logger = structlog.get_logger()


class ProgressEmitter:
    """The single seq source + emit path for one `run_build` (KD-12)."""

    def __init__(self, sink: ProgressSink) -> None:
        self._sink = sink
        self._counter = itertools.count(1)
        self.last_seq = 0

    async def _emit(self, make_event: Callable[[int], ProgressEnvelope]) -> int:
        """Assign the next `seq`, build the envelope, and push it to the sink. The ONLY place a
        `seq` is assigned and the ONLY caller of the sink."""
        seq = next(self._counter)
        self.last_seq = seq
        event = make_event(seq)
        try:
            await self._sink(event)
        except Exception:
            # The sink is contractually non-throwing; if it ever raises, a lost frame must not
            # break the build loop (KD-12). Swallow-and-log — the counter has already advanced.
            logger.warning("progress_sink_raised", seq=seq, event_type=event.type)
        return seq

    async def step(
        self,
        *,
        name: str,
        label: str,
        state: Literal["started", "ok", "failed"],
        hidden: bool = False,
    ) -> int:
        # `hidden` drops read-only + housekeeping steps from the visible feed (F3/U3); it defaults
        # False so every existing caller keeps emitting a visible step unchanged.
        return await self._emit(
            lambda seq: StepEvent(seq=seq, name=name, label=label, state=state, hidden=hidden)
        )

    async def log(self, *, source: str, stream: Literal["stdout", "stderr"], text: str) -> int:
        # Redact BEFORE wrapping — raw exec/dev output can carry app-env credentials and this
        # text is relayed to the portal verbatim (KD-5).
        clean = redact_secrets(text)
        return await self._emit(
            lambda seq: LogEvent(seq=seq, source=source, stream=stream, text=clean)
        )

    async def error(self, error: BuildError) -> int:
        # `error` is already de-noised + redacted by `errors.declutter` (the single redaction
        # site for the structured-error egress path).
        return await self._emit(
            lambda seq: ErrorEvent(
                seq=seq,
                source=error.source,
                title=error.title,
                cleaned_stack=error.cleaned_stack,
            )
        )

    async def preview_ready(self, *, preview_url: str) -> int:
        return await self._emit(lambda seq: PreviewReadyEvent(seq=seq, preview_url=preview_url))

    async def preview_reconnecting(self) -> int:
        # F8/U5 — the dev-server process crashed after the preview was framed. A feed-only signal
        # (no payload): the portal shows a distinct "reconnecting" visual until a `preview_ready`
        # re-frames. Emitted only by the early readiness watcher, which owns crash detection.
        return await self._emit(lambda seq: PreviewReconnectingEvent(seq=seq))

    async def escalation(
        self, *, reason: str, detail: str, last_error: BuildError | None = None
    ) -> int:
        return await self._emit(
            lambda seq: EscalationEvent(
                seq=seq, reason=reason, detail=detail, last_error=last_error
            )
        )

    async def quota_exceeded(self, *, limit: int, used: int, resets_at: str) -> int:
        return await self._emit(
            lambda seq: QuotaExceededEvent(seq=seq, limit=limit, used=used, resets_at=resets_at)
        )

    # NOTE: no `ended` helper by design — see the module docstring. The terminal frame is
    # SESSION-API's (`_do_finalize`), emitted after the C4 snapshot at `last_seq + 1`.
