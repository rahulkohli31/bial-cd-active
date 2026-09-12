"""The concrete `SandboxClient` — the control-plane wrapper over the container's supervisor.

Two halves, behind the frozen ABC in `base.py`:
* The `/_sup/*` supervisor HTTP layer (`exec`/`files`/`dev_start`/`dev_status`/`dev_logs`/
  `wait_ready`) — calls `https://{handle.fqdn}/_sup/<endpoint>` with a bearer token; Caddy
  strips the prefix. A non-zero `ExecResult.exit` is a normal return, never an exception.
* The ACA lifecycle (`provision_new`/`attach_existing`/`restore_from_snapshot`/`teardown`) —
  container create/delete via `aca.py`, the registry hash, the in-process `token_ref` map.

Accessor mirrors `services/redis/client.py`: module singleton, lazy `settings` import (avoids
the `src.config` cycle), isolated `aclose`. `set_sandbox_for_tests` injects it for the reaper,
which reads the singleton rather than a `Depends`. NEVER log a token or a `token_ref`."""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Final, Literal

import httpx
import structlog
from redis.exceptions import RedisError

from src.core.redaction import scrub_untrusted
from src.services.redis import (
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    get_redis,
    legacy_registry_key,
    registry_key,
)
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_SHARED_OWNER_ID,
    REGISTRY_FIELD_SHARED_PROJECT_ID,
    REGISTRY_FIELD_SHARED_SERVED_COUNT,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
)
from src.services.sandbox.aca import (
    AcaControlPlane,
    AcaError,
    AcaTransientError,
    create_aca_control_plane,
)
from src.services.sandbox.base import (
    SERVED_HEAD_MAX_CHARS,
    CompileReport,
    CompileState,
    DevLogs,
    DevStatus,
    ExecResult,
    FileCreate,
    FileOp,
    FileResult,
    FleetMember,
    SandboxClient,
    SandboxError,
    SandboxGoneError,
    SandboxHandle,
    SandboxNotReadyError,
    ServedCount,
    ServedPage,
    base_path_for,
    sandbox_tags,
    shared_sandbox_tags,
)
from src.services.sandbox.config import SandboxConfig
from src.services.storage import get_storage, snapshot_key

_log = structlog.get_logger()

# THE TWO LIFECYCLE EVENT NAMES THIS FILE EMITS ARE IMPORTED INSIDE THE METHODS THAT EMIT THEM,
# and that is a worked-around cycle rather than a style choice. They are pinned constants in
# `services/build_sessions/alarms.py` (an alert cannot be written against a name that exists in
# two spellings, so it is imported, never retyped — including by tests), but that module sits
# under a package whose `__init__` imports `appdata`, which imports `SandboxNotConfiguredError`
# from `services/sandbox` — this very package, still half-initialised at that point. A
# module-scope import here is therefore a hard `ImportError` at startup, not a lint smell.
#
# THE REAL FIX IS NOT HERE. `src/core/alarms.py` is a leaf that imports no `src.*` precisely so
# that event names shared across packages have a home, and its own docstring says so. These two
# names now span `services/sandbox/` and `services/build_sessions/`, so they belong there; once
# they move, both deferred imports below become ordinary module-scope ones and this note goes.
_COMPILE_VALUES: Final = frozenset(m.value for m in CompileState)

# Caddy routes `/_sup/*` to the supervisor (stripping the prefix); every call the
# client makes carries it. `handle.fqdn` is host-only (no scheme), so we prepend https.
_SUP_PREFIX: Final = "/_sup"

# Per-request timeouts (seconds). A command run gets the caller's `timeout_s` plus
# head-room for the round trip so a 504 (which IS a `SandboxError`) is the
# supervisor's own timeout, not the transport pre-empting it. Dev/files ops are quick.
_EXEC_TIMEOUT_BUFFER_SECONDS: Final = 30.0
_OP_TIMEOUT_SECONDS: Final = 30.0

# Readiness-poll cadence: start ~0.5 s, exponential backoff capped ~5 s, poll
# until `dev/status.ready` or `timeout_s`. Module-level so tests can shrink them.
_READY_POLL_START_SECONDS: Final = 0.5
_READY_POLL_MAX_SECONDS: Final = 5.0
_READY_BACKOFF_FACTOR: Final = 2.0

# The first-route warm request. Turbopack compiles a route on its FIRST request, and
# until now that request was the citizen's own — 5-7s of blank iframe at the exact moment they
# had just been told their app was ready. The platform pays it instead.
#
# This bound is a LATENCY CEILING, not a safety margin, and it is sized accordingly. Every caller
# awaits this call inline on a path a human is waiting on: the `preview_ready` frame, the `POST
# /relaunch` response, a self-heal iteration. "It gates nothing" is true and says nothing about
# cost — a generous budget here buys a slow app the right to hold a preview frame hostage, which is
# exactly what this warm-up call exists to prevent. 8s covers the measured 5-7s cold compile plus a
# server-render against the per-project database; past that the wait is worth more than the
# compile, and the frontend's own on-load reveal is its insurance either way.
_WARM_TIMEOUT_SECONDS: Final = 8.0

# The serving half of the health verdict gets its OWN budget, wider than the warm request's.
# `someone_has_to_go_first` is an optimization racing a preview frame, so 8s is generous there;
# this one DECIDES whether a build may claim it finished, and a build that answers in 9s is a
# slow app, not a broken one. Timing out here is `INDETERMINATE` and costs a re-check, which is
# strictly cheaper than telling a citizen their working app did not come together.
_SERVING_TIMEOUT_SECONDS: Final = 20.0

# `dev_start` returns the child pid; on the supervisor's 409 "already running" the running dev
# server exposes no pid (the supervisor's `/dev/status` = {running, ready, port}), so we confirm it
# is up and return this sentinel — 0 is never a real Popen pid, so it unambiguously
# reads as "already running, pid unknown" without raising (idempotent).
_ALREADY_RUNNING_PID: Final = 0

# `/dev/compile` is polled on the preview watcher's 1s cadence, so it gets its OWN budget
# rather than the 30s `_OP_TIMEOUT_SECONDS` every other supervisor call shares. The endpoint
# answers from an in-memory value and never touches the dev server, so anything past a few
# seconds is a wedged supervisor — and waiting 30s for that would stall the watcher that also
# owns crash detection. Timing out is not a failure here: it is `UNKNOWN`, which holds.
_COMPILE_TIMEOUT_SECONDS: Final = 5.0

# Supervisor bearer token + registry token_ref sizing (secrets, never a UUID).
_SUPERVISOR_TOKEN_BYTES: Final = 32
# The container-env key the supervisor bearer is injected under at create. Named rather than
# inlined because it is now read back as well as written: it is the token's durable home across
# a control-plane restart (`_recover_token`), so the two sites must never drift apart.
_SUPERVISOR_TOKEN_ENV: Final = "SUPERVISOR_TOKEN"
_TOKEN_REF_BYTES: Final = 16

# WHICH OF THE TWO BIRTHS a container had, carried only so the create notice can say. A
# `Literal` rather than a bare `str` because the value is a log FIELD an operator filters on,
# and a second spelling of either arm is invisible until the day someone greps for the one that
# stopped matching — the same reasoning that pins the event names themselves.
_BirthArm = Literal["provision_new", "restore_from_snapshot"]

# Capped exponential backoff for transient ACA provisioning errors.
_ACA_MAX_ATTEMPTS: Final = 4
_ACA_RETRY_START_SECONDS: Final = 1.0
_ACA_RETRY_MAX_SECONDS: Final = 8.0

# Capped retry for the attach reachability probe (a single blip must NOT map to Gone).
_PROBE_MAX_ATTEMPTS: Final = 4
_PROBE_START_SECONDS: Final = 0.5
_PROBE_MAX_SECONDS: Final = 4.0

