"""The single seq source + redacted egress (U2, KD-5/KD-12)."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from src.api.v1.build_sessions.schemas import (
    BuildSessionStatus,
    EndedEvent,
    LogEvent,
    ProgressEnvelope,
    StepEvent,
)
from src.services.orchestrator import errors
from src.services.orchestrator.progress import ProgressEmitter

_ENVELOPE: TypeAdapter[ProgressEnvelope] = TypeAdapter(ProgressEnvelope)


def _collecting_sink() -> tuple[list[ProgressEnvelope], ProgressEmitter]:
    captured: list[ProgressEnvelope] = []

    async def sink(env: ProgressEnvelope) -> None:
        captured.append(env)

    return captured, ProgressEmitter(sink)


async def test_seq_is_strictly_increasing_gap_free() -> None:
    captured, emitter = _collecting_sink()
    await emitter.step(name="scaffold", label="Scaffolding…", state="started")
    await emitter.log(source="exec", stream="stdout", text="hello")
    await emitter.preview_ready(preview_url="https://x/")
    await emitter.ended(
        status=BuildSessionStatus.ENDED, reason="completed", snapshot_committed=False
    )
    seqs = [env.seq for env in captured]
    assert seqs == [1, 2, 3, 4]
    assert emitter.last_seq == 4


async def test_every_envelope_validates_against_the_union() -> None:
    captured, emitter = _collecting_sink()
    await emitter.step(name="s", label="l", state="ok")
    await emitter.log(source="dev", stream="stderr", text="t")
    for env in captured:
        # round-trips through the discriminated union (extra="forbid" rejects stray keys)
        assert _ENVELOPE.validate_python(env.model_dump())


async def test_ended_rejects_a_non_terminal_status() -> None:
    # A terminal frame carrying a non-terminal status must fail validation, not slip through.
    with pytest.raises(ValidationError):
        EndedEvent.model_validate(
            {"seq": 1, "status": "building", "reason": "nope", "snapshot_committed": False}
        )


async def test_log_text_is_redacted_at_emit() -> None:
    captured, emitter = _collecting_sink()
    await emitter.log(
        source="dev",
        stream="stderr",
        text="cfg BIAL_APP_CREDENTIAL=bial_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6 loaded",
    )
    log = captured[0]
    assert isinstance(log, LogEvent)
    assert "bial_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6" not in log.text
    assert "***" in log.text


async def test_error_egress_is_redacted() -> None:
    captured, emitter = _collecting_sink()
    err = errors.from_server("FOO_SECRET=leakme crashed the server")
    await emitter.error(err)
    egressed = captured[0].model_dump()
    assert "leakme" not in egressed["cleaned_stack"]


async def test_raising_sink_is_swallowed_and_counter_still_advances() -> None:
    async def boom(env: ProgressEnvelope) -> None:
        raise RuntimeError("sink is down")

    emitter = ProgressEmitter(boom)
    # Does not propagate…
    await emitter.step(name="s", label="l", state="started")
    await emitter.step(name="s2", label="l2", state="ok")
    # …and the seq counter still advanced (a lost frame never rewinds the sequence, KD-12).
    assert emitter.last_seq == 2


def test_step_state_is_constrained() -> None:
    with pytest.raises(ValidationError):
        StepEvent.model_validate({"seq": 1, "name": "s", "label": "l", "state": "bogus"})
