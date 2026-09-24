"""Build-sessions domain — the frozen control API + the brain interface.

The portal↔SESSION-API control surface and the BRAIN↔SESSION-API progress envelope + `run_build`
are rendered here as real, tested schemas. BRAIN imports the progress-envelope shapes READ-ONLY.
Public surface via explicit `from .x import Y as Y` re-exports (never `__all__`).

WHY THIS EXISTS
SCHEMAS ONLY — deliberately. This package MUST NOT re-export `deps` or `router` at package
level. Doing so drags FastAPI, the session manager and the whole route tree into any process
that merely wants a progress-envelope shape, which is what made
`src.services.build_sessions.reaper` unimportable outside the API process — the blocker to
running reclamation on a worker at all. The re-exports that were here were also dead: every
consumer already imports the submodule (`src/api/v1/router.py`, `deploy/router.py`,
`admin/router.py`, `conversations/{transition,turns}.py`). Pinned by
`tests/test_import_graph.py`, which imports in a COLD-CACHE SUBPROCESS — an in-process assertion
passes spuriously because conftest has already imported the app.

The `schemas` re-exports below stay: this is where the progress-envelope shapes are frozen."""

from src.api.v1.build_sessions.schemas import BillingSessionFactory as BillingSessionFactory
from src.api.v1.build_sessions.schemas import BuildError as BuildError
from src.api.v1.build_sessions.schemas import BuildResult as BuildResult
from src.api.v1.build_sessions.schemas import BuildSessionStatus as BuildSessionStatus
from src.api.v1.build_sessions.schemas import EndedEvent as EndedEvent
from src.api.v1.build_sessions.schemas import ErrorEvent as ErrorEvent
from src.api.v1.build_sessions.schemas import ErrorSource as ErrorSource
from src.api.v1.build_sessions.schemas import EscalationEvent as EscalationEvent
from src.api.v1.build_sessions.schemas import PreviewReadyEvent as PreviewReadyEvent
from src.api.v1.build_sessions.schemas import (
    PreviewReconnectingEvent as PreviewReconnectingEvent,
)
from src.api.v1.build_sessions.schemas import ProgressEnvelope as ProgressEnvelope
from src.api.v1.build_sessions.schemas import ProgressSink as ProgressSink
from src.api.v1.build_sessions.schemas import QuotaExceededEvent as QuotaExceededEvent
from src.api.v1.build_sessions.schemas import RunBuild as RunBuild
from src.api.v1.build_sessions.schemas import StartBuildRequest as StartBuildRequest
from src.api.v1.build_sessions.schemas import StartBuildResponse as StartBuildResponse
from src.api.v1.build_sessions.schemas import StepEvent as StepEvent