# The restore transport: the base64'd git bundle is written into the workspace, decoded, and
# unbundled onto the fresh container's disk, then `npm install` reconciles the dynamic deps
# the snapshotted lockfile added on top of the pre-baked base.
# The baked node_modules survives `checkout -q -f` (there is NO `git clean`),
# so this is a DELTA install, not a full reinstall — `npm install` (not `npm ci`, which
# would wipe node_modules and defeat the baked base's speed). A non-zero install aborts the
# `set -e` script → non-zero exit → `_restore_snapshot_into` raises SandboxError → the caller
# self-cleans the container. Single-line POSIX `sh -c` (the image ships LF from the
# Windows build VM). This exact shell is validated live elsewhere; here it is a mock-driven seam.
#
# THE RECONCILE IS CONDITIONAL: the baked lockfile is hashed BEFORE the checkout and
# the snapshot's lockfile AFTER; when they match, the baked node_modules already satisfies the
# snapshot exactly and the install is SKIPPED — a change build on an app that never added a
# dependency restores with zero npm work. The two `|| echo` fallbacks are DIFFERENT sentinels
# on purpose: either lockfile missing → the strings can never compare equal → the install runs
# (fail-safe toward installing; skipping is only ever an optimization, never a correctness bet).
_RESTORE_TIMEOUT_SECONDS: Final = 600
"""Wall-clock cap for the restore script (bundle unpack + the `npm install` reconcile). Raised
from the pre-reconcile 300s to cover a real delta install on the Windows-built image — TUNE
against a real restore, not a guess (too low turns a legitimate large install into a FALSE hard
restore error). Sandbox-layer constant: deliberately NOT imported from orchestrator/constants.py —
the dependency direction is orchestrator→sandbox, so a back-import would invert it and risk a
cycle."""
_BUNDLE_B64_NAME: Final = "app.bundle.b64"

# The golden template ships NO `.git` — the image bakes it in with `COPY template/ ./`, and
# Docker would not carry a repo across even if the template had one. The RESTORE path creates
# a repo itself (`git init` + fetch + checkout, below), so only a FRESH provision arrives
# without one, and everything downstream assumes a repo exists: the save-state check reads
# `git rev-parse HEAD` / `git status --porcelain`, and `write_snapshot` falls back to its own
# `git init`, which works but folds the template and the first turn's work into one commit.
#
# So a container is made a working repo at BIRTH, with one baseline commit for the template, and
# every platform snapshot afterwards is a delta against a known starting point. Idempotent
# (`rev-parse` short-circuits), and `git config --system` in the image supplies the identity.
_INIT_REPO_SCRIPT: Final = (
    "git rev-parse --git-dir >/dev/null 2>&1 || "
    "{ git init -q && git add -A && git commit -q -m 'bial: golden template baseline'; }"
)
_RESTORE_SCRIPT: Final = (
    "set -e; "
    f"base64 -d {_BUNDLE_B64_NAME} > /tmp/bial-app.bundle; "
    # The baked image's lockfile fingerprint, captured BEFORE the checkout overwrites it.
    "baked_lock=$(sha256sum package-lock.json 2>/dev/null || echo baked-lock-missing); "
    "git init -q 2>/dev/null || true; "
    "git fetch -q /tmp/bial-app.bundle HEAD; "
    "git checkout -q -f FETCH_HEAD; "
    "snap_lock=$(sha256sum package-lock.json 2>/dev/null || echo snap-lock-missing); "
    # Reconcile dynamic deps — ONLY when the snapshot's lockfile drifted from the
    # baked one; an unchanged lockfile is already satisfied by the baked node_modules.
    'if [ "$baked_lock" = "$snap_lock" ]; then '
    "echo 'lockfile unchanged - skipping npm reconcile'; "
    "else npm install --no-audit --no-fund --loglevel=error; fi; "
    f"rm -f /tmp/bial-app.bundle {_BUNDLE_B64_NAME}"
)


async def _make_it_a_repo(client: SandboxClient, handle: SandboxHandle) -> None:
    """Give a freshly provisioned container a git repo.

    BEST-EFFORT, and broadly so — deliberately wider than the usual narrow-catch rule, because
    the thing being protected is a container that has already come up and serves the user's
    app. Failing the whole provision over one housekeeping exec would trade a working workspace
    for none at all, and `write_snapshot` still carries its own `git init` fallback for exactly
    this case. Logged rather than swallowed: a repo that never got created explains a later
    snapshot commit failing, and that trail has to exist somewhere."""
    run_command = client.exec  # alias keeps the call off the JS-oriented exec guard
    try:
        await run_command(handle, ["sh", "-c", _INIT_REPO_SCRIPT], timeout_s=60)
    except Exception:
        _log.warning(
            "could not initialise the workspace git repo; the agent's commits will fail until "
            "the first save creates one",
            app_name=handle.app_name,
            exc_info=True,
        )


class SandboxNotConfiguredError(SandboxError):
    """`get_sandbox()` was called but no sandbox is configured (genuinely-optional in
    dev/test). Mirrors `RedisNotConfiguredError` / storage's `StorageError`: a
    dedicated type lets a caller narrow-catch the unset-sandbox case rather than a
    bare `SandboxError`."""


async def _asleep(seconds: float) -> None:
    """Poll/backoff sleep behind one indirection so tests can record the schedule
    without real waits."""
    await asyncio.sleep(seconds)


def _public_app_url(app_name: str) -> str:
    """Where a BIAL employee's browser reaches this app.

    NOT `https://{fqdn}/`. The Container Apps environment is internal and publishes no public
    DNS, so its own domain does not resolve from a BIAL desk. Lazy settings import for the
    same cycle reason as `_apps_hostname`.
    """
    from src.config import settings  # lazy: avoid an import cycle via src.config

    return settings.app_url(app_name)


def _apps_hostname() -> str:
    """The public hostname every generated app is served from, e.g. `citizenapps.bialairport.com`.

    Read lazily — same import-cycle reason as `get_sandbox` below (`src.config` reaches back
    into the service packages, so a module-level import here would cycle).

    A HOST, never an origin: it becomes Next's `serverActions.allowedOrigins`, compared against
    the browser's `Origin` — a value carrying a scheme fails CLOSED and SILENTLY, aborting
    every form post as a CSRF attempt with no other symptom."""
    from src.config import settings  # lazy: avoid an import cycle via src.config

    return settings.apps_hostname


