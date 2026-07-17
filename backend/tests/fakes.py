"""Shared test doubles.

`FakeStorage` is a dict-backed `ObjectStorage` so attachment upload/download/delete,
conversation delete-sweeps, and the C4 snapshot round-trip run without Azurite.

`FakeSandboxClient` is a canned C2 `SandboxClient` (the mock helper C1) and `FakeBrain`
is a scripted mock C7 `run_build` — together they let SESSION-API's reaper + SessionManager
+ router tests run without a live container, real ACA, or Track BRAIN.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Literal

from src.api.v1.build_sessions.schemas import (
    BuildResult,
    BuildSessionStatus,
    LogEvent,
    PreviewReadyEvent,
    ProgressEnvelope,
    StepEvent,
)
from src.services.sandbox.base import (
    DevLogs,
    DevStatus,
    ExecResult,
    FileOp,
    FileResult,
    SandboxClient,
    SandboxGoneError,
    SandboxHandle,
)
from src.services.storage.base import ListPage, ObjectMeta, ObjectStorage
from src.services.storage.errors import StorageNotFoundError


class FakeStorage(ObjectStorage):
    """A dict-backed `ObjectStorage` — just enough of the ABC for the attachment routes."""

    def __init__(self) -> None:
        super().__init__(provider="fake")
        self.objects: dict[str, bytes] = {}

    async def put(self, key, data, *, content_type=None, metadata=None):
        self.objects[key] = data
        return ObjectMeta(
            key=key, size=len(data), content_type=content_type, etag=None, last_modified=None
        )

    async def get(self, key):
        if key not in self.objects:
            raise StorageNotFoundError("object not found", provider="fake", key=key)
        return self.objects[key]

    async def head(self, key):
        data = self.objects.get(key)
        if data is None:
            return None
        return ObjectMeta(
            key=key, size=len(data), content_type=None, etag=None, last_modified=None
        )

    async def delete(self, key):
        self.objects.pop(key, None)

    async def list(self, prefix, *, page_size=1000, token=None):
        # Real pagination (sorted keys, offset token) so callers' next_token walks are
        # actually exercised — a fake that returns everything in one page would let a
        # single-page listing bug pass silently (R23).
        matching = sorted(k for k in self.objects if k.startswith(prefix))
        start = int(token) if token else 0
        page = matching[start : start + page_size]
        next_start = start + page_size
        return ListPage(
            keys=tuple(page),
            next_token=str(next_start) if next_start < len(matching) else None,
        )

    async def _signed_read_url_impl(self, key, *, expires_in: timedelta):
        return f"https://fake.local/{key}"

    async def aclose(self):
        return None


def _fake_handle(app_name: str) -> SandboxHandle:
    fqdn = f"{app_name}.westeurope.azurecontainerapps.io"
    return SandboxHandle(
        fqdn=fqdn,
        token=f"tok-{app_name}",
        app_name=app_name,
        preview_url=f"https://{fqdn}/",
        ready=False,
    )


class FakeSandboxClient(SandboxClient):
    """A canned C2 client (mock helper C1). Records provision/restore/teardown calls,
    honors teardown idempotency + the typed `SandboxGoneError`, and lets tests script
    `exec` (e.g. a base64 bundle read for the C4 snapshot)."""

    def __init__(self) -> None:
        self.provisioned: list[str] = []
        self.restored: list[str] = []
        self.torn_down: list[str] = []
        # attach returns this handle when set; otherwise raises SandboxGoneError (the
        # default "no live sandbox" so the caller provisions).
        self.attach_handle: SandboxHandle | None = None
        self.teardown_error: Exception | None = None
        # Optional per-command exec script; defaults to a clean exit-0 result.
        self.exec_handler: Callable[[list[str]], ExecResult] | None = None

    async def provision_new(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        self.provisioned.append(app_name)
        return _fake_handle(app_name)

    async def wait_ready(
        self, handle: SandboxHandle, *, timeout_s: float = 120.0
    ) -> SandboxHandle:
        return SandboxHandle(
            fqdn=handle.fqdn,
            token=handle.token,
            app_name=handle.app_name,
            preview_url=handle.preview_url,
            ready=True,
        )

    async def attach_existing(self, user_id: str) -> SandboxHandle:
        if self.attach_handle is None:
            raise SandboxGoneError("no live sandbox for user")
        return self.attach_handle

    async def restore_from_snapshot(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        self.restored.append(app_name)
        return _fake_handle(app_name)

    async def exec(
        self,
        handle: SandboxHandle,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout_s: int = 900,
    ) -> ExecResult:
        if self.exec_handler is not None:
            return self.exec_handler(cmd)
        return ExecResult(stdout="", stderr="", exit=0)

    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        return FileResult(ok=True, detail={})

    async def dev_start(
        self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
    ) -> int:
        return 4321

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        return DevStatus(running=True, ready=True, port=3000)

    async def dev_logs(self, handle: SandboxHandle, *, since: int = 0) -> DevLogs:
        return DevLogs(lines=[], next_cursor=since)

    async def teardown(self, handle: SandboxHandle) -> None:
        if self.teardown_error is not None:
            raise self.teardown_error
        self.torn_down.append(handle.app_name)


ProgressSinkFn = Callable[[ProgressEnvelope], Awaitable[None]]


class FakeBrain:
    """A scripted mock C7 `run_build`: emits step → log → preview_ready via `on_progress`,
    then RETURNS its verdict as a `BuildResult`.

    It emits no terminal `ended` because real BRAIN cannot (R7): the frame is SESSION-API's,
    rendered from the returned verdict after the C4 snapshot. A fake that emitted one would
    mask that seam — the manager would look correct while never exercising its own emission.
    `raise_before_ended` scripts the abnormal path where BRAIN dies with no verdict at all."""

    def __init__(
        self,
        *,
        raise_before_ended: bool = False,
        reason: str = "completed",
        status: Literal[
            BuildSessionStatus.ENDED, BuildSessionStatus.FAILED
        ] = BuildSessionStatus.ENDED,
        preview_url: str = "https://preview.example/",
        app_id: uuid.UUID | None = None,
    ) -> None:
        self.raise_before_ended = raise_before_ended
        self.reason = reason
        # Annotated so the terminal narrowing survives the attribute assignment (pyright
        # widens to the bare enum otherwise, and `BuildResult.status` only takes the two).
        self.status: Literal[BuildSessionStatus.ENDED, BuildSessionStatus.FAILED] = status
        self.preview_url = preview_url
        self.app_id = app_id or uuid.uuid4()

    async def __call__(
        self,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        sandbox_client: SandboxClient,
        on_progress: ProgressSinkFn,
    ) -> BuildResult:
        await on_progress(
            StepEvent(seq=1, name="scaffold", label="Scaffolding the app", state="started")
        )
        await on_progress(
            LogEvent(seq=2, source="exec", stream="stdout", text="installing dependencies")
        )
        await on_progress(PreviewReadyEvent(seq=3, preview_url=self.preview_url))
        if self.raise_before_ended:
            raise RuntimeError("brain blew up mid-build")
        return BuildResult(
            status=self.status,
            reason=self.reason,
            app_id=self.app_id,
            preview_url=self.preview_url,
            last_seq=3,
            snapshot_committed=False,
        )
