"""Build-sessions domain — the frozen C3 control API + the C7 brain interface.

C3 (portal↔SESSION-API control surface) and C7 (BRAIN↔SESSION-API progress envelope +
`run_build`) are rendered here as real, tested schemas (U8). BRAIN imports the C7
shapes READ-ONLY (D3). Public surface via explicit `from .x import Y as Y` re-exports
(`.claude/rules/modules.md` — never `__all__`)."""

from src.api.v1.build_sessions.deps import redis_dependency as redis_dependency
from src.api.v1.build_sessions.deps import require_csrf as require_csrf
from src.api.v1.build_sessions.deps import run_build_dependency as run_build_dependency
from src.api.v1.build_sessions.deps import sandbox_dependency as sandbox_dependency
from src.api.v1.build_sessions.deps import (
    session_manager_dependency as session_manager_dependency,
)
from src.api.v1.build_sessions.router import router as router
from src.api.v1.build_sessions.schemas import BillingSessionFactory as BillingSessionFactory
from src.api.v1.build_sessions.schemas import BuildError as BuildError
from src.api.v1.build_sessions.schemas import BuildResult as BuildResult
from src.api.v1.build_sessions.schemas import BuildSessionStatus as BuildSessionStatus
from src.api.v1.build_sessions.schemas import (
    BuildSessionStatusResponse as BuildSessionStatusResponse,
)
from src.api.v1.build_sessions.schemas import EndedEvent as EndedEvent
from src.api.v1.build_sessions.schemas import ErrorEvent as ErrorEvent
from src.api.v1.build_sessions.schemas import ErrorSource as ErrorSource
from src.api.v1.build_sessions.schemas import EscalationEvent as EscalationEvent
from src.api.v1.build_sessions.schemas import ForceEndResponse as ForceEndResponse
from src.api.v1.build_sessions.schemas import HeartbeatResponse as HeartbeatResponse
from src.api.v1.build_sessions.schemas import LockReleaseResponse as LockReleaseResponse
from src.api.v1.build_sessions.schemas import LockStateResponse as LockStateResponse
from src.api.v1.build_sessions.schemas import LogEvent as LogEvent
from src.api.v1.build_sessions.schemas import PreviewReadyEvent as PreviewReadyEvent
from src.api.v1.build_sessions.schemas import ProgressEnvelope as ProgressEnvelope
from src.api.v1.build_sessions.schemas import ProgressSink as ProgressSink
from src.api.v1.build_sessions.schemas import QuotaExceededEvent as QuotaExceededEvent
from src.api.v1.build_sessions.schemas import RunBuild as RunBuild
from src.api.v1.build_sessions.schemas import StartBuildRequest as StartBuildRequest
from src.api.v1.build_sessions.schemas import StartBuildResponse as StartBuildResponse
from src.api.v1.build_sessions.schemas import StepEvent as StepEvent
from src.api.v1.build_sessions.schemas import StopBuildRequest as StopBuildRequest
from src.api.v1.build_sessions.schemas import StopBuildResponse as StopBuildResponse