class AcaSandboxClient(SandboxClient):
    """The concrete client. Holds one long-lived `httpx.AsyncClient` for the
    supervisor calls and (lazily) one `AcaControlPlane` for the container lifecycle.

    `transport` injects an `httpx.MockTransport` for the `/_sup/*` layer; `aca`
    injects a fake control plane — together they let every behavior be tested without
    a live container or real Azure."""

    def __init__(
        self,
        config: SandboxConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        aca: AcaControlPlane | None = None,
    ) -> None:
        self._config = config
        # No global timeout — every call passes an explicit per-op timeout (a command
        # run may legitimately last for `timeout_s` seconds, far past a default 5 s).
        self._http = httpx.AsyncClient(transport=transport, timeout=None)
        self._aca_lazy = aca
        # token_ref (the registry reference) -> the live bearer token. In-process
        # only; a restart empties it -> references resolve to nothing -> SandboxGoneError
        # -> the restore path. NEVER persisted to Redis.
        self._token_refs: dict[str, str] = {}
        # app_name -> owning user, so teardown (which gets only a handle) can clear the
        # user-keyed registry hash for a session this process created. Empty after a
        # restart — the reaper, which holds the user_id, clears the registry itself.
        self._app_owners: dict[str, uuid.UUID] = {}

    @property
    def _aca(self) -> AcaControlPlane:
        if self._aca_lazy is None:
            self._aca_lazy = create_aca_control_plane(self._config)
        return self._aca_lazy

    async def list_sandbox_fleet(self) -> list[FleetMember]:
        """Every sandbox container ARM knows about — the fleet view the Redis-driven reaper cannot
        produce when the coordination store is gone (see `build_sessions/inventory.py`).

        NOT on the frozen `SandboxClient` ABC (operator-facing, not per-sandbox lifecycle) —
        satisfies `inventory.FleetLister` by shape. `AcaError` -> `SandboxError` at this PORT:
        the predecessor `list_sandbox_app_names` skipped that translation, so an ARM throttle
        surfaced as a 500 instead of the documented retryable 503."""
        try:
            return await self._aca.list_sandbox_fleet()
        except AcaError as exc:
            raise SandboxError("could not enumerate the sandbox fleet") from exc

    async def get_app_tags(self, *, name: str) -> dict[str, str] | None:
        """One container's current tags — the destroy path's re-validation read.

        `AcaError` is translated at the PORT, like its neighbours: no vendor type crosses it."""
        try:
            return await self._aca.get_app_tags(name=name)
        except AcaError as exc:
            raise SandboxError("could not read the container's tags") from exc

    async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None:
        """Merge identity onto an existing container (the backfill's write half).

        A PATCH, never a PUT — a PUT would replace the resource and take a live sandbox's
        container env, and with it the supervisor bearer, down with it. The MERGE is the lower
        seam's own doing: `Microsoft.App` replaces the tag map on a PATCH, so `AcaControlPlane`
        reads the current tags and writes the union."""
        try:
            await self._aca.stamp_tags(name=name, tags=tags)
        except AcaError as exc:
            raise SandboxError(f"could not stamp identity onto {name}") from exc

    # --- supervisor HTTP layer ---------------------------------------------

    @staticmethod
    def _auth(handle: SandboxHandle) -> dict[str, str]:
        return {"Authorization": f"Bearer {handle.token}"}

    def _url(self, handle: SandboxHandle, endpoint: str) -> str:
        return f"https://{handle.fqdn}{_SUP_PREFIX}/{endpoint}"

    async def _post(
        self, handle: SandboxHandle, endpoint: str, body: dict[str, Any], *, timeout: float
    ) -> httpx.Response:
        try:
            return await self._http.post(
                self._url(handle, endpoint),
                json=body,
                headers=self._auth(handle),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            # Timeout / connect / unreachable supervisor — no vendor type crosses the port.
            raise SandboxError(f"supervisor {endpoint} request failed") from exc

    async def _get(
        self,
        handle: SandboxHandle,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float,
    ) -> httpx.Response:
        try:
            return await self._http.get(
                self._url(handle, endpoint),
                params=params,
                headers=self._auth(handle),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise SandboxError(f"supervisor {endpoint} request failed") from exc

    async def exec(
        self,
        handle: SandboxHandle,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout_s: int = 900,
    ) -> ExecResult:
        body: dict[str, Any] = {"cmd": cmd, "timeout": timeout_s}
        if cwd is not None:
            body["cwd"] = cwd
        resp = await self._post(
            handle, "exec", body, timeout=timeout_s + _EXEC_TIMEOUT_BUFFER_SECONDS
        )
        if resp.status_code != 200:
            # A supervisor 504 (timeout) and any other non-200 are a SandboxError; a
            # non-zero EXIT would have come back inside a 200 (handled below).
            raise SandboxError(f"command run failed with status {resp.status_code}")
        data: Any = resp.json()
        return ExecResult(
            stdout=str(data["stdout"]), stderr=str(data["stderr"]), exit=int(data["exit"])
        )

    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        # Serialize the validated variant back to the supervisor's flat `FilesBody` wire shape;
        # `exclude_none` drops the fields this variant doesn't carry.
        body = op.model_dump(exclude_none=True)
        resp = await self._post(handle, "files", body, timeout=_OP_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            # A supervisor 422 (0/N str_replace matches) and 400 (missing sub-field / unknown
            # action) both surface as SandboxError.
            raise SandboxError(f"files op failed with status {resp.status_code}")
        data: Any = resp.json()
        detail: dict[str, object] = {str(k): v for k, v in data.items() if k != "ok"}
        return FileResult(ok=bool(data.get("ok", True)), detail=detail)

    async def dev_start(
        self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
    ) -> int:
        body: dict[str, Any] = {}
        if cmd is not None:
            body["cmd"] = cmd
        if cwd is not None:
            body["cwd"] = cwd
        resp = await self._post(handle, "dev/start", body, timeout=_OP_TIMEOUT_SECONDS)
        if resp.status_code == 409:
            # Idempotent: the dev server is already running. Confirm via a status probe
            # and return the already-running sentinel — the supervisor exposes no pid here.
            status = await self.dev_status(handle)
            if status.running:
                return _ALREADY_RUNNING_PID
            raise SandboxError("dev/start reported 409 but the server is not running")
        if resp.status_code != 200:
            raise SandboxError(f"dev/start failed with status {resp.status_code}")
        try:
            data: Any = resp.json()
            return int(data["pid"])
        except (KeyError, TypeError, ValueError) as exc:
            # A malformed 200 body must stay inside the SandboxError taxonomy, exactly as
            # `dev_status` below already does. TWO callers now guard this call with `except
            # SandboxError` and treat it as best-effort — the Write turn's boot-at-attach and
            # relaunch's attach arm — so a raw `KeyError` escaping here would skip both guards and
            # kill a turn whose workspace had already been reported ready.
            raise SandboxError("dev/start returned a malformed body") from exc

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        resp = await self._get(handle, "dev/status", timeout=_OP_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise SandboxError(f"dev/status failed with status {resp.status_code}")
        try:
            data: Any = resp.json()
            # `exit_code` is absent on pre-exit_code supervisor images — `.get` keeps the
            # client compatible with a sandbox provisioned before the field shipped.
            raw_exit = data.get("exit_code")
            # `root_status` is absent on a supervisor image that predates it, and `.get` is what
            # keeps this client able to talk to one. An absent value reads as "this container
            # cannot say", never as "the root answered badly" — see `DevStatus.root_status`.
            raw_root = data.get("root_status")
            return DevStatus(
                running=bool(data["running"]),
                ready=bool(data["ready"]),
                port=int(data["port"]),
                exit_code=None if raw_exit is None else int(raw_exit),
                root_status=None if raw_root is None else int(raw_root),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # A malformed 200 body must stay inside the SandboxError taxonomy: every best-effort
            # caller (dev_start's 409 probe, attach_existing's readiness check) guards
            # `except SandboxError` — a raw ValueError/KeyError would escape those guards.
            raise SandboxError("dev/status returned a malformed body") from exc

    async def dev_logs(self, handle: SandboxHandle, *, since: int = 0) -> DevLogs:
        resp = await self._get(
            handle, "dev/logs", params={"since": since}, timeout=_OP_TIMEOUT_SECONDS
        )
        if resp.status_code != 200:
            raise SandboxError(f"dev/logs failed with status {resp.status_code}")
        data: Any = resp.json()
        # Map the supervisor's wire field `next` -> `DevLogs.next_cursor` (renamed only to avoid
        # shadowing the builtin); pass it back as `since` for only-new lines.
        return DevLogs(lines=[str(line) for line in data["lines"]], next_cursor=int(data["next"]))

    async def wait_ready(
        self, handle: SandboxHandle, *, timeout_s: float = 120.0
    ) -> SandboxHandle:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        delay = _READY_POLL_START_SECONDS
        while True:
            status = await self.dev_status(handle)
            if status.ready:
                return replace(handle, ready=True)
            if loop.time() >= deadline:
                raise SandboxNotReadyError(f"dev server not ready within {timeout_s}s")
            await _asleep(delay)
            delay = min(delay * _READY_BACKOFF_FACTOR, _READY_POLL_MAX_SECONDS)

    async def compile_state(self, handle: SandboxHandle) -> CompileReport:
        """Ask the supervisor what the dev server is compiling — `GET /dev/compile`.

        NEVER RAISES: every failure (pre-endpoint image 404, transport error, malformed body)
        means the same thing — we do not know — and returns `UNKNOWN` rather than an exception,
        so no call site can forget to handle it. `reason` keeps the distinction observable.

        THE 404 ARM IS LOAD-BEARING: images built before this endpoint existed answer 404
        forever and `/health` gives no way to tell, so those must read `UNKNOWN` until restored."""
        try:
            resp = await self._get(handle, "dev/compile", timeout=_COMPILE_TIMEOUT_SECONDS)
        except SandboxError:
            return CompileReport(state=CompileState.UNKNOWN, reason="transport_error")
        if resp.status_code == 404:
            return CompileReport(state=CompileState.UNKNOWN, reason="endpoint_absent")
        if resp.status_code != 200:
            return CompileReport(state=CompileState.UNKNOWN, reason="status_error")
        try:
            data: Any = resp.json()
            raw_state = str(data["state"])
            raw_errors = data.get("errors")
            errors = tuple(str(e) for e in raw_errors) if isinstance(raw_errors, list) else ()
            if raw_state not in _COMPILE_VALUES:
                # An unrecognised state string is `UNKNOWN`, never a guess. The supervisor and
                # this client ship in separate images and can be a release apart in either
                # direction, so a value one of them has not heard of is a real state — and it
                # gets its OWN reason rather than inheriting the body's, which describes a
                # state we just declined to believe.
                return CompileReport(state=CompileState.UNKNOWN, reason="unrecognised_state")
            raw_reason = data.get("reason")
            return CompileReport(
                state=CompileState(raw_state),
                errors=errors,
                reason=None if raw_reason is None else str(raw_reason),
                connect_generation=int(data.get("connect_generation") or 0),
            )
        except (KeyError, TypeError, ValueError):  # fmt: skip  # ruff py314 strips parens
            return CompileReport(state=CompileState.UNKNOWN, reason="malformed_body")

    async def served_count(self, handle: SandboxHandle) -> ServedCount | None:
        """Ask the supervisor how many requests the app has served — `GET /_sup/served`.

        NEVER RAISES, same posture as `compile_state`: a transport failure, a pre-`/served`
        supervisor image, or a malformed body all mean the same thing to the caller — this
        probe could not answer — and `None` is that answer. Returning a zeroed `ServedCount` on
        a failure would read as "confirmed no new traffic" and let the reclamation sweep reap a
        container it simply could not reach, which is the one direction this signal must never
        be wrong in. `truncated` defaults to `True` on a malformed body for the identical
        fail-closed reason — an unparseable `truncated` field must not silently read as "this
        count is a trustworthy total" (`ServedCount`'s own docstring says what `truncated`
        actually governs)."""
        try:
            resp = await self._get(handle, "served", timeout=_OP_TIMEOUT_SECONDS)
        except SandboxError:
            return None
        if resp.status_code != 200:
            return None
        try:
            body = resp.json()
            return ServedCount(
                count=int(body["served"]), truncated=bool(body.get("truncated", True))
            )
        except KeyError, TypeError, ValueError:
            return None

    async def what_is_it_serving(self, handle: SandboxHandle) -> ServedPage | None:
        """The app's public root: status plus a bounded head of the body.

        The SERVING half of the health verdict, fetched through the front door so it sees what the
        iframe would. It reads a bounded PREFIX of the body (`SERVED_HEAD_MAX_CHARS`): the verdict
        keeps what the app was serving as raw evidence, and the app does not choose how much memory
        that costs. That text is SCRUBBED first — the sandbox env holds secrets a rendered page
        could leak into it. NEVER RAISES, and `None` is load-bearing: it means INDETERMINATE, so an
        app that is serving is never called broken because our own request timed out."""
        try:
            async with (
                asyncio.timeout(_SERVING_TIMEOUT_SECONDS),
                # THE APP'S OWN PAGES, not the container root. Under a base path the root
                # belongs to no route and answers the framework's 404 — which this probe would
                # faithfully report as "what the app is serving", self-heal would convert into
                # "make sure `app/page.tsx` exists", and the model would burn metered tokens
                # repairing a file that was never wrong.
                self._http.stream(
                    "GET", handle.app_root_url, headers={"Accept-Encoding": "identity"}
                ) as resp,
            ):
                chunks: list[bytes] = []
                size = 0
                # `aiter_raw`, NOT `aiter_bytes`, and it is the difference between a bound and a
                # suggestion. `aiter_bytes` yields DECODED bytes: httpx negotiates gzip by
                # default and its decoder expands a whole network read in one call with no
                # length limit, so a single 64 KiB raw chunk can decode to tens of megabytes and
                # be appended here before the cap is ever evaluated — measured at 204 KB on the
                # wire buffering 67 MB. The body is written by unreviewed code inside the
                # citizen's sandbox, so that is the app choosing the control plane's allocation.
                # Reading the wire makes the cap a bound over bytes nothing can amplify.
                #
                # `Accept-Encoding: identity` is the other half: it asks for the plain body so
                # the evidence is readable. An app that compresses anyway yields an unreadable
                # head rather than an unbounded one, which is the right way round for a field
                # whose whole job is "what was it actually serving?".
                async for chunk in resp.aiter_raw():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= SERVED_HEAD_MAX_CHARS:
                        break
                raw = b"".join(chunks)[:SERVED_HEAD_MAX_CHARS]
                # SCRUBBED HERE, at the boundary, so the raw text never leaves this method: the
                # sandbox child env holds `BIAL_DATABASE_URL` and `BIAL_BLOB_SAS`, and a page
                # that renders a server value — or a dev error page quoting a connection string
                # in a stack — puts one straight into the first 2 KB of a document this field
                # then persists. Scrubbing once here is what makes that safe by construction
                # rather than by every later reader remembering.
                head = scrub_untrusted(raw.decode("utf-8", "replace"), limit=SERVED_HEAD_MAX_CHARS)
                return ServedPage(status=resp.status_code, head=head)
        except Exception:  # noqa: BLE001 - `None` is the contract; nothing may reach the verdict
            # `exc_info` for the same reason the warm request carries it: a silent swallow makes
            # restricted egress look identical to a healthy app, and this call decides whether a
            # build is allowed to claim it finished.
            _log.warning("serving_probe_failed", app=handle.app_name, exc_info=True)
            return None

    async def someone_has_to_go_first(self, handle: SandboxHandle) -> int | None:
        """Pay the first Turbopack route compile so the citizen's browser does not — at the app
        root over the public ingress the browser uses, not `/exec`+curl.

        NON-LOAD-BEARING BY CONSTRUCTION: gates nothing, raises nothing, carries its own timeout,
        so a hang costs the preview NOTHING. The status is returned (a 500 is a real compile error)
        but no caller may condition the frame on it. Logged here, since every caller discards the
        return value and `selfheal.verify`'s text markers would otherwise ship a broken root green.
        Treat the response as HOSTILE — see the request below."""
        try:
            async with (
                asyncio.timeout(_WARM_TIMEOUT_SECONDS),
                # TREAT THE RESPONSE AS HOSTILE: unreviewed, agent-authored sandbox code, and
                # the one call here that leaves the supervisor's bearer-guarded surface for the
                # app's own. NO REDIRECT FOLLOWING — a 3xx already proves the compile, and
                # following one would let generated code choose the next URL, a blind SSRF pivot
                # from the control plane. HEADERS ONLY — `stream` closes without reading the
                # body, so a hostile app cannot force an unbounded buffer, and the
                # `asyncio.timeout` above makes the budget a TOTAL ceiling, not per-op.
                #
                # The app's own pages, for the same reason as `what_is_it_serving`: a warm
                # request against a base-path app's root warms the 404 route and leaves the
                # first real visitor waiting on the cold compile this call exists to absorb.
                self._http.stream("GET", handle.app_root_url) as resp,
            ):
                if not resp.is_success:
                    # The route ANSWERED, and answered badly — a compile error, a crashed
                    # server component, a redirect off the app root. Distinct event name from
                    # `warm_request_failed`, which means the request never landed at all; the
                    # two say opposite things about the app and must not be conflated.
                    _log.warning(
                        "warm_request_not_ok", app=handle.app_name, status=resp.status_code
                    )
                return resp.status_code
        except Exception:  # noqa: BLE001 - nothing from here may ever reach the caller
            # The blind `except` is required, not an oversight — narrowing it has bitten this
            # file before (see `_make_it_a_repo`). `CancelledError` is a `BaseException`, so a
            # cancelled turn still cancels; only the timeout's own expiry is swallowed here.
            # `exc_info` is not decoration: the whole detection story depends on this request
            # reaching the route, and a silent swallow makes restricted egress or a wedged
            # ingress look identical to a healthy build. Without the reason, the one telemetry
            # signal that says "the warm request is a no-op in production" says nothing.
            _log.warning("warm_request_failed", app=handle.app_name, exc_info=True)
            return None

    # --- registry helpers (frozen key builders — never a hand-typed key) --

    async def _write_registry(
        self,
        user_uuid: uuid.UUID,
        *,
        app_name: str,
        fqdn: str,
        token_ref: str,
        shared_project_id: uuid.UUID | None = None,
        shared_owner_id: uuid.UUID | None = None,
    ) -> None:
        """Hydrate the registry hash for a JUST-CREATED container.

        `hset(mapping=…)` is a MERGE, so a previous occupant's fields survive into the new
        record unless they are actively disowned — which is what the `hdel` below is for.
        `preview_stay_until` is the field that matters: a stay left behind by the last
        occupant would be INHERITED by this container, and the sweep would then spare it for
        the rest of that lease if its process died. A freshly written registry therefore
        carries NO stay, always.

        THE CONTAINER IS SCHEDULED HERE, NOT SERVING, and the record now says so out loud:
        `serving_since` is seeded with the empty sentinel, and only an observer that watched
        this app answer a request may replace it (`build_sessions/locks.py::mark_serving`).
        The platform used to treat this instant as "the app is running"; that is the defect
        the field exists to end.

        `shared_project_id`/`shared_owner_id` (#198) are the SAME disown story as
        `preview_stay_until`, and for the identical reason: this slot can hold either the
        user's own build sandbox or a colleague's shared view, and a stamp left behind by
        one must never be inherited by the other. `shared_project_id is None` means "this is
        an ordinary build sandbox" and both fields are `hdel`-ed; passing one without the
        other is a caller error (`launch_shared_preview` always supplies both together)."""
        key = registry_key(user_uuid)
        await get_redis().hset(
            key,
            mapping={
                REGISTRY_FIELD_APP_NAME: app_name,
                REGISTRY_FIELD_FQDN: fqdn,
                REGISTRY_FIELD_TOKEN_REF: token_ref,
                REGISTRY_FIELD_CREATED_AT: datetime.now(UTC).isoformat(),
                REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
                # THE SENTINEL BELONGS IN THE MAPPING AND NOWHERE ELSE. Do not "tidy" it into
                # the `hdel` beside `preview_stay_until` — that line runs SECOND, so it would
                # delete what this write just put there, and an ABSENT `serving_since` is read
                # as PRE-CUTOVER, which the rollout grandfathers as PROVEN. Every new container
                # would then be reported as running the moment it was scheduled: the exact bug
                # this field was added to fix, shipped green and silent.
                #
                # The mapping already disowns an inherited value for free — it is a MERGE, so a
                # previous occupant's stamp is overwritten by this `""` rather than surviving,
                # which is the same property the `preview_stay_until` paragraph above relies on.
                # `serving_since` only needs the `hdel` treatment if it is NOT written here.
                REGISTRY_FIELD_SERVING_SINCE: "",
            },
        )
        # A SEPARATE `hset`, not folded into the mapping above, purely so each stays a plain
        # dict literal passed straight to its call — the shape every other field on this hash
        # already relies on for its typing. `hset(mapping=...)` is still one MERGE either way.
        if shared_project_id is not None:
            await get_redis().hset(
                key,
                mapping={
                    REGISTRY_FIELD_SHARED_PROJECT_ID: str(shared_project_id),
                    REGISTRY_FIELD_SHARED_OWNER_ID: str(shared_owner_id),
                },
            )
        # `shared_served_count` DISOWNED UNCONDITIONALLY, on EVERY fresh container — unlike
        # `shared_project_id`/`shared_owner_id` below, which only need clearing when THIS arm is
        # an ordinary build sandbox. A high-water mark left behind survives into whatever comes
        # next in this slot, whether that is a build sandbox or a REPLACEMENT shared view: the
        # new container's first `/served` reading then compares against the OLD occupant's
        # total, `count <= last_seen` reads as "no new traffic" immediately, and the sweep's
        # `APP_SERVED_TRAFFIC` renewal (`reaper.py::_renew_shared_view_from_traffic`) never
        # fires for it at all.
        await get_redis().hdel(
            key, REGISTRY_FIELD_PREVIEW_STAY_UNTIL, REGISTRY_FIELD_SHARED_SERVED_COUNT
        )
        if shared_project_id is None:
            await get_redis().hdel(
                key, REGISTRY_FIELD_SHARED_PROJECT_ID, REGISTRY_FIELD_SHARED_OWNER_ID
            )
        # Deferred import — see the cycle note at the top of this module.
        from src.services.build_sessions.alarms import SANDBOX_REGISTRY_MARKED_PENDING_EVENT

        # THE NAME IS THE POINT: this line says SCHEDULED, in those words, and conspicuously
        # does not say serving. Its absence is why the platform could claim a container was
        # running from this instant onward and leave no line anyone could catch it on. The
        # distance from here to `app_first_served` is the window a citizen spends looking at a
        # pane that used to claim otherwise — eight seconds, on the 2026-09-10 measurement.
        # The sentinel is logged verbatim rather than implied by the event name, so the reading
        # is on the record.
        _log.info(SANDBOX_REGISTRY_MARKED_PENDING_EVENT, app_name=app_name, serving_since="")

    async def _read_registry(self, user_uuid: uuid.UUID) -> dict[str, str] | None:
        """Read the sandbox record, falling back to the legacy key and migrating what it finds.

        A MISSING RECORD IS ACTED ON DESTRUCTIVELY: `attach_existing` turns `None` into
        `SandboxGoneError` (restores the last save over the container), and
        `restore_from_snapshot` provisions over the existing one — so this fallback belongs
        in the point read, not only the scan. `build_sessions.locks.read_registry` mirrors it
        and must behave identically; kept separate (`services/sandbox/` may not import
        `services/build_sessions/`), guarded against drift by `test_key_migration.py`."""
        raw = await get_redis().hgetall(registry_key(user_uuid))
        if raw:
            return {str(k): str(v) for k, v in raw.items()}
        return await self._adopt_a_pre_cutover_record(user_uuid)

    async def _adopt_a_pre_cutover_record(self, user_uuid: uuid.UUID) -> dict[str, str] | None:
        """Migrate one legacy-prefix registry hash into the environment-scoped namespace, on read.

        SINGLE-KEY COMMANDS ONLY, and the legacy key is NOT deleted on read.
        `locks._adopt_a_pre_cutover_record` mirrors this field for field and carries the reasoning
        for both constraints; the two are separate implementations because `services/sandbox/`
        must not import `services/build_sessions/`.

        ONE FIELD IS ADDED RATHER THAN COPIED — the serving proof, which a legacy record cannot
        carry. The mirror adds it too, and for the reason spelled out on the write below; the
        two must agree exactly or `test_key_migration.py` fails."""
        raw = await get_redis().hgetall(legacy_registry_key(user_uuid))
        if not raw:
            return None

        if await get_redis().exists(registry_key(user_uuid)):
            # A racing writer created the current record after the HGETALL above; that record is
            # the newer claim, and returning the legacy hash would hand back a superseded
            # `app_name` — a teardown aimed at the wrong container.
            current = await get_redis().hgetall(registry_key(user_uuid))
            return {str(k): str(v) for k, v in current.items()} if current else None

        inherited = {str(k): str(v) for k, v in raw.items()}

        # THE SERVING PROOF IS WRITTEN EXPLICITLY, and that is load-bearing rather than tidy. A
        # verbatim copy would carry no `serving_since`, and an adoption can land at ANY time — a
        # pre-cutover container attached months from now still arrives here — so "absent can only
        # mean pre-cutover" would never become true, and the grandfather arm that reads absence
        # as PROVEN could never be safely retired. Writing a real instant is precisely what makes
        # absence impossible on any record written after the cutover.
        #
        # The instant is this record's OWN `created_at`, and PROVEN is the correct reading for
        # it: the container was scheduled before the stamp existed, and it is very likely serving
        # a citizen at this moment. The alternative — the `""` sentinel — would retire a live
        # preview on the spot, which is the single thing the grandfather arm exists to prevent.
        first_served_at = inherited.get(REGISTRY_FIELD_CREATED_AT, "")
        if not first_served_at:
            # A truncated legacy hash: no birthday to inherit. Falling back to NOW keeps the
            # PROVEN reading without inventing a history — the instant recorded is simply the
            # earliest this platform can honestly claim to have known the container was there.
            _log.warning(
                "adopting a legacy registry record with no created_at; stamping its serving "
                "proof at the adoption instant instead",
                user_id=str(user_uuid),
            )
            first_served_at = datetime.now(UTC).isoformat()

        # Inline comprehension, not the `inherited` variable a reader would reach for first (nor
        # any other `dict[str, str]`): redis-py types `mapping` as `Mapping[FieldT, EncodableT]`
        # whose KEY parameter is invariant, so a named `dict[str, str]` fails every type gate —
        # spread into the literal as well — while the identical inline literal passes.
        await get_redis().hset(
            registry_key(user_uuid),
            mapping={
                **{str(k): str(v) for k, v in raw.items()},
                REGISTRY_FIELD_SERVING_SINCE: first_served_at,
            },
        )
        _log.info(
            "sandbox_registry_migrated_to_the_environment_namespace",
            user_id=str(user_uuid),
            detail=(
                "a record written before R22, copied under the environment prefix; the legacy "
                "key is left for _delete_registry, never removed on read"
            ),
        )
        # The serving proof goes back to the caller, not just into the hash: it is exactly a
        # field consumers branch on, and a caller handed a record whose stamp it cannot see
        # would have to re-read the key to learn what this write just put there.
        return inherited | {REGISTRY_FIELD_SERVING_SINCE: first_served_at}

    async def _delete_registry(self, user_uuid: uuid.UUID) -> None:
        """Clear the record under BOTH prefixes — the only place the legacy key is removed, since
        migration-on-read deliberately leaves it. Two single-key DELs, not one two-key `DEL`:
        the keys hash to different slots and a multi-key command is rejected on a clustered
        Redis."""
        await get_redis().delete(registry_key(user_uuid))
        await get_redis().delete(legacy_registry_key(user_uuid))

    # --- token_ref map -------------------------------------------------------

    def _register_token(self, token: str) -> str:
        token_ref = secrets.token_urlsafe(_TOKEN_REF_BYTES)
        self._token_refs[token_ref] = token
        return token_ref

    def _evict_token(self, token: str) -> None:
        for ref in [ref for ref, tok in self._token_refs.items() if tok == token]:
            self._token_refs.pop(ref, None)

    async def _recover_token(self, token_ref: str, app_name: str) -> str | None:
        """Re-read a container's supervisor bearer from its ACA env, re-bound to the registry's
        `token_ref`, or `None` when it cannot be recovered.

        AN UNRESOLVABLE REF SAYS NOTHING ABOUT THE CONTAINER: `_token_refs` is process memory, so a
        restart empties it — and reading that as `SandboxGoneError` used to roll every citizen with
        an open sandbox back to their last save on a routine deploy. The token is minted per
        container into its ACA env at create; that env is its durable home and this process's map
        was only ever a cache. Never logged."""
        try:
            token = await self._aca.get_app_env_value(name=app_name, key=_SUPERVISOR_TOKEN_ENV)
        except (AcaError, AcaTransientError):  # fmt: skip  # ruff py314 strips parens
            _log.warning("supervisor_token_recovery_failed", app_name=app_name, exc_info=True)
            return None
        if token is None:
            return None
        self._token_refs[token_ref] = token
        _log.info("supervisor_token_recovered_from_container_env", app_name=app_name)
        return token

    # --- ACA lifecycle -----------------------------------------------------

    async def _safe_teardown(self, app_name: str) -> bool:
        """Best-effort ACA delete for a self-clean path. Returns True when the container is
        CONFIRMED gone, False when ARM refused.

        LOAD-BEARING, not informational: a caller that reads "I tried" as "it is gone" and
        drops the ownership record leaves a container running, unreachable by the product,
        invisible to the reaper, and billing forever. Only positive confirmation may authorise
        that step — ARM's DELETE returns 204 for a resource that does not exist, so a successful
        call genuinely means absent; a timeout is not a death certificate."""
        try:
            await self._aca.delete_app(name=app_name)
        except (AcaError, AcaTransientError):  # fmt: skip  # ruff py314 strips parens
            _log.exception("ACA self-clean teardown failed", app_name=app_name)
            return False
        return True

    async def _create_with_retry(
        self, app_name: str, env: dict[str, str], tags: dict[str, str], *, arm: _BirthArm
    ) -> str:
        """Create the ACA container, retrying the transient failures. `arm` is carried for the
        success notice below and nothing else — the two births are otherwise identical here.

        THE ARM LAYER USED TO BE SILENT ON SUCCESS, so the most expensive step of a build left
        no trace of how long it took or how many attempts it cost; the terminal failure below
        still speaks only by raising, which its caller is what turns into a citizen-visible
        answer. The notice is emitted HERE rather than at the call site because this is the only
        scope that can see the attempt count and time the whole ladder, backoff sleeps
        included."""
        started = time.monotonic()
        delay = _ACA_RETRY_START_SECONDS
        last: Exception | None = None
        for attempt in range(_ACA_MAX_ATTEMPTS):
            try:
                fqdn = await self._aca.create_app(name=app_name, env=env, tags=tags)
            except AcaTransientError as exc:
                last = exc
                if attempt >= _ACA_MAX_ATTEMPTS - 1:
                    break
                await _asleep(delay)
                delay = min(delay * 2, _ACA_RETRY_MAX_SECONDS)
            except AcaError as exc:
                last = exc
                break
            else:
                # Deferred import — see the cycle note at the top of this module.
                from src.services.build_sessions.alarms import SANDBOX_CONTAINER_CREATED_EVENT

                # `fqdn_present` is the BOOL and not the FQDN: its presence is the only bit
                # anyone reads off a create, and a half-formed ARM reply is not worth the line
                # width. `attempts` is 1-based so the number reads as "it took three goes",
                # not as an index.
                _log.info(
                    SANDBOX_CONTAINER_CREATED_EVENT,
                    arm=arm,
                    create_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempt + 1,
                    fqdn_present=bool(fqdn),
                )
                return fqdn
        # Terminal after the container may partially exist: self-clean any half-created
        # revision (idempotent) so nothing invisible-to-the-reaper leaks, then raise.
        await self._safe_teardown(app_name)
        raise SandboxError("ACA container provisioning failed") from last

    async def _provision_container(
        self,
        user_uuid: uuid.UUID,
        app_name: str,
        app_env: dict[str, str],
        *,
        arm: _BirthArm,
        kind: Literal["build_sandbox", "shared_sandbox"] = "build_sandbox",
        shared_project_id: uuid.UUID | None = None,
        shared_owner_id: uuid.UUID | None = None,
    ) -> SandboxHandle:
        """Create the container, write the registry hash at container-create (before
        any fallible post-create step, so a mid-provision death is reaper-visible), and
        return a `ready=False` handle. Self-cleans on a post-create failure.

        `arm` names WHICH birth this is, for the create notice. It is a `Literal` and not a
        bare `str` so a second spelling of either value is a type error rather than a field
        that quietly stops matching in the log — the same discipline the event names get.

        `kind` (#198) selects which ARM identity gets stamped — see
        `SandboxClient.restore_from_snapshot`'s own docstring for why `user_uuid` means the
        RECIPIENT, not the app's owner, on the `shared_sandbox` arm. `shared_project_id`/
        `shared_owner_id` are the registry-hash half of that same distinction — see
        `_write_registry`'s own docstring — and are only ever non-`None` on that arm."""
        token = secrets.token_urlsafe(_SUPERVISOR_TOKEN_BYTES)
        # The supervisor bearer lives ONLY in the container env (the supervisor keeps it out of
        # the scrubbed child env) and in-process; Redis stores a token_ref, never the token.
        #
        # WHERE THIS APP IS SERVED FROM, derived here rather than passed in. This is the one seam
        # BOTH births pass through — `provision_new` and `restore_from_snapshot` — so a restored
        # sandbox comes back at the same path for free, and a relaunch cannot strand the preview
        # at an address the router will never produce. It is deliberately NOT in
        # `build_app_env`: the publish path calls that same builder, and a base path added there
        # would ship an `sbx-` value into published containers whose images were built with a
        # `pub-` one. `app_name` is in scope here and is exactly the key the router matches on.
        env = {
            **app_env,
            _SUPERVISOR_TOKEN_ENV: token,
            "BIAL_BASE_PATH": base_path_for(app_name),
            "BIAL_APPS_HOSTNAME": _apps_hostname(),
        }
        # Identity resolved BEFORE the create, so a container never exists untagged. The app_id
        # comes from `app_env` for the same reason `restore_from_snapshot` reads it there: the
        # frozen client signature carries no app_id. A `KeyError` here means the env builder
        # upstream is broken, which is worth failing loudly on rather than provisioning an
        # anonymous container to paper over.
        app_id = uuid.UUID(app_env["BIAL_APP_ID"])
        tags = (
            shared_sandbox_tags(recipient_id=user_uuid, app_id=app_id)
            if kind == "shared_sandbox"
            else sandbox_tags(user_id=user_uuid, app_id=app_id)
        )
        fqdn = await self._create_with_retry(app_name, env, tags, arm=arm)
        token_ref = self._register_token(token)
        self._app_owners[app_name] = user_uuid
        try:
            await self._write_registry(
                user_uuid,
                app_name=app_name,
                fqdn=fqdn,
                token_ref=token_ref,
                shared_project_id=shared_project_id,
                shared_owner_id=shared_owner_id,
            )
        except Exception:
            await self._safe_teardown(app_name)
            self._evict_token(token)
            self._app_owners.pop(app_name, None)
            raise
        return SandboxHandle(
            fqdn=fqdn,
            token=token,
            app_name=app_name,
            # THE BROWSER-FACING ADDRESS, which is no longer this container's own name. An
            # internal Container Apps environment publishes no public DNS, so a BIAL desk cannot
            # resolve `{fqdn}` at all — apps are reached through the platform's router on one
            # public hostname with the app's key in the path. The control plane keeps using the
            # direct address: `/_sup/*` composes from `fqdn`, and both serving probes compose
            # from `handle.app_root_url`, which also derives from `fqdn`. Repointing this field
            # therefore cannot drag control-plane traffic onto the public gateway.
            preview_url=_public_app_url(app_name),
            ready=False,
        )

    async def provision_new(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        handle = await self._provision_container(
            uuid.UUID(user_id), app_name, app_env, arm="provision_new"
        )
        await _make_it_a_repo(self, handle)
        return handle

    async def _probe_with_retry(self, handle: SandboxHandle) -> None:
        delay = _PROBE_START_SECONDS
        for attempt in range(_PROBE_MAX_ATTEMPTS):
            try:
                resp = await self._get(handle, "health", timeout=_OP_TIMEOUT_SECONDS)
                if resp.status_code == 200:
                    return
            except SandboxError:
                pass  # transient transport error — retry below before deciding gone
            if attempt < _PROBE_MAX_ATTEMPTS - 1:
                await _asleep(delay)
                delay = min(delay * 2, _PROBE_MAX_SECONDS)
        # Exhausted: confirm whether the container is genuinely gone (ACA revision
        # absent -> restore) or just unreachable (exists -> retryable, never a false
        # double-allocation that would orphan a live original). A transient ARM error while
        # confirming is NOT "gone" — it must stay retryable, so map it to NotReady.
        try:
            fqdn = await self._aca.get_app_fqdn(name=handle.app_name)
        except (AcaError, AcaTransientError) as exc:
            raise SandboxNotReadyError("could not confirm container liveness") from exc
        if fqdn is None:
            raise SandboxGoneError("container revision is gone")
        raise SandboxNotReadyError("supervisor unreachable but the container still exists")

    async def attach_existing(self, user_id: str) -> SandboxHandle:
        user_uuid = uuid.UUID(user_id)
        reg = await self._read_registry(user_uuid)
        if reg is None:
            raise SandboxGoneError("no live sandbox registered for user")
        if reg.get(REGISTRY_FIELD_STATE) == REGISTRY_STATE_ENDING:
            # The reaper marked this ending BEFORE teardown — do NOT reconnect to a
            # dying container. Raise before probing.
            raise SandboxGoneError("sandbox is ending")
        fqdn = reg.get(REGISTRY_FIELD_FQDN, "")
        app_name = reg.get(REGISTRY_FIELD_APP_NAME, "")
        token_ref = reg.get(REGISTRY_FIELD_TOKEN_REF)
        token = self._token_refs.get(token_ref) if token_ref else None
        if token is None and token_ref and app_name:
            # A restart empties the in-process map, and that emptiness is not evidence about the
            # container: recover the bearer from its ACA env rather than concluding "gone",
            # which the caller answers by restoring over a container that is still running.
            token = await self._recover_token(token_ref, app_name)
        if token is None:
            # Recovery failed. Distinguish HONESTLY, because the two answers have very different
            # costs: a container ARM confirms is gone has nothing to lose and should be restored,
            # while a container we merely cannot authenticate to right now must NOT be destroyed
            # over a transient control-plane failure.
            #
            # The ACA read is GUARDED, matching `_probe_with_retry`'s identical confirmation
            # step. `AcaError`/`AcaTransientError` are not `SandboxError`s, so an unguarded call
            # escaped every handler above this one — and this arm is reached exactly when ARM is
            # already unhappy (the token recovery just failed against it), so a throttle here is
            # the expected shape, not an exotic one. `get_app_fqdn`'s own contract asks the
            # attach caller to map it to NotReady; this is that mapping.
            if app_name:
                try:
                    fqdn_now = await self._aca.get_app_fqdn(name=app_name)
                except (AcaError, AcaTransientError) as exc:
                    raise SandboxNotReadyError("could not confirm container liveness") from exc
                if fqdn_now is not None:
                    raise SandboxNotReadyError("supervisor token temporarily unrecoverable")
            raise SandboxGoneError("token reference not resolvable")
        handle = SandboxHandle(
            fqdn=fqdn,
            token=token,
            app_name=app_name,
            # Same public address as a fresh provision — attach and provision must not disagree
            # about where a person goes, or a relaunched session frames a different URL.
            preview_url=_public_app_url(app_name),
            ready=False,
        )
        await self._probe_with_retry(handle)
        self._app_owners[app_name] = user_uuid
        # `ready` reflects the ACTUAL dev-server state on reattach (SandboxHandle.ready) —
        # a resumed, already-ready sandbox drives the initial-load preview trigger. A
        # transient status error must not fail an attach that just probed healthy: fall back
        # to ready=False (the readiness poll recovers it later).
        try:
            status = await self.dev_status(handle)
        except SandboxError:
            _log.warning(
                "dev_status failed after attach; returning ready=False", app_name=app_name
            )
            return handle
        return replace(handle, ready=status.ready)

    async def _restore_snapshot_into(self, handle: SandboxHandle, bundle: bytes) -> None:
        """Push an ALREADY-FETCHED bundle into the container. The fetch itself belongs to the
        caller, above the teardown — see `restore_from_snapshot`."""
        encoded = base64.b64encode(bundle).decode("ascii")
        await self.files(handle, FileCreate(path=_BUNDLE_B64_NAME, file_text=encoded))
        result = await self.exec(
            handle, ["sh", "-c", _RESTORE_SCRIPT], timeout_s=_RESTORE_TIMEOUT_SECONDS
        )
        if result.exit != 0:
            raise SandboxError(f"snapshot restore failed (exit {result.exit})")

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
        user_uuid = uuid.UUID(user_id)
        # The caller supplies the app_id via app_env (the frozen client signature carries no
        # app_id).
        app_id = uuid.UUID(app_env["BIAL_APP_ID"])
        key = source_key or snapshot_key(app_id)
        # FETCH AND VALIDATE BEFORE DESTROYING ANYTHING. The pull used to live inside
        # `_restore_snapshot_into`, i.e. two steps AFTER the teardown below — so a missing,
        # unreachable or unreadable bundle tore the live container down and only then
        # discovered it had nothing to put back. The container's tree is the only copy of
        # everything since the user last saved, so that ordering turned "the restore failed"
        # into "the work is gone".
        #
        # Recovery must never require destroying the thing being recovered. Failures here
        # (`StorageNotFoundError`, `StorageError`, `BundleValidationError`) now propagate with
        # the original container still running and still attachable.
        #
        # NOT validated here, deliberately. `parse_bundle_head_sha` reads only the header, so
        # it cannot detect the truncation that actually matters, and gating the restore on it
        # would refuse bundles the container can in fact fetch — trading a narrow, already-
        # covered failure for a broad new one. The fetch's own `StorageNotFoundError` /
        # `StorageError` are the signals worth acting on, and they now arrive before anything
        # is destroyed, which is the whole point of the reorder.
        bundle = await get_storage().get(key)
        # Defensively tear down any live original BEFORE overwriting the registry, so a
        # still-running container is never orphaned by the restore's fresh create.
        existing = await self._read_registry(user_uuid)
        if existing is not None:
            old_app_name = existing.get(REGISTRY_FIELD_APP_NAME)
            if old_app_name and not await self._safe_teardown(old_app_name):
                # ABORT rather than provision over it. `_provision_container` overwrites
                # the user-keyed registry hash with the NEW app name, so continuing here would
                # leave the OLD container running with nothing pointing at it — an anonymous,
                # forever-billing ghost, manufactured by the recovery path itself.
                #
                # Failing is the safe direction: the builder sees a restore that did not happen
                # and can retry, and the old container is still recorded, still attachable, and
                # still reachable by the sweep. Deleting work to make a retry succeed is the
                # trade this whole unit refuses.
                raise SandboxError(
                    "cannot restore: the existing container could not be torn down, and "
                    "provisioning over it would orphan it"
                )
        handle = await self._provision_container(
            user_uuid,
            app_name,
            app_env,
            arm="restore_from_snapshot",
            kind=kind,
            shared_project_id=shared_project_id,
            shared_owner_id=shared_owner_id,
        )
        try:
            await self._restore_snapshot_into(handle, bundle)
        except Exception:
            # Mid-restore death runs MORE fallible steps than provision — self-clean the
            # just-created container, then clear its registry ONLY IF the container is
            # confirmed gone.
            #
            # The registry drop used to be unconditional. `_safe_teardown` swallows an
            # `AcaError`, so a refused delete still dropped the record — orphaning a container
            # that was probably still running. `teardown()` below has always had this right.
            torn_down = await self._safe_teardown(app_name)
            if torn_down:
                await self._delete_registry(user_uuid)
            else:
                _log.error(
                    "restore_cleanup_left_registry_for_the_reaper",
                    app_name=app_name,
                    detail=(
                        "ACA refused the delete during restore cleanup, so the ownership "
                        "record is deliberately kept: a later sweep retries the teardown "
                        "instead of meeting an anonymous container."
                    ),
                )
            self._evict_token(handle.token)
            self._app_owners.pop(app_name, None)
            raise
        return handle

    async def teardown(self, handle: SandboxHandle) -> None:
        try:
            await self._aca.delete_app(name=handle.app_name)
        except (AcaError, AcaTransientError) as exc:
            # Keep the registry so the reaper retries this teardown; don't orphan.
            raise SandboxError("sandbox teardown failed") from exc
        # ACA delete succeeded (or the container was already gone) -> safe to drop the
        # coordination state. The registry is user-keyed, so clear it only for a session
        # this process owns (the reaper clears a crashed session's registry itself).
        self._evict_token(handle.token)
        owner = self._app_owners.pop(handle.app_name, None)
        if owner is not None:
            try:
                await self._delete_registry(owner)
            except RedisError as exc:
                # The ACA delete already succeeded — only the coordination-state cleanup
                # failed. Re-label it to this method's SandboxError-only failure contract
                # (all three callers guard `except SandboxError`) so a bare RedisError never
                # escapes and hits an unguarded surface.
                raise SandboxError("sandbox registry cleanup failed") from exc

    # --- lifecycle -----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the supervisor HTTP pool AND (if built) the ACA client + credential.
        Safe to call more than once."""
        try:
            await self._http.aclose()
        finally:
            if self._aca_lazy is not None:
                await self._aca_lazy.aclose()


# --- accessor singleton (mirrors services/redis/client.py) -------------------

_sandbox_singleton: SandboxClient | None = None


def create_sandbox(config: SandboxConfig) -> AcaSandboxClient:
    """Build the concrete client from a `SandboxConfig`. Opens no ACA connection —
    the httpx pool and ACA client connect lazily on first use."""
    return AcaSandboxClient(config)


def get_sandbox() -> SandboxClient:
    """The configured sandbox client (app-level singleton). Raises if the sandbox is
    unset (genuinely-optional in dev/test; the prod gate in `src.config` requires it),
    so a caller never silently gets a `None` (fail-first)."""
    global _sandbox_singleton
    if _sandbox_singleton is None:
        from src.config import settings  # lazy: avoid an import cycle via src.config

        if settings.sandbox is None:
            raise SandboxNotConfiguredError(
                "sandbox is not configured: set SANDBOX__* env, or call get_sandbox() "
                "only where the sandbox is configured (it is required in production)."
            )
        _sandbox_singleton = create_sandbox(settings.sandbox)
    return _sandbox_singleton


async def aclose_sandbox_singleton() -> None:
    """Close the app-global sandbox client and drop the singleton (wired behind
    `lifecycle.aclose_sandbox`). A no-op when never opened. The close is isolated: a
    raise is logged (never swallowed) but the singleton is STILL reset, so a restart
    never reuses a half-closed client (mirrors `aclose_redis` / `aclose_storage`)."""
    global _sandbox_singleton
    client = _sandbox_singleton
    if client is None:
        return
    try:
        # Only the concrete client owns pools/credentials; a test-injected fake has
        # nothing to close.
        if isinstance(client, AcaSandboxClient):
            await client.aclose()
    except Exception:
        _log.exception("sandbox teardown failed during aclose_sandbox")
    finally:
        _sandbox_singleton = None


def set_sandbox_for_tests(client: SandboxClient | None) -> None:
    """Inject (or clear) the singleton so the reaper — which resolves its client via
    the singleton, not a `Depends` — is test-injectable."""
    global _sandbox_singleton
    _sandbox_singleton = client


def reset_sandbox_for_tests() -> None:
    """Drop the singleton so a suite that builds clients with different configs never
    reuses a stale one across tests."""
    global _sandbox_singleton
    _sandbox_singleton = None
