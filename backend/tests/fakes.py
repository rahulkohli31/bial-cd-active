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
from datetime import UTC, datetime, timedelta
from typing import Literal

from src.api.v1.build_sessions.schemas import (
    BuildResult,
    BuildSessionStatus,
    LogEvent,
    PreviewReadyEvent,
    ProgressEnvelope,
    StepEvent,
)
from src.services.redis import (
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    get_redis,
    registry_key,
)
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
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


def a_git_bundle(sha: str = "a" * 40) -> bytes:
    """Bytes that survive `parse_bundle_head_sha`.

    Use this for any snapshot a test expects to be RESTORED. `restore_from_snapshot` validates
    the header before it tears the live container down, so dummy bytes like `b"BUNDLE"` now
    fail the gate rather than sailing through to a `git fetch` that fails inside a container
    which no longer has anything to fall back to. Tests that only assert a blob's
    presence/absence (governance sweeps, project delete) do not need this."""
    return b"# v2 git bundle\n" + sha.encode() + b" HEAD\n\nPACKDATA"


class FakeStorage(ObjectStorage):
    """A dict-backed `ObjectStorage` — just enough of the ABC for the attachment routes."""

    def __init__(self) -> None:
        super().__init__(provider="fake")
        self.objects: dict[str, bytes] = {}
        # Per-key `last_modified` for the reconciler's grace check. Defaults to None (unset), so
        # `head()` returns `last_modified=None` exactly as before for every test that ignores it —
        # a test that AGES a blob past the grace sets `mtimes[key] = <datetime>` explicitly.
        self.mtimes: dict[str, datetime] = {}

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
            key=key,
            size=len(data),
            content_type=None,
            etag=None,
            last_modified=self.mtimes.get(key),
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


async def _hydrate_registry(user_id: str, handle: SandboxHandle) -> None:
    """The one real-client side effect a canned fake must not omit: `_provision_container`
    writes the C5 registry hash at container-create, for BOTH `provision_new` and
    `restore_from_snapshot` (`services/sandbox/client.py`).

    Load-bearing, not cosmetic. The relaunched-preview lease (#43) is a field ON that hash
    and `grant_stay_of_execution` is guarded on the hash EXISTING — so a fake that never
    writes it makes every lease assertion silently vacuous (the grant is skipped, the field
    is absent, and a test asserting "spared" passes for the wrong reason). Same reason the
    reaper's teardown target has to be discoverable: a registry the sweep cannot see is a
    container nobody can reap."""
    await get_redis().hset(
        registry_key(uuid.UUID(user_id)),
        mapping={
            REGISTRY_FIELD_APP_NAME: handle.app_name,
            REGISTRY_FIELD_FQDN: handle.fqdn,
            # A reference, never the raw token — mirrors the real client's C5 contract.
            REGISTRY_FIELD_TOKEN_REF: f"ref-{handle.app_name}",
            REGISTRY_FIELD_CREATED_AT: datetime.now(UTC).isoformat(),
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )


class FakeSandboxClient(SandboxClient):
    """A canned C2 client (mock helper C1). Records provision/restore/teardown calls,
    hydrates the C5 registry hash exactly as the real client does (see
    `_hydrate_registry`), honors teardown idempotency + the typed `SandboxGoneError`, and
    lets tests script `exec` (e.g. a base64 bundle read for the C4 snapshot)."""

    def __init__(self) -> None:
        self.provisioned: list[str] = []
        self.restored: list[str] = []
        self.torn_down: list[str] = []
        # The env dict each BIRTH arm actually handed the container. Recorded separately from
        # the names because a container gets its env exactly once, at birth (KTD-3) — "was the
        # SAS / the per-project DSN injected on THIS arm" is only answerable here, and the
        # attach arm's `None` is itself the assertion that it forwards no env.
        self.provision_env: dict[str, str] | None = None
        self.restore_env: dict[str, str] | None = None
        # attach returns this handle when set; otherwise raises SandboxGoneError (the
        # default "no live sandbox" so the caller provisions).
        self.attach_handle: SandboxHandle | None = None
        self.teardown_error: Exception | None = None
        # Optional per-command exec script; defaults to a clean exit-0 result.
        self.exec_handler: Callable[[list[str]], ExecResult] | None = None
        # The U3 warm requests this client was asked for, and the status each one answers with.
        self.warmed: list[str] = []
        self.warm_status: int | None = 200

    async def provision_new(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        self.provisioned.append(app_name)
        self.provision_env = dict(app_env)
        handle = _fake_handle(app_name)
        await _hydrate_registry(user_id, handle)
        return handle

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
        """Mirrors the real client's TWO refusals, not just the obvious one.

        The `ending` guard (`services/sandbox/client.py`) matters more than it looks: `reap_user`
        marks the registry `ending` BEFORE it tears down, so the real client refuses a container
        the reaper has already committed to destroying. A fake without that guard happily
        attaches to it, which makes reap-ordering bugs invisible and makes code paths look
        reachable that production refuses outright — it already cost one investigation a false
        positive.

        The `user_id` check is the same class of divergence: returning `attach_handle` to ANY
        caller means a test can never catch attaching to the wrong user's container, which in a
        single-tenant system scoped entirely by `user_id` (ADR-0004) is the leak that matters.
        """
        reg = await get_redis().hgetall(registry_key(uuid.UUID(user_id)))
        if reg and reg.get(REGISTRY_FIELD_STATE) == REGISTRY_STATE_ENDING:
            raise SandboxGoneError("sandbox is ending")
        if self.attach_handle is None:
            raise SandboxGoneError("no live sandbox for user")
        # When a registry exists it names WHOSE container this is; refuse a mismatch rather
        # than handing back a handle to somebody else's app.
        if reg and reg.get(REGISTRY_FIELD_APP_NAME) != self.attach_handle.app_name:
            raise SandboxGoneError("no live sandbox for user")
        return self.attach_handle

    async def restore_from_snapshot(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        self.restored.append(app_name)
        self.restore_env = dict(app_env)
        handle = _fake_handle(app_name)
        await _hydrate_registry(user_id, handle)
        return handle

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

    async def someone_has_to_go_first(self, handle: SandboxHandle) -> int | None:
        """The U3 warm request. Recorded rather than performed — the real one is a live GET at
        the app root, and "was the first route paid for before the frame went out" is only
        answerable by counting. `warm_status` scripts the answer (a 500 is a compile error, and
        the frame must still go out)."""
        self.warmed.append(handle.preview_url)
        return self.warm_status

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
