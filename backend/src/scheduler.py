"""The Taskiq scheduler (ADR-0011).

The scheduler is only a CLOCK. On a cron tick it pushes a message onto the broker; the receiver
picks it up and executes it. It does not run anything itself — which is why a scheduler deployed
without a worker is worse than no deployment at all: it would enqueue ticks nothing ever
consumes, behind a container that looks perfectly healthy.

`LabelScheduleSource` reads each task's `schedule` label at startup. It creates ZERO Redis keys,
which is why it is used here rather than `RedisScheduleSource` — that adds a `schedule:*` key
family and issues a multi-key read that breaks under an OSS clustering policy.
"""

from __future__ import annotations

from taskiq import ScheduleSource, TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from src.broker import broker

# Schedule sources are held at module scope so `worker_main` can start each one exactly once.
# Annotated `list[ScheduleSource]`, not inferred: `TaskiqScheduler` declares an INVARIANT
# `list[ScheduleSource]`, so an inferred `list[LabelScheduleSource]` is rejected by all three
# type checkers. Widening the element type at the declaration is the fix; `Sequence` is not,
# because the parameter is a `list`.
schedule_sources: list[ScheduleSource] = [LabelScheduleSource(broker)]

scheduler = TaskiqScheduler(broker=broker, sources=schedule_sources)
