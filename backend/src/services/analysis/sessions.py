"""The Azure dynamic-sessions data-plane client behind `AnalysisRuntime`.

Each operation is one HTTPS call to the pool's management endpoint, carrying the chat's session
identifier and a token for the backend's own identity. Every non-2xx answer, 429 and 5xx included,
and every transport or token failure is `AnalysisUnavailableError`; a call that outlives its
deadline is `AnalysisTimedOutError`. An execution answers 200 whatever the code did, so its
outcome is read from the body.

Logs carry the operation, the conversation id, the status and the duration. Never code, output,
or a file name: those are the citizen's document, or the model's reading of it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final
from urllib.parse import quote

import httpx
import structlog
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

from src.services.analysis.config import AnalysisConfig
from src.services.analysis.runtime import (
    AnalysisTimedOutError,
    AnalysisUnavailableError,
    Execution,
    SessionFile,
)

_log = structlog.get_logger()

# An unknown version answers 401 with an empty body, so a wrong pin reads as an auth failure.
API_VERSION: Final = "2025-10-02-preview"
TOKEN_SCOPE: Final = "https://dynamicsessions.io/.default"

_OP_TIMEOUT_S: Final = 30.0
# A 30 MiB file over a slow link, with room to spare.
_UPLOAD_TIMEOUT_S: Final = 120.0
# Past the execution's own deadline, so the service answers before the transport gives up.
_RUN_TIMEOUT_BUFFER_S: Final = 15.0
# The service cuts each stream here without a marker; above the cap the caller applies itself.
_OUTPUT_STREAM_LIMIT: Final = 20_000

# The service's own words in a failed execution's stderr.
_SERVER_TIMED_OUT: Final = "Request timed out waiting for code execution to complete"
_ABORTED: Final = "Execution aborted"
_KERNEL_RESTARTED: Final = "Kernel restarted"

TokenProvider = Callable[[], Awaitable[str]]


def _conversation_of(session_id: str) -> str:
    return str(uuid.UUID(hex=session_id))


def _execution(body: Any) -> Execution:
    """An execution's answer, read off its body. Raises for the two outcomes that are not the
    code's own: the service's deadline, and a call aborted behind one."""
    succeeded = body["status"] == "Succeeded"
    result = body.get("result") or {}
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    if not succeeded and stderr.startswith(_SERVER_TIMED_OUT):
        raise AnalysisTimedOutError("run: the service's deadline passed")
    if not succeeded and stderr.startswith(_ABORTED):
        raise AnalysisUnavailableError("run: aborted")
    return Execution(
        succeeded=succeeded,
        stdout=stdout,
        stderr=stderr,
        out_of_memory=not succeeded
        and (stderr.startswith(_KERNEL_RESTARTED) or "MemoryError" in stderr),
    )


class DynamicSessionsRuntime:
    """One long-lived HTTP pool and one credential for the life of the process."""

    def __init__(
        self,
        config: AnalysisConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        token: TokenProvider | None = None,
    ) -> None:
        self._endpoint = config.pool_endpoint.rstrip("/")
        self._http = httpx.AsyncClient(transport=transport, timeout=None)
        self._credential: DefaultAzureCredential | None = None
        if token is None:
            self._credential = DefaultAzureCredential()
            token = get_bearer_token_provider(self._credential, TOKEN_SCOPE)
        self._token = token

    async def _call(
        self,
        operation: str,
        method: str,
        path: str,
        session_id: str,
        *,
        timeout_s: float,
        json: Mapping[str, Any] | None = None,
        files: Mapping[str, tuple[str, bytes]] | None = None,
        absent_is_done: bool = False,
    ) -> httpx.Response:
        started = time.monotonic()
        status: int | None = None
        try:
            if self._http.is_closed:
                raise AnalysisUnavailableError(f"{operation}: client closed")
            try:
                bearer = await self._token()
            except Exception as exc:
                _log.warning(
                    "analysis_token_failed", operation=operation, error=type(exc).__name__
                )
                raise AnalysisUnavailableError(f"{operation}: no token") from None
            try:
                response = await self._http.request(
                    method,
                    f"{self._endpoint}/{path}",
                    params={"api-version": API_VERSION, "identifier": session_id},
                    headers={"Authorization": f"Bearer {bearer}"},
                    json=json,
                    files=files,
                    timeout=httpx.Timeout(timeout_s, connect=10.0),
                )
            except httpx.TimeoutException:
                raise AnalysisTimedOutError(f"{operation}: no answer in {timeout_s}s") from None
            except httpx.HTTPError as exc:
                raise AnalysisUnavailableError(f"{operation}: {type(exc).__name__}") from None
            status = response.status_code
            if not response.is_success and not (absent_is_done and status == 404):
                raise AnalysisUnavailableError(f"{operation}: answered {status}")
            return response
        finally:
            _log.info(
                "analysis_call",
                operation=operation,
                conversation_id=_conversation_of(session_id),
                status=status,
                duration_ms=round((time.monotonic() - started) * 1000),
            )

    async def list_files(self, session_id: str) -> list[SessionFile]:
        response = await self._call(
            "list_files", "GET", "files", session_id, timeout_s=_OP_TIMEOUT_S
        )
        try:
            return [
                SessionFile(
                    name=str(entry["name"]),
                    size=int(entry["sizeInBytes"]),
                    modified=entry.get("lastModifiedAt"),
                )
                for entry in response.json()["value"]
                if entry.get("type") == "file" and entry.get("directory", ".") == "."
            ]
        except ValueError, KeyError, TypeError:
            raise AnalysisUnavailableError("list_files: unreadable answer") from None

    async def upload_file(self, session_id: str, name: str, data: bytes) -> None:
        await self._call(
            "upload_file",
            "POST",
            "files",
            session_id,
            timeout_s=_UPLOAD_TIMEOUT_S,
            files={"file": (name, data)},
        )

    async def delete_file(self, session_id: str, name: str) -> None:
        await self._call(
            "delete_file",
            "DELETE",
            f"files/{quote(name, safe='')}",
            session_id,
            timeout_s=_OP_TIMEOUT_S,
            absent_is_done=True,
        )

    async def run(self, session_id: str, code: str, *, timeout_s: float) -> Execution:
        response = await self._call(
            "run",
            "POST",
            "executions",
            session_id,
            timeout_s=timeout_s + _RUN_TIMEOUT_BUFFER_S,
            json={
                "codeInputType": "Inline",
                "executionType": "Synchronous",
                "code": code,
                "timeoutInSeconds": round(timeout_s),
                "outputStreamsMaxLength": _OUTPUT_STREAM_LIMIT,
            },
        )
        try:
            body = response.json()
        except ValueError:
            raise AnalysisUnavailableError("run: unreadable answer") from None
        try:
            return _execution(body)
        except KeyError, TypeError, AttributeError:
            raise AnalysisUnavailableError("run: unreadable answer") from None

    async def delete_session(self, session_id: str) -> None:
        await self._call(
            "delete_session", "DELETE", "session", session_id, timeout_s=_OP_TIMEOUT_S
        )

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._credential is not None:
            await self._credential.close()
