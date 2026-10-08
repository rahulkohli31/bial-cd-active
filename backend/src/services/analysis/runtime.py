"""The seam BIAL Chat's file analysis talks to: five calls against one chat's session.

A session is named by its chat alone (`session_identifier`), so nothing a chat does can reach
another chat's files. Every failure is one of two named errors; callers turn them into fixed
results and never into a crash of the reply.

The accessor answers `None` when `ANALYSIS__*` is unset. Only the backend's identity can call the
pool, so the identifier is a name, not a capability.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

SESSION_FILES_DIR = "/mnt/data"
"""Where an uploaded file lands inside a session, and the interpreter's working directory."""

READER_NAME = "read_attachment.py"
"""The reader's name in the session. Uploaded on every placement, so a copy that code in the
session altered is replaced before the reply reads anything."""


class AnalysisUnavailableError(RuntimeError):
    """The session could not be reached or refused the call: any non-2xx, a transport error, or a
    token that could not be had. Never carries document content, code or a file name."""


class AnalysisTimedOutError(RuntimeError):
    """A call outlived its deadline. For an execution, the code may still be running."""


@dataclass(frozen=True, slots=True)
class SessionFile:
    """One file as the session lists it."""

    name: str
    size: int
    #: As the service reports it, compared only for equality; `None` when it reports none.
    modified: str | None = None


@dataclass(frozen=True, slots=True)
class Execution:
    """What one run of code produced. `succeeded` is false when the code raised;
    `out_of_memory` when it ran out, and the session lost its variables with it."""

    succeeded: bool
    stdout: str
    stderr: str
    out_of_memory: bool = False


class AnalysisRuntime(Protocol):
    """One session per identifier, created by the service on first use."""

    async def list_files(self, session_id: str) -> list[SessionFile]: ...

    async def upload_file(self, session_id: str, name: str, data: bytes) -> None: ...

    async def delete_file(self, session_id: str, name: str) -> None: ...

    async def run(self, session_id: str, code: str, *, timeout_s: float) -> Execution: ...

    async def delete_session(self, session_id: str) -> None: ...

    async def aclose(self) -> None: ...


def session_identifier(conversation_id: uuid.UUID) -> str:
    """The chat's session name: its id as 32 lowercase hex characters, which the service accepts
    and no URL needs escaped."""
    return conversation_id.hex


_runtime_singleton: AnalysisRuntime | None = None


def get_analysis_runtime() -> AnalysisRuntime | None:
    """The configured runtime, built once, or `None` when the analysis block is unset."""
    global _runtime_singleton
    if _runtime_singleton is None:
        from src.config import settings  # lazy: settings imports this package's config

        if settings.analysis is None:
            return None
        from src.services.analysis.sessions import DynamicSessionsRuntime

        _runtime_singleton = DynamicSessionsRuntime(settings.analysis)
    return _runtime_singleton


async def aclose_analysis() -> None:
    """Close the runtime's connection pool and credential. A no-op when none was built."""
    global _runtime_singleton
    runtime, _runtime_singleton = _runtime_singleton, None
    if runtime is not None:
        await runtime.aclose()
