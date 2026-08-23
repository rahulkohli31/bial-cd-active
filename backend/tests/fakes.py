"""Shared test doubles.

`FakeStorage` is a dict-backed `ObjectStorage` so attachment upload/download/delete,
conversation delete-sweeps, and the C4 snapshot round-trip run without Azurite.

`FakeSandboxClient` is a canned C2 `SandboxClient` (the mock helper C1) and `FakeBrain`
is a scripted mock C7 `run_build` — together they let SESSION-API's reaper + SessionManager
+ router tests run without a live container, real ACA, or Track BRAIN.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Awaitable, Callable, Mapping
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
    CompileReport,
    CompileState,
    DevLogs,
    DevStatus,
    ExecResult,
    FileOp,
    FileResult,
    FleetMember,
    SandboxClient,
    SandboxGoneError,
    SandboxHandle,
    ServedPage,
)
from src.services.storage.base import ListPage, ObjectMeta, ObjectStorage
from src.services.storage.errors import StorageNotFoundError

# U6's baseline-identity probe, as a fake container answers it. Matched on a fragment of the real
# script rather than on the whole thing: the script is a private constant whose wording is allowed
# to change, and a fake that string-matched all of it would go quietly inert the first time it did
# — answering the generic empty result, which parses as `UNANSWERABLE`.
_BASELINE_MARKER = "git rev-list --max-parents=0"

BASELINE_ROOT_SHA = "0" * 40
"""The root commit a fake container reports — the `bial: golden template baseline`."""

BASELINE_TEMPLATE_BLOB = "1" * 40
"""The blob the root commit stored at `app/page.tsx`."""

BASELINE_DIVERGED_STDOUT = f"{BASELINE_ROOT_SHA}@@{BASELINE_TEMPLATE_BLOB}@@{'2' * 40}"
"""A BUILT app: one root commit, and a root route the agent has since rewritten."""

BASELINE_UNTOUCHED_STDOUT = (
    f"{BASELINE_ROOT_SHA}@@{BASELINE_TEMPLATE_BLOB}@@{BASELINE_TEMPLATE_BLOB}"
)
"""THE 2026-08-18 SHAPE: every server-side check green, and `app/page.tsx` byte-identical to the
golden template the workspace was born with."""


def a_git_bundle(sha: str = "a" * 40) -> bytes:
    """Bytes that survive `parse_bundle_head_sha`.

    Use this for any snapshot a test expects to be RESTORED. `restore_from_snapshot` validates
    the header before it tears the live container down, so dummy bytes like `b"BUNDLE"` now
    fail the gate rather than sailing through to a `git fetch` that fails inside a container
    which no longer has anything to fall back to. Tests that only assert a blob's
    presence/absence (governance sweeps, project delete) do not need this."""
    return b"# v2 git bundle\n" + sha.encode() + b" HEAD\n\nPACKDATA"


def a_sandbox_name(marker: str = "x") -> str:
    """A container name the platform could actually have MINTED, carrying a readable marker.

    `manager.app_name_for` emits `sbx-` + exactly 28 lowercase hex characters and nothing else.
    Fixtures across this suite used to say `"sbx-x"`, `"sbx-stale"`, `"sbx-ghost"` — none of which
    any code path can produce — and that is precisely what let a missing name guard on the ARM
    delete path go unnoticed: `reap_user` handed whatever the registry said straight to a delete,
    including `""`, and a suite whose every name is unreal cannot notice that nothing checks them.

    The marker is hex-encoded and padded, so names stay distinct and a failure message still says
    which fixture it came from."""
    return "sbx-" + (marker.encode().hex() + "0" * 28)[:28]


def a_fleet_member(
    name: str,
    *,
    tags: Mapping[str, str] | None = None,
    running_status: str | None = "Running",
    fqdn: str | None = None,
    arm_created_at: datetime | None = None,
) -> FleetMember:
    """One container as `list_sandbox_fleet` projects it (U9).

    Shared rather than re-declared per test file so every fleet fake agrees on the shape, and so
    a field added to the projection turns up in one place instead of six. The default is the
    UNTAGGED container — no identity at all — because that is the population this whole system
    exists to collect, and a helper whose default was a fully-identified sandbox would quietly
    make the interesting case the one nobody wrote."""
    return FleetMember(
        name=name,
        tags=dict(tags or {}),
        running_status=running_status,
        fqdn=fqdn if fqdn is not None else f"{name}.example.azurecontainerapps.io",
        arm_created_at=arm_created_at,
    )


class FakeStorage(ObjectStorage):
    """A dict-backed `ObjectStorage` — just enough of the ABC for the attachment routes."""

    def __init__(self) -> None:
        super().__init__(provider="fake")
        self.objects: dict[str, bytes] = {}
        # Per-key `last_modified`. Set by `put` (see there) and overridable: a test that AGES a
        # blob past the reconciler's grace still assigns `mtimes[key] = <datetime>` explicitly
        # after writing, and a test that seeds `objects[key]` directly leaves it unset, so
        # `head()` reports `last_modified=None` exactly as before.
        self.mtimes: dict[str, datetime] = {}
        # User metadata per key, mirroring what Azure returns from `head` — the recovery
        # comparison identifies a TREE by the sha stamped here, not by its age.
        self.meta: dict[str, dict[str, str]] = {}
        # A monotonic stand-in for the store's clock, so two writes in one tick still order.
        self._clock = datetime(2026, 1, 1, tzinfo=UTC)

    async def put(self, key, data, *, content_type=None, metadata=None):
        self.objects[key] = data
        self.meta[key] = dict(metadata) if metadata else {}
        # Stamp a write time, because the real store does and something now DEPENDS on it:
        # "is the recovery copy newer than the saved one" is answered by comparing the two
        # blobs' `last_modified`. A fake that left this None would make that comparison read
        # "cannot tell" in every test, and the branch would never be exercised. Monotonic per
        # call so two writes in the same tick still order.
        self._clock += timedelta(microseconds=1)
        self.mtimes[key] = self._clock
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
            metadata=self.meta.get(key, {}),
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
        self.restored_from: list[str | None] = []
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
        # The R17/R18 compile signal this container reports, and how often it was asked.
        self.compile_report: CompileReport = CompileReport(
            state=CompileState.UNKNOWN, reason="endpoint_absent"
        )
        self.compile_polls = 0
        # U6/R9 — what the app's own root answers, and every URL that was asked. `None` scripts
        # the probe that could not reach the app at all, which is an INDETERMINATE input.
        self.served_page: ServedPage | None = ServedPage(
            status=200, head="<!DOCTYPE html><html><body>an app</body></html>"
        )
        self.served_probes: list[str] = []

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

        DELIBERATELY NOT MODELLED: a check that the registry's `app_name` matches
        `attach_handle`. The real client has no such concept — it BUILDS the handle from the
        registry rather than comparing against one it was handed — so a fake that refuses on a
        mismatch invents a `SandboxGoneError` production never raises. That is not a harmless
        extra strictness: `_refuse_if_reclaim_would_destroy_work` reads a confirmed-gone
        container as "nothing to lose" and reclaims silently, which is precisely the #83 bug.
        A double that is stricter than the real thing hides bugs just as effectively as one
        that is laxer.
        """
        reg = await get_redis().hgetall(registry_key(uuid.UUID(user_id)))
        if reg and reg.get(REGISTRY_FIELD_STATE) == REGISTRY_STATE_ENDING:
            raise SandboxGoneError("sandbox is ending")
        if self.attach_handle is None:
            raise SandboxGoneError("no live sandbox for user")
        return self.attach_handle

    async def restore_from_snapshot(
        self,
        user_id: str,
        app_name: str,
        *,
        app_env: dict[str, str],
        source_key: str | None = None,
    ) -> SandboxHandle:
        self.restored.append(app_name)
        # Which bundle a restore PULLED is the whole question for the recovery flow, so record
        # it — `restored` only says a restore happened, never from what.
        self.restored_from.append(source_key)
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
        if len(cmd) == 3 and cmd[0] == "sh" and _BASELINE_MARKER in cmd[2]:
            # U6's baseline-identity probe. The default is a BUILT app — one root commit, and a
            # root route whose blob no longer matches the one the baseline stored — for the same
            # reason the `base64` arm below exists: the realistic answer, not the empty one.
            #
            # AND IT IS THE NON-ACCUSING DEFAULT ON PURPOSE. An empty stdout parses as
            # `UNANSWERABLE`, so every test that happens to switch the content check on without
            # scripting `exec` would silently start exercising the INDETERMINATE retry path —
            # slow, and asserting something other than what it says. A test that wants the app to
            # still be the starter page says so by overriding `exec_handler`.
            return ExecResult(stdout=BASELINE_DIVERGED_STDOUT, stderr="", exit=0)
        if cmd[:1] == ["base64"]:
            # `write_snapshot` reads its bundle back through `base64 <file>` and now validates
            # the bytes before uploading them, so an empty default stdout would decode to b""
            # and fail the gate on every path that snapshots. A real container answers this
            # command with an actual bundle; the fake should too. Tests that care about the
            # CONTENT still override `exec_handler`.
            return ExecResult(stdout=base64.b64encode(a_git_bundle()).decode(), stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        return FileResult(ok=True, detail={})

    async def dev_start(
        self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
    ) -> int:
        return 4321

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        return DevStatus(running=True, ready=True, port=3000)

    async def compile_state(self, handle: SandboxHandle) -> CompileReport:
        """The R17/R18 compile signal, scripted per test.

        The DEFAULT is `UNKNOWN`, not `CLEAN`, and that is a deliberate copy of production
        rather than laziness: an existing container answers 404 here until it is next
        provisioned from an image carrying `/dev/compile`, so `UNKNOWN` is what the whole live
        fleet says. A fake that defaulted to `CLEAN` would make every turn test assert against
        a state most real containers cannot produce."""
        self.compile_polls += 1
        return self.compile_report

    async def what_is_it_serving(self, handle: SandboxHandle) -> ServedPage | None:
        """U6's serving probe — the health verdict's own GET at the app root.

        SEPARATE FROM `warm_status` even though production makes one request for both jobs,
        because the two are asserted for opposite reasons: `warmed` answers "was the first route
        paid for before the frame went out", and this answers "what did the app say when we
        decided whether to let it claim it finished". A test scripting one must not silently move
        the other."""
        self.served_probes.append(handle.preview_url)
        return self.served_page

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
