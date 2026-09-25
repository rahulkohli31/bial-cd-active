"""Shared test doubles.

`FakeStorage` is a dict-backed `ObjectStorage` so attachment upload/download/delete,
conversation delete-sweeps, and the snapshot round-trip run without Azurite.

`FakeSandboxClient` is a canned `SandboxClient` (the mock helper) that lets SESSION-API's
reaper + SessionManager + router tests run without a live container or real ACA.

`ToolDeps` and `write_legacy_build_started` at the foot of the file are re-hosted from `src/`:
both were harness-only in production and were deleted with it, and both were the driver for
tests of code that is still live. See the section comment there.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Final, Literal

import sqlalchemy as sa
from pydantic import AnyUrl, TypeAdapter, UrlConstraints, ValidationError
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.core.connectors import CONNECTORS, ConnectedSystem, ResolvedWindow
from src.db.models.conversation import ChatKind, Conversation
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.db.models.project_connector import ConnectorWindowKind
from src.services.build_sessions.manager import SessionManager
from src.services.build_sessions.outcome import (
    FORCE_ENDED,
    IDLE_TEARDOWN,
    QUOTA_EXCEEDED,
    STOPPED_BY_USER,
)
from src.services.messages.store import SeqContentionError, append_batch
from src.services.orchestrator.deps import SandboxSession
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
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_SHARED_OWNER_ID,
    REGISTRY_FIELD_SHARED_PROJECT_ID,
    REGISTRY_FIELD_SHARED_SERVED_COUNT,
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
    SandboxNotReadyError,
    ServedCount,
    ServedPage,
)
from src.services.storage.base import ListPage, ObjectMeta, ObjectStorage
from src.services.storage.errors import StorageNotFoundError

# Matched on a fragment of the real script, not the whole thing: the script is a private
# constant free to reword, and a full-string match would go quietly inert (answering the empty
# result, which parses as UNANSWERABLE) the first time it did.
_BASELINE_MARKER = "git rev-list --max-parents=0"

# A BUILT app, in the four-field shape the real probe emits: one root commit, the template's
# original blob, and a different blob there now. Other shapes' constants live beside the tests
# that drive them in tests/services/orchestrator/fake_sandbox.py — keep this the only copy.
_BASELINE_STDOUT = f"{'0' * 40}@@{'1' * 40}@@{'2' * 40}@@bial: golden template baseline"


_STATE_MARKER = "git rev-list --count HEAD"

# The default answer to the workspace-state probe: a container holding real work, descended from
# whatever HEAD it is asked about — deliberately NOT the empty-stdout default `exec` falls back
# to for an unrecognised command, which `parse_state` reads as a CONFIRMED REVERSION. A fake
# defaulting to that would make every turn test that happens to seed a bundle silently exercise
# the quarantine-and-restore branch while asserting something else.
_STATE_STDOUT = f"{'a' * 40}@@@@4"


def a_git_bundle(sha: str = "a" * 40) -> bytes:
    """Bytes that survive `parse_bundle_head_sha`. Use this for any snapshot a test expects to
    be RESTORED — `restore_from_snapshot` validates the header before tearing the live container
    down, so dummy bytes like `b"BUNDLE"` now fail the gate instead of reaching a `git fetch` with
    nothing left to fall back to. Tests that only check a blob's presence/absence don't need it."""
    return b"# v2 git bundle\n" + sha.encode() + b" HEAD\n\nPACKDATA"


def a_sandbox_name(marker: str = "x") -> str:
    """A container name the platform could actually have MINTED: `manager.app_name_for` emits
    `sbx-` + exactly 28 lowercase hex characters. Fixtures used to say `"sbx-x"` — a shape no
    code path produces — which let a missing name guard on the ARM delete path go unnoticed
    (`reap_user` handed the registry's value straight to a delete, `""` included). Hex-encoded
    and padded so names stay distinct and a failure message still names its fixture."""
    return "sbx-" + (marker.encode().hex() + "0" * 28)[:28]


def a_shared_sandbox_name(marker: str = "x") -> str:
    """The `shr-` sibling of `a_sandbox_name` (#198) — a shape `manager.shr_name_for` could
    actually have minted, for the same reason: a fixture no code path produces would let a
    missing shape guard on the ARM delete path go unnoticed."""
    return "shr-" + (marker.encode().hex() + "0" * 28)[:28]


def a_fleet_member(
    name: str,
    *,
    tags: Mapping[str, str] | None = None,
    running_status: str | None = "Running",
    fqdn: str | None = None,
    arm_created_at: datetime | None = None,
) -> FleetMember:
    """One container as `list_sandbox_fleet` projects it. Shared rather than re-declared per
    test file, so a field added to the projection turns up in one place instead of six. Defaults
    to the UNTAGGED container — no identity at all — since that is the population this system
    exists to collect; a fully-identified default would quietly make the interesting case the
    one nobody wrote."""
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
        # Per-key `last_modified`, set by `put` and overridable: a test that AGES a blob past
        # the reconciler's grace assigns `mtimes[key]` directly after writing.
        self.mtimes: dict[str, datetime] = {}
        # Mirrors what Azure returns from `head` — the recovery comparison identifies a TREE by
        # the sha stamped here, not by its age.
        self.meta: dict[str, dict[str, str]] = {}
        self._clock = datetime(2026, 1, 1, tzinfo=UTC)  # monotonic, so same-tick writes order

    async def put(self, key, data, *, content_type=None, metadata=None):
        self.objects[key] = data
        self.meta[key] = dict(metadata) if metadata else {}
        # "Is the recovery copy newer than the saved one" is answered by comparing two blobs'
        # `last_modified` — a fake leaving this None would make that read "cannot tell" always,
        # and the branch would never be exercised.
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
        # Real pagination so callers' next_token walks are actually exercised — one page for
        # everything would let a single-page listing bug pass silently.
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


async def _hydrate_registry(
    user_id: str,
    handle: SandboxHandle,
    *,
    shared_project_id: uuid.UUID | None = None,
    shared_owner_id: uuid.UUID | None = None,
) -> None:
    """The one real-client side effect a canned fake must not omit: `_provision_container`
    writes the registry hash at container-create, for BOTH `provision_new` and
    `restore_from_snapshot` (`services/sandbox/client.py`). Load-bearing: `grant_stay_of_execution`
    is guarded on this hash EXISTING, so skipping it makes every lease assertion silently
    vacuous, and an undiscoverable registry is a container nobody can reap.

    THE SERVING SENTINEL IS PART OF THAT CONTRACT, and leaving it out would be the same class of
    omission one field further in. An absent `serving_since` is the PRE-CUTOVER reading, which
    the rollout grandfathers as PROVEN — so a fake that skipped it would hand every test in this
    suite a brand-new container the platform reports as ALREADY RUNNING, and the "created but
    never served" arm would be unreachable from any test that provisions through this double.
    Green, and blind to the whole change.

    `shared_project_id`/`shared_owner_id` (#198) mirror the real client's `_write_registry`:
    stamped only when given, `None` on the ordinary `provision_new` arm."""
    key = registry_key(uuid.UUID(user_id))
    await get_redis().hset(
        key,
        mapping={
            REGISTRY_FIELD_APP_NAME: handle.app_name,
            REGISTRY_FIELD_FQDN: handle.fqdn,
            # A reference, never the raw token — mirrors the real client's contract.
            REGISTRY_FIELD_TOKEN_REF: f"ref-{handle.app_name}",
            REGISTRY_FIELD_CREATED_AT: datetime.now(UTC).isoformat(),
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
            # Scheduled, not serving. Only an observer that watched this app answer a request
            # may replace it (`build_sessions/locks.py::mark_serving`).
            REGISTRY_FIELD_SERVING_SINCE: "",
        },
    )
    if shared_project_id is not None:
        await get_redis().hset(
            key,
            mapping={
                REGISTRY_FIELD_SHARED_PROJECT_ID: str(shared_project_id),
                REGISTRY_FIELD_SHARED_OWNER_ID: str(shared_owner_id),
            },
        )
    # `shared_served_count` disowned UNCONDITIONALLY — mirrors the real client's own fix: a
    # high-water mark left behind by a PRIOR occupant of this slot (build sandbox or a
    # replaced shared view) must never be compared against a fresh container's first reading.
    await get_redis().hdel(key, REGISTRY_FIELD_SHARED_SERVED_COUNT)
    if shared_project_id is None:
        await get_redis().hdel(
            key, REGISTRY_FIELD_SHARED_PROJECT_ID, REGISTRY_FIELD_SHARED_OWNER_ID
        )


class FakeSandboxClient(SandboxClient):
    """A canned client (mock helper). Records provision/restore/teardown calls,
    hydrates the registry hash exactly as the real client does (see
    `_hydrate_registry`), honors teardown idempotency + the typed `SandboxGoneError`, and
    lets tests script `exec` (e.g. a base64 bundle read for the snapshot)."""

    def __init__(self) -> None:
        self.provisioned: list[str] = []
        self.restored: list[str] = []
        self.restored_from: list[str | None] = []
        # #198 — the `kind` each restore was called with, parallel to `restored`.
        self.restored_as_kind: list[Literal["build_sandbox", "shared_sandbox"]] = []
        # #198 — the `(shared_project_id, shared_owner_id)` each restore was called with,
        # parallel to `restored_as_kind`.
        self.restored_shared_identity: list[tuple[uuid.UUID | None, uuid.UUID | None]] = []
        self.torn_down: list[str] = []
        # The env dict each BIRTH arm actually handed the container, recorded separately from the
        # names so "was the SAS / the per-project DSN injected on THIS arm" stays answerable. The
        # attach arm leaves these `None`, which is itself the assertion that it forwards no env.
        self.provision_env: dict[str, str] | None = None
        self.restore_env: dict[str, str] | None = None
        # attach returns this handle when set; otherwise raises SandboxGoneError (the
        # default "no live sandbox" so the caller provisions).
        self.attach_handle: SandboxHandle | None = None
        # ATTACH_BY_NAME'S OWN WORLD, independent of the registry and of `attach_handle`: a name
        # absent here means ARM confirms nothing answers to it (`SandboxGoneError`); a name
        # present in `unreachable_by_name` means ARM confirms the container but the supervisor
        # does not (`SandboxNotReadyError`) — the reach failure a caller must never read as
        # absence. `provision_new`/`restore_from_snapshot` register into it and `teardown`
        # retires the entry, mirroring what ARM would actually know regardless of what the
        # per-user registry says.
        self.by_name: dict[str, SandboxHandle] = {}
        self.unreachable_by_name: set[str] = set()
        self.teardown_error: Exception | None = None
        # Optional per-command exec script; defaults to a clean exit-0 result.
        self.exec_handler: Callable[[list[str]], ExecResult] | None = None
        # Every bundle a discard reset the container to, in order.
        self.reset_to: list[bytes] = []
        self.warmed: list[str] = []
        self.warm_status: int | None = 200
        self.compile_report: CompileReport = CompileReport(
            state=CompileState.UNKNOWN, reason="endpoint_absent"
        )
        self.compile_polls = 0
        # What the app's own root answers, and every URL that was asked. `None` scripts
        # the probe that could not reach the app at all, which is an INDETERMINATE input.
        self.served_page: ServedPage | None = ServedPage(
            status=200, head="<!DOCTYPE html><html><body>an app</body></html>"
        )
        self.served_probes: list[str] = []
        # WHAT `/dev/status` SAYS THE APP ROOT ANSWERED WITH, and the default is `None` because
        # that is the reading every assertion written before the page proof existed was made
        # under — not because `None` is neutral. `DevStatus.shows_a_page` reads `None` as the
        # GRANDFATHER arm ("a supervisor image predating the field cannot say, so keep today's
        # behaviour") and answers True, so `None` here means every existing test goes on
        # asserting exactly what it asserted. Moving this default to 200 would change no test's
        # colour and would be the wrong fix anyway: the gate would still never be exercised.
        #
        # AND THAT IS THE GAP THIS KNOB EXISTS TO CLOSE. Until it was added, NO double in this
        # repo ever set the field, so `if not page_is_up` had never once been True in a test on
        # any path — which is how a fix that tore a restored container down over a 404 shipped
        # with a green suite. A test that wants the page-less reading now says
        # `client.root_status = 404` out loud, and one that wants the pre-`root_status` fleet
        # says `None` and means it.
        self.root_status: int | None = None
        # #198 — the supervisor's `/served` count, scripted per test. `None` (the default)
        # scripts the probe that could not answer, same convention as `served_page`.
        self.served_count_value: int | None = None
        # #198 — whether that count is a windowed reading rather than a cumulative total
        # (`ServedCount`'s own docstring). `False` by default: most tests script a small count
        # that is meant to compare as a real total.
        self.served_count_truncated: bool = False

    async def provision_new(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        self.provisioned.append(app_name)
        self.provision_env = dict(app_env)
        handle = _fake_handle(app_name)
        await _hydrate_registry(user_id, handle)
        self.by_name[app_name] = handle
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
        """Mirrors the real client's TWO refusals, not just the obvious one: `reap_user` marks
        the registry `ending` BEFORE it tears down, so the real client refuses a container the
        reaper has already committed to destroying — a fake without that guard makes
        reap-ordering bugs invisible.
        DELIBERATELY NOT MODELLED: a check that the registry's `app_name` matches
        `attach_handle`. The real client BUILDS the handle from the registry and has no such
        concept, so refusing on a mismatch would invent an error production never raises."""
        reg = await get_redis().hgetall(registry_key(uuid.UUID(user_id)))
        if reg and reg.get(REGISTRY_FIELD_STATE) == REGISTRY_STATE_ENDING:
            raise SandboxGoneError("sandbox is ending")
        if self.attach_handle is None:
            raise SandboxGoneError("no live sandbox for user")
        return self.attach_handle

    async def attach_by_name(self, *, app_name: str) -> SandboxHandle:
        """Mirrors the real client's absent-vs-unreachable split, keyed on `by_name` /
        `unreachable_by_name` rather than a registry read — there is none for this path."""
        if app_name not in self.by_name:
            raise SandboxGoneError(f"no container answers to {app_name!r}")
        if app_name in self.unreachable_by_name:
            raise SandboxNotReadyError(f"{app_name!r} exists but the supervisor does not answer")
        return self.by_name[app_name]

    async def restore_from_snapshot(
        self,
        user_id: str,
        app_name: str,
        *,
        app_env: dict[str, str],
        source_key: str | None = None,
        kind: Literal["build_sandbox", "shared_sandbox"] = "build_sandbox",
        shared_project_id: uuid.UUID | None = None,
        shared_owner_id: uuid.UUID | None = None,
    ) -> SandboxHandle:
        self.restored.append(app_name)
        # Which bundle a restore PULLED is the whole question for the recovery flow, so record
        # it — `restored` only says a restore happened, never from what.
        self.restored_from.append(source_key)
        # #198 — which ARM identity this restore would have stamped. The fake tracks no ARM
        # tags at all (see `test_aca.py` for the real client's own tag-stamping coverage), so
        # this is the one place a manager-level test can assert it asked for the right kind.
        self.restored_as_kind.append(kind)
        # #198 — the registry-hash half of that same identity, parallel to `restored_as_kind`.
        self.restored_shared_identity.append((shared_project_id, shared_owner_id))
        self.restore_env = dict(app_env)
        handle = _fake_handle(app_name)
        await _hydrate_registry(
            user_id, handle, shared_project_id=shared_project_id, shared_owner_id=shared_owner_id
        )
        self.by_name[app_name] = handle
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
            # The non-accusing default, on purpose: an empty stdout parses as UNANSWERABLE, so a
            # test that turns the content check on without scripting `exec` would silently start
            # exercising the slow INDETERMINATE retry path instead. A test wanting the starter
            # page overrides `exec_handler` to say so.
            return ExecResult(stdout=_BASELINE_STDOUT, stderr="", exit=0)
        if len(cmd) == 3 and cmd[0] == "sh" and _STATE_MARKER in cmd[2]:
            # The ancestry field answers only when the probe ASKED (`merge-base`) — answering
            # unconditionally would erase `Ancestry.NOT_ASKED`, which exists to keep an unasked
            # question distinguishable from a judgement.
            answered = "0 0" if "merge-base" in cmd[2] else ""
            return ExecResult(stdout=f"{_STATE_STDOUT}@@{answered}", stderr="", exit=0)
        if cmd[:1] == ["base64"]:
            # `write_snapshot` validates the bytes it reads back before uploading, so an empty
            # default would fail that gate on every path that snapshots. Tests that care about
            # the CONTENT still override `exec_handler`.
            return ExecResult(stdout=base64.b64encode(a_git_bundle()).decode(), stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        return FileResult(ok=True, detail={})

    async def reset_to_bundle(self, handle: SandboxHandle, bundle: bytes) -> None:
        self.reset_to.append(bundle)

    async def dev_start(
        self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
    ) -> int:
        return 4321

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        """A dev server that is up and answering. `root_status` rides from the attribute rather
        than being hard-coded here so a test can script what the app ROOT says without
        subclassing — the two facts are separate questions (`ready` is fail-open by the
        supervisor's own design and counts a 404), and a fake that could only say them together
        is a fake no page-gate test can be written against.

        A SUBCLASS THAT OVERRIDES THIS OWNS BOTH FIELDS. Several in this suite do, to script a
        sequence; each one has to pass `root_status` itself, and one that forgets is asserting
        against the grandfather arm whether it meant to or not."""
        return DevStatus(running=True, ready=True, port=3000, root_status=self.root_status)

    async def compile_state(self, handle: SandboxHandle) -> CompileReport:
        """The compile signal, scripted per test. Defaults to `UNKNOWN`, not `CLEAN`: an existing
        container answers 404 here until reprovisioned from an image carrying `/dev/compile`, so
        `UNKNOWN` is what the whole live fleet says — defaulting to `CLEAN` would make every turn
        test assert against a state most real containers cannot produce."""
        self.compile_polls += 1
        return self.compile_report

    async def what_is_it_serving(self, handle: SandboxHandle) -> ServedPage | None:
        """The serving probe — the health verdict's own GET at the app root.

        SEPARATE FROM `warm_status` even though production makes one request for both jobs,
        because the two are asserted for opposite reasons: `warmed` answers "was the first route
        paid for before the frame went out", and this answers "what did the app say when we
        decided whether to let it claim it finished". A test scripting one must not silently move
        the other."""
        self.served_probes.append(handle.preview_url)
        return self.served_page

    async def someone_has_to_go_first(self, handle: SandboxHandle) -> int | None:
        """The warm request. Recorded rather than performed — the real one is a live GET at
        the app root, and "was the first route paid for before the frame went out" is only
        answerable by counting. `warm_status` scripts the answer (a 500 is a compile error, and
        the frame must still go out)."""
        self.warmed.append(handle.preview_url)
        return self.warm_status

    async def served_count(self, handle: SandboxHandle) -> ServedCount | None:
        """#198's shared-runtime traffic signal, scripted per test via `served_count_value`/
        `served_count_truncated`. `None` (the default) is the probe that could not answer, same
        convention as `served_page`."""
        if self.served_count_value is None:
            return None
        return ServedCount(count=self.served_count_value, truncated=self.served_count_truncated)

    async def dev_logs(self, handle: SandboxHandle, *, since: int = 0) -> DevLogs:
        return DevLogs(lines=[], next_cursor=since)

    async def teardown(self, handle: SandboxHandle) -> None:
        if self.teardown_error is not None:
            raise self.teardown_error
        self.torn_down.append(handle.app_name)
        self.by_name.pop(handle.app_name, None)
        self.unreachable_by_name.discard(handle.app_name)


class DevServerDownUntilStarted(FakeSandboxClient):
    """A container whose dev server is down until something starts it.

    The base double reports a serving app on every `dev_status`, so it cannot tell a path that
    starts the app from one that forgets to. A restore is a new container, so it forgets any
    earlier start."""

    def __init__(self) -> None:
        super().__init__()
        self.dev_started: list[str] = []
        self._serving: set[str] = set()

    async def restore_from_snapshot(
        self,
        user_id: str,
        app_name: str,
        *,
        app_env: dict[str, str],
        source_key: str | None = None,
        kind: Literal["build_sandbox", "shared_sandbox"] = "build_sandbox",
        shared_project_id: uuid.UUID | None = None,
        shared_owner_id: uuid.UUID | None = None,
    ) -> SandboxHandle:
        self._serving.discard(app_name)
        return await super().restore_from_snapshot(
            user_id,
            app_name,
            app_env=app_env,
            source_key=source_key,
            kind=kind,
            shared_project_id=shared_project_id,
            shared_owner_id=shared_owner_id,
        )

    async def dev_start(
        self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
    ) -> int:
        self.dev_started.append(handle.app_name)
        self._serving.add(handle.app_name)
        return await super().dev_start(handle, cmd=cmd, cwd=cwd)

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        up = handle.app_name in self._serving
        return DevStatus(running=up, ready=up, port=3000, root_status=200 if up else None)


async def detached_work_done(manager: SessionManager) -> None:
    """Wait out everything the manager has detached — a start's slow half, its watch for a first
    page, a compensation — including whatever those spawn while being waited on."""
    while manager._tasks:
        await asyncio.gather(*list(manager._tasks), return_exceptions=True)


# ── Writers re-hosted from `src/`, where nothing calls them any more ──────────
#
# `BuildDeps`, `write_build_started` and `write_build_outcome` were all in `src/` until their
# production callers were deleted. Each is the DRIVER for tests of code that is still live — the
# sandbox toolset, and the transcript projection's two lifecycle arms — and the rows the two
# writers append are permanent in the production database, so their readers must stay tested
# against a faithful row rather than a hand-built dict.


@dataclass
class ToolDeps:
    """A run's dependencies as far as `orchestrator.tools` is concerned — which is: whatever the
    `sandbox_of` accessor closes over. The tool bodies never touch `ctx.deps` directly, so any
    object will do; this is the minimal one, and it is the shape the deleted `BuildDeps` had.

    A Write chat turn's real deps are `ChatDeps` (`services/agent/agent.py`), which carries a
    great deal this has no business modelling. Tests that want the tool surface and nothing else
    take this instead."""

    sandbox: SandboxSession
    user_id: uuid.UUID = field(default_factory=uuid.uuid4)


async def write_legacy_build_started(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    session_id: uuid.UUID,
    started_seq: int,
) -> bool:
    """Append a `build_started` lifecycle row exactly as `outcome.write_build_started` did.

    THE PRODUCTION WRITER IS DELETED and this is deliberately not a re-implementation for its own
    sake: rows of this shape are PERMANENT in the production transcript, the projection still
    reads them (`BuildInProgressItem`), and the outcome idempotency probe still has to skip them.
    Those readers are live and must stay tested against a faithful row rather than a hand-built
    dict that can drift from what is actually in the database.

    Kept byte-identical to the writer it replaces: hidden `system_event`, EMPTY native payload,
    `meta = {kind, sessionId, startedSeq}`."""
    try:
        await append_batch(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            messages=[],
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            kind=ChatKind.BUILD,
            visibility=MessageVisibility.HIDDEN,
            meta={
                "kind": "build_started",
                "sessionId": str(session_id),
                "startedSeq": started_seq,
            },
        )
    except SeqContentionError:
        return False
    return True


# The preview link is PARSED, not pattern-checked, and https-only — the same parse the deployed
# URL gets at the admin boundary. It is the fail-closed floor under "we only write URLs we
# minted": the constraint is scheme plus length and never host or shape, so an app address that
# carries a path still passes.
_PREVIEW_URL_MAX_CHARS: Final = 2048
_PREVIEW_URL: Final[TypeAdapter[AnyUrl]] = TypeAdapter(
    Annotated[AnyUrl, UrlConstraints(max_length=_PREVIEW_URL_MAX_CHARS, allowed_schemes=["https"])]
)


def _safe_preview_url(preview_url: str | None) -> str | None:
    """The preview link when it parses as https, else None — never the raw string.

    The recorded link is rendered straight into an `<a href>` in the portal's outcome card,
    same-origin with the user's session, so `javascript:` and `data:` must not survive. Fails
    closed to None — the record's own "no preview" state — rather than raising: losing a whole
    outcome row over a cosmetic link is the worse trade. The ORIGINAL string is returned rather
    than the parse's output, because pydantic normalizes (a path-less URL gains a trailing `/`)
    and the recorded link should be the address the sandbox actually served."""
    if preview_url is None:
        return None
    try:
        _PREVIEW_URL.validate_python(preview_url)
    except ValidationError:
        return None
    return preview_url


def _summary(status: BuildSessionStatus, reason: str | None) -> str:
    """The outcome's prose. This is the payload's TEXT, so it is both what a reader sees and what
    the model is replayed as history on the user's next turn — hence plain, factual wording.

    Every arm under the FAILED one keys on the REASON, because the STATUS cannot tell these
    apart: a natural finish, a Stop, a force-end and an idle reap all carry ENDED. Reading the
    status alone recorded a build stopped at minute two as "Build finished." — permanently, and
    then replayed that back to the model as history on the user's next turn."""
    if status is BuildSessionStatus.FAILED:
        return f"The build failed: {reason}" if reason else "The build failed."
    if reason == QUOTA_EXCEEDED:
        return "The build stopped: you reached your daily limit."
    if reason == STOPPED_BY_USER:
        return "You stopped this build before it finished."
    if reason == FORCE_ENDED:
        # The one graceful end that DISCARDED its work — the kill switch skipped the snapshot —
        # so any summary implying otherwise is a lie about the user's code.
        return "This build was force-stopped before it finished, and its work was discarded."
    if reason == IDLE_TEARDOWN:
        return "This build was stopped because it sat idle."
    return "Build finished."


def build_outcome_meta(
    *,
    status: BuildSessionStatus,
    session_id: uuid.UUID,
    preview_url: str | None,
    snapshot_committed: bool,
    reason: str | None,
    started_seq: int | None,
) -> dict[str, Any]:
    """The outcome row's `meta` — the build's structured record, OUTSIDE the native payload.

    `startedSeq` is the transcript's high-water seq at the moment a build began, and it is what
    made the attachment boundary TEMPORAL rather than positional. It is absent from every row
    written now; the parameter stays because rows already in the database carry it and because
    the trap it names is the one any re-introduction has to avoid — a row allocated at build END
    can land AFTER a turn recorded while the build ran, so a reader keying on this row's POSITION
    drops those turns permanently and silently."""
    meta: dict[str, Any] = {
        "kind": "build_outcome",
        "status": status.value,
        "sessionId": str(session_id),
        "previewUrl": _safe_preview_url(preview_url),
        "snapshotCommitted": snapshot_committed,
        "reason": reason,
    }
    if started_seq is not None:
        meta["startedSeq"] = started_seq
    return meta


async def write_build_outcome(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    session_id: uuid.UUID,
    status: BuildSessionStatus,
    preview_url: str | None,
    snapshot_committed: bool,
    reason: str | None,
    started_seq: int | None = None,
) -> bool:
    """Append a build-outcome `system_event` row. Returns True if written.

    Owner-scoped: the conversation must be the caller's, else this is a no-op, never a
    cross-user write. Idempotent on `session_id` — one outcome per build; an exhausted seq
    retry budget (`append_batch`) returns False rather than raising."""
    conversation = await db.scalar(
        sa.select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if conversation is None:
        return False  # deleted mid-build, or never ours — nothing to record it in

    if await _outcome_already_recorded(db, conversation_id, session_id):
        return False

    payload = ModelResponse(parts=[TextPart(content=_summary(status, reason))])
    meta = build_outcome_meta(
        status=status,
        session_id=session_id,
        preview_url=preview_url,
        snapshot_committed=snapshot_committed,
        reason=reason,
        started_seq=started_seq,
    )
    try:
        await append_batch(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            messages=[payload],
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            kind=ChatKind.BUILD,
            meta=meta,
        )
    except SeqContentionError:
        return False
    return True


async def _outcome_already_recorded(
    db: AsyncSession, conversation_id: uuid.UUID, session_id: uuid.UUID
) -> bool:
    """True if this session's outcome is already in the thread — keyed on `meta->>'sessionId'`,
    the only field that identifies the BUILD (a fresh row id/seq says nothing about which build
    it was)."""
    row = await db.scalar(
        sa.select(Message.id).where(
            Message.conversation_id == conversation_id,
            Message.entry_kind == MessageEntryKind.SYSTEM_EVENT,
            # `kind` disambiguates: the `build_started` lifecycle row carries this
            # session's id too, and without this predicate it would satisfy the idempotency
            # probe and silently suppress the real outcome.
            Message.meta["kind"].astext == "build_outcome",
            Message.meta["sessionId"].astext == str(session_id),
        )
    )
    return row is not None


# --- The connected-data surface's one input ---------------------------------------------------
#
# `ConnectedSystem` is a pure `core/` dataclass over the registry entry and a resolved window, and
# building one by hand takes eight lines of dates nobody's test is about. It lives HERE rather
# than in `factories.py` because it writes no row: it is the shape a router hands the turn, and
# every consumer of it (the prompt stub, the toolset gate, the tool body) needs one without a
# database at all.


def a_connected_system(*, effectively_on: bool = True) -> ConnectedSystem:
    """One connector this project may read, resolved.

    THE KEY AND THE ENTRY COME OFF THE REGISTRY, never a literal — the same discipline
    `test_access_state.py` uses. A test that spelled the key itself would keep passing after the
    registry renamed it, and would then be asserting about a connector the platform does not have.

    `effectively_on=False` builds the state the router never returns, which is exactly why it is
    offered: the tool's refusal for a system that is switched off is unreachable through
    registration, so the only way to assert it is to construct it."""
    key, connector = next(iter(CONNECTORS.items()))
    today = date(2026, 9, 10)
    return ConnectedSystem(
        key=key,
        connector=connector,
        window=ResolvedWindow(
            effectively_on=effectively_on,
            kind=ConnectorWindowKind.RELATIVE,
            start=today - timedelta(days=connector.max_window_days),
            end=today - timedelta(days=connector.freshness_lag_days),
            days=connector.max_window_days,
            clamped=False,
            earliest=today - timedelta(days=connector.max_window_days),
            latest=today - timedelta(days=connector.freshness_lag_days),
        ),
    )
