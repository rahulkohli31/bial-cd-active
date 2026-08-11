"""Concrete C2 `SandboxClient` — the control-plane wrapper over the C1 supervisor.

This is the ACA/helper client SESSION-API supplies in Wave 1 behind the frozen C2
ABC (`base.py`). Two halves:

* **The `/_sup/*` supervisor HTTP layer** (U1: `exec` / `files` / `dev_start` /
  `dev_status` / `dev_logs` / `wait_ready`) — everything BRAIN calls through the
  injected client, plus the readiness poll. Each call goes to
  `https://{handle.fqdn}/_sup/<endpoint>` with `Authorization: Bearer {handle.token}`;
  Caddy strips `/_sup`, so the supervisor sees the C1 paths (C1 / C2). A non-zero
  `ExecResult.exit` is a NORMAL return, never an exception (C1).
* **The ACA lifecycle** (U2: `provision_new` / `attach_existing` /
  `restore_from_snapshot` / `teardown`) — the container create/delete (via `aca.py`),
  the C5 registry hash, the in-process `token_ref` map, and the C4 restore pull.

Accessor mirrors `services/redis/client.py`: a module singleton, lazy `settings`
import (avoids the `src.config` cycle), a typed `SandboxNotConfiguredError` when
`settings.sandbox is None` (fail-first — never returns `None`), and an isolated
`aclose`. `set_sandbox_for_tests` lets the reaper's client (which reads the
singleton, not a `Depends`) be injected in tests (KTD-9). NEVER log a token or a
`token_ref` (security.md).
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import structlog
from redis.exceptions import RedisError

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
    sandbox_tags,
)
from src.services.sandbox.config import SandboxConfig
from src.services.storage import get_storage, snapshot_key

_log = structlog.get_logger()

# Caddy routes `/_sup/*` to the supervisor (stripping the prefix); every call the
# client makes carries it. `handle.fqdn` is host-only (no scheme), so we prepend https.
_SUP_PREFIX: Final = "/_sup"

# Per-request timeouts (seconds). A command run gets the caller's `timeout_s` plus
# head-room for the round trip so a C1 504 (which IS a `SandboxError`) is the
# supervisor's own timeout, not the transport pre-empting it. Dev/files ops are quick.
_EXEC_TIMEOUT_BUFFER_SECONDS: Final = 30.0
_OP_TIMEOUT_SECONDS: Final = 30.0

# Readiness-poll cadence (C2): start ~0.5 s, exponential backoff capped ~5 s, poll
# until `dev/status.ready` or `timeout_s`. Module-level so tests can shrink them.
_READY_POLL_START_SECONDS: Final = 0.5
_READY_POLL_MAX_SECONDS: Final = 5.0
_READY_BACKOFF_FACTOR: Final = 2.0

# The first-route warm request (U3/R3). Turbopack compiles a route on its FIRST request, and
# until now that request was the citizen's own — 5-7s of blank iframe at the exact moment they
# had just been told their app was ready. The platform pays it instead.
#
# This bound is a LATENCY CEILING, not a safety margin, and it is sized accordingly. Every caller
# awaits this call inline on a path a human is waiting on: the `preview_ready` frame, the
# `POST /relaunch` response, a self-heal iteration. "It gates nothing" (R6) is true and says
# nothing about cost — a generous budget here buys a slow app the right to hold a preview frame
# hostage, which is the outcome U3 exists to prevent. 8s covers the measured 5-7s cold compile
# plus a server-render against the per-project database; past that the wait is worth more than
# the compile, and U5's on-load reveal is the frontend's own insurance either way.
_WARM_TIMEOUT_SECONDS: Final = 8.0

# `dev_start` returns the child pid; on C1's 409 "already running" the running dev
# server exposes no pid (C1 `/dev/status` = {running, ready, port}), so we confirm it
# is up and return this sentinel — 0 is never a real Popen pid, so it unambiguously
# reads as "already running, pid unknown" without raising (idempotent, C2).
_ALREADY_RUNNING_PID: Final = 0

# Supervisor bearer token + registry token_ref sizing (secrets, never a UUID — ADR-0006).
_SUPERVISOR_TOKEN_BYTES: Final = 32
# The container-env key the supervisor bearer is injected under at create. Named rather than
# inlined because it is now read back as well as written: it is the token's durable home across
# a control-plane restart (`_recover_token`), so the two sites must never drift apart.
_SUPERVISOR_TOKEN_ENV: Final = "SUPERVISOR_TOKEN"
_TOKEN_REF_BYTES: Final = 16

# Capped exponential backoff for transient ACA provisioning errors.
_ACA_MAX_ATTEMPTS: Final = 4
_ACA_RETRY_START_SECONDS: Final = 1.0
_ACA_RETRY_MAX_SECONDS: Final = 8.0

# Capped retry for the attach reachability probe (a single blip must NOT map to Gone).
_PROBE_MAX_ATTEMPTS: Final = 4
_PROBE_START_SECONDS: Final = 0.5
_PROBE_MAX_SECONDS: Final = 4.0

# The C4 restore transport (KTD-7): the base64'd git bundle is written into the
# workspace, decoded, and unbundled onto the fresh container's disk, then `npm install`
# reconciles the dynamic deps the snapshotted lockfile added on top of the pre-baked base
# (U6 / R16). The baked node_modules survives `checkout -q -f` (there is NO `git clean`),
# so this is a DELTA install, not a full reinstall — `npm install` (not `npm ci`, which
# would wipe node_modules and defeat the baked base's speed). A non-zero install aborts the
# `set -e` script → non-zero exit → `_restore_snapshot_into` raises SandboxError → the caller
# self-cleans the container (R17). Single-line POSIX `sh -c` (the image ships LF from the
# Windows build VM). Track SANDBOX live-validates the exact shell; here it is a mock-driven seam.
#
# THE RECONCILE IS CONDITIONAL (#11/R4): the baked lockfile is hashed BEFORE the checkout and
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
# without one, and everything downstream assumes a repo exists:
#
#   * the agent is instructed to commit each coherent slice of its work (W1). Without a repo
#     every one of those commits fails with "not a git repository", on precisely the build
#     where the history would be most useful — the first one. The commit-reminder counter
#     never resets either, so the model is nagged for the rest of the run about something it
#     cannot do.
#   * the save-state check reads `git rev-parse HEAD` / `git status --porcelain`.
#   * `write_snapshot` runs its own `git init` as a fallback, which works but collapses the
#     whole first build into a single commit — the per-slice history is simply gone.
#
# So a container is made a working repo at BIRTH, with one baseline commit for the template.
# The agent's commits are then real deltas against a known starting point. Idempotent
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
    # Reconcile dynamic deps (U6/R16) — ONLY when the snapshot's lockfile drifted from the
    # baked one (#11/R4); an unchanged lockfile is already satisfied by the baked node_modules.
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
    this case. Logged rather than swallowed: a repo that never got created explains a later run
    of failed agent commits, and that trail has to exist somewhere."""
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


class AcaSandboxClient(SandboxClient):
    """The concrete C2 client. Holds one long-lived `httpx.AsyncClient` for the
    supervisor calls and (lazily) one `AcaControlPlane` for the container lifecycle.

    `transport` injects an `httpx.MockTransport` for the `/_sup/*` layer; `aca`
    injects a fake control plane — together they let every behavior be tested without
    a live container or real Azure (KTD-9)."""

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
        # token_ref (the C5 registry reference) -> the live bearer token. In-process
        # only; a restart empties it -> references resolve to nothing -> SandboxGoneError
        # -> the restore path (KTD-7). NEVER persisted to Redis.
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
        """Every sandbox container ARM knows about, carrying the identity that lets each one be
        judged with the coordination store gone — the fleet view the Redis-driven reaper cannot
        produce (`build_sessions/inventory.py` explains why it is needed).

        Deliberately NOT on the `SandboxClient` ABC: that is a frozen cross-track contract (C2),
        and this is an operator-facing capability rather than part of the per-sandbox lifecycle
        every caller depends on. Satisfies `inventory.FleetLister` by shape; see C2
        §"Fleet capability lives OFF the ABC" for why it is not an abstractmethod.

        `AcaError` is translated to `SandboxError` here because this is the PORT: no vendor type
        crosses it (C2), and a caller that had to know about `azure.core` to catch a failure would
        be importing the SDK to talk to the abstraction that exists to hide it. The predecessor
        `list_sandbox_app_names` omitted this translation, so an ARM throttle during
        `reconcile-sandboxes` escaped the route's `except SandboxError` and surfaced as a 500
        instead of the documented retryable 503 — a bug this collapse removes rather than
        inherits."""
        try:
            return await self._aca.list_sandbox_fleet()
        except AcaError as exc:
            raise SandboxError("could not enumerate the sandbox fleet") from exc

    async def get_app_tags(self, *, name: str) -> dict[str, str] | None:
        """One container's current tags — the destroy path's re-validation read (U15).

        `AcaError` is translated at the PORT, like its neighbours: no vendor type crosses it."""
        try:
            return await self._aca.get_app_tags(name=name)
        except AcaError as exc:
            raise SandboxError("could not read the container's tags") from exc

    async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None:
        """Merge C10 identity onto an existing container (the backfill's write half).

        A MERGE PATCH, never a PUT — a PUT would replace the resource and take a live sandbox's
        container env, and with it the supervisor bearer, down with it."""
        try:
            await self._aca.stamp_tags(name=name, tags=tags)
        except AcaError as exc:
            raise SandboxError(f"could not stamp identity onto {name}") from exc

    # --- supervisor HTTP layer (U1) ------------------------------------------

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
            # C1 504 (supervisor timeout) and any other non-200 are a SandboxError; a
            # non-zero EXIT would have come back inside a 200 (handled below).
            raise SandboxError(f"command run failed with status {resp.status_code}")
        data: Any = resp.json()
        return ExecResult(
            stdout=str(data["stdout"]), stderr=str(data["stderr"]), exit=int(data["exit"])
        )

    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        # Serialize the validated variant back to C1's flat `FilesBody` wire shape;
        # `exclude_none` drops the fields this variant doesn't carry.
        body = op.model_dump(exclude_none=True)
        resp = await self._post(handle, "files", body, timeout=_OP_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            # C1 422 (0/N str_replace matches) and 400 (missing sub-field / unknown
            # action) both surface as SandboxError (C2).
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
            # (C2) and return the already-running sentinel — C1 exposes no pid here.
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
            # A malformed 200 body must stay inside the C2 taxonomy, exactly as `dev_status`
            # below already does. TWO callers now guard this call with `except SandboxError`
            # and treat it as best-effort — the Write turn's boot-at-attach and relaunch's
            # attach arm — so a raw `KeyError` escaping here would skip both guards and kill a
            # turn whose workspace had already been reported ready.
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
            return DevStatus(
                running=bool(data["running"]),
                ready=bool(data["ready"]),
                port=int(data["port"]),
                exit_code=None if raw_exit is None else int(raw_exit),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # A malformed 200 body must stay inside the C2 taxonomy: every best-effort
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
        # Map the C1 wire field `next` -> `DevLogs.next_cursor` (renamed only to avoid
        # shadowing the builtin); pass it back as `since` for only-new lines (C2).
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

    async def someone_has_to_go_first(self, handle: SandboxHandle) -> int | None:
        """Pay the first Turbopack route compile so the citizen's browser does not (U3, R3).

        Straight at the app root over the SAME public ingress the browser uses — deliberately
        NOT through the supervisor's `/exec` + `curl`. One Caddy on one FQDN fronts both
        (`/_sup/*` → supervisor, `/*` → next dev), so `/exec` is not a cheaper local hop: it is
        the identical round trip plus a process spawn, and it warms a path no user ever takes.
        Going in the front door means this request compiles exactly what the citizen will load.

        NON-LOAD-BEARING BY CONSTRUCTION (R6). It gates nothing and raises nothing, and it
        carries its own timeout: the frame this precedes is worth more than the compile it pays
        for, so a warm request that hangs must cost the preview NOTHING. The status code comes
        back for callers that want the signal — a 500 here is a real compile error — but no
        caller may make the frame conditional on it.

        A non-2xx is LOGGED here rather than left to the callers, because every one of them
        currently discards the return value. Only the exception path was observable, so a root
        route answering 500 on every build looked identical in telemetry to a healthy one — and
        `selfheal.verify` decides green/red from five hard-coded text markers, so a 500 that
        prints none of them ships green. This does not change any outcome; it makes the outcome
        visible.

        TREAT THE RESPONSE AS HOSTILE. It is produced by unreviewed, agent-authored code
        running in the citizen's sandbox, and this is the one call in this client that leaves
        the supervisor's bearer-guarded surface for the app's own. Two consequences are baked
        into the shape below and must not be relaxed:

        - NO REDIRECT FOLLOWING. A 3xx already proves the route compiled, which is the whole
          job here. Following one would let generated code choose the next URL and turn this
          into a blind SSRF pivot from the control plane's network position — on every preview
          frame, every relaunch and every self-heal iteration.
        - HEADERS ONLY, NEVER THE BODY. `stream` returns as soon as the status line lands and
          the context manager closes without reading further, so a hostile app cannot make the
          control plane buffer an unbounded body. (The supervisor's own probe stops at headers
          for the same reason.) `asyncio.timeout` is what makes the budget a TOTAL ceiling —
          httpx's own timeout is per-network-operation, so a slow trickle resets it forever.

        The blind `except` is the requirement, not an oversight: any exception escaping here
        would hold a preview hostage to an optimization, and narrowing it has bitten this file
        before (see `_make_it_a_repo` — a narrow catch let a `KeyError` through and killed a
        provision that had already succeeded). `CancelledError` is a `BaseException`, so a
        cancelled turn still cancels; only the timeout's own expiry is swallowed here.
        """
        try:
            async with (
                asyncio.timeout(_WARM_TIMEOUT_SECONDS),
                self._http.stream("GET", handle.preview_url) as resp,
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
        except Exception:  # noqa: BLE001 - R6: nothing from here may ever reach the caller
            # `exc_info` is not decoration: U4's whole detection story depends on this request
            # reaching the route, and a silent swallow makes restricted egress or a wedged
            # ingress look identical to a healthy build. Without the reason, the one telemetry
            # signal that says "the warm request is a no-op in production" says nothing.
            _log.warning("warm_request_failed", app=handle.app_name, exc_info=True)
            return None

    # --- C5 registry helpers (frozen key builders — never a hand-typed key) --

    async def _write_registry(
        self, user_uuid: uuid.UUID, *, app_name: str, fqdn: str, token_ref: str
    ) -> None:
        """Hydrate the C5 registry hash for a JUST-CREATED container.

        `hset(mapping=…)` is a MERGE, so this is also the point where a previous
        occupant's fields must be actively disowned. `preview_stay_until` is the one that
        matters (#43): a relaunched preview's 30-minute lease can outlive its own registry
        hash whenever `reap_user`'s teardown raises (that arm deliberately KEEPS the
        registry for a later sweep), and a preview holds no lock — so the user's next real
        build acquires cleanly, re-registers over the surviving hash, and would INHERIT a
        lease it never asked for. If that build's process then died, the sweep would spare
        its orphaned container for the rest of the half hour: the protection inverted into
        a leak. A freshly written registry therefore carries NO stay, always."""
        key = registry_key(user_uuid)
        await get_redis().hset(
            key,
            mapping={
                REGISTRY_FIELD_APP_NAME: app_name,
                REGISTRY_FIELD_FQDN: fqdn,
                REGISTRY_FIELD_TOKEN_REF: token_ref,
                REGISTRY_FIELD_CREATED_AT: datetime.now(UTC).isoformat(),
                REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
            },
        )
        await get_redis().hdel(key, REGISTRY_FIELD_PREVIEW_STAY_UNTIL)

    async def _read_registry(self, user_uuid: uuid.UUID) -> dict[str, str] | None:
        """Read the sandbox record, falling back to the pre-R22 key and migrating what it finds.

        DUAL-READ (C5's R22 window). The environment segment was added to the key root while a
        live fleet was registered under the old one, and the registry hash carries no TTL — so a
        read that only knew the new key would answer `None` for every pre-cutover container. Two
        things downstream of here treat that `None` as fact and act destructively on it:
        `attach_existing` raises `SandboxGoneError`, whose caller RESTORES — tearing the live
        container down and rolling the builder back to their last save; and
        `restore_from_snapshot` skips its defensive teardown of the existing container and
        provisions over it, orphaning a running container with nothing pointing at it.

        `build_sessions.locks.read_registry` is the other point read and must behave identically.
        C5 keeps the two separate deliberately — `services/sandbox/` may not import
        `services/build_sessions/` — so the thing stopping them from drifting is
        `tests/services/build_sessions/test_key_migration.py`, not a shared module.
        """
        raw = await get_redis().hgetall(registry_key(user_uuid))
        if raw:
            return {str(k): str(v) for k, v in raw.items()}
        return await self._adopt_a_pre_cutover_record(user_uuid)

    async def _adopt_a_pre_cutover_record(self, user_uuid: uuid.UUID) -> dict[str, str] | None:
        """Migrate one legacy-prefix registry hash into the environment-scoped namespace, on read.

        SINGLE-KEY COMMANDS ONLY, and the legacy key is NOT deleted on read. Both constraints
        and their full reasoning live on `locks._adopt_a_pre_cutover_record`, which this mirrors
        field for field — C5 keeps the two point reads as separate implementations because
        `services/sandbox/` must not import `services/build_sessions/`, so the contract is the
        doc, not a shared function. In short: `COPY`/multi-key `DEL` are cross-slot and rejected
        on a clustered Redis, and deleting the legacy key on read lets a mispointed process
        relocate another environment's record and orphan its container."""
        raw = await get_redis().hgetall(legacy_registry_key(user_uuid))
        if not raw:
            return None

        if await get_redis().exists(registry_key(user_uuid)):
            # A racing writer created the current record after the HGETALL above; that record is
            # the newer claim, and returning the legacy hash would hand back a superseded
            # `app_name` — a teardown aimed at the wrong container.
            current = await get_redis().hgetall(registry_key(user_uuid))
            return {str(k): str(v) for k, v in current.items()} if current else None

        # Inline comprehension, not a `dict[str, str]` variable: redis-py types `mapping` as
        # `Mapping[FieldT, EncodableT]` whose KEY parameter is invariant, so a named
        # `dict[str, str]` fails every type gate while the identical inline literal passes.
        await get_redis().hset(
            registry_key(user_uuid), mapping={str(k): str(v) for k, v in raw.items()}
        )
        _log.info(
            "sandbox_registry_migrated_to_the_environment_namespace",
            user_id=str(user_uuid),
            detail=(
                "a record written before R22, copied under the environment prefix; the legacy "
                "key is left for _delete_registry, never removed on read"
            ),
        )
        return {str(k): str(v) for k, v in raw.items()}

    async def _delete_registry(self, user_uuid: uuid.UUID) -> None:
        """Clear the record under BOTH prefixes — the only place the legacy key is removed, since
        migration-on-read deliberately leaves it. Two single-key DELs, not one two-key `DEL`:
        the keys hash to different slots and a multi-key command is rejected on a clustered
        Redis. See `locks.delete_registry`. Release B drops the legacy arm."""
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
        """Re-read a container's supervisor bearer from its own ACA env and re-bind it to the
        registry's `token_ref`, or `None` when it cannot be recovered.

        WHY THIS EXISTS. `_token_refs` is process memory, so every control-plane restart — every
        deploy — emptied it. `attach_existing` read that emptiness as `SandboxGoneError`, the
        caller heard "gone" and restored, and restore tears the live container down before
        pulling the last SAVED bundle. So a routine deploy silently rolled every citizen with an
        open sandbox back to their last save. That is SL-20's data loss reached through a second
        door, and it fires on a schedule rather than on a mistake.

        The premise was simply wrong: an unresolvable ref says nothing about the container. The
        token was minted per container and injected into its ACA env at create, so the container
        app spec is the token's durable home and this process's map was only ever a cache.

        The token is never logged and never returned to a caller that would log it (security.md).
        """
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

    # --- ACA lifecycle (U2) --------------------------------------------------

    async def _safe_teardown(self, app_name: str) -> bool:
        """Best-effort ACA delete for a self-clean path. Returns True when the container is
        CONFIRMED gone, False when ARM refused.

        The return value is load-bearing, not informational (U18). This used to return `None`,
        so every caller treated "I tried" as "it is gone" — and a caller that then dropped the
        ownership record left a running container with nothing pointing at it: unreachable by
        the product, invisible to the Redis-enumerating sweep, and billing forever. That is the
        exact population ADR-0029 exists to collect, manufactured by the recovery path.

        "A timeout is not a death certificate": only positive confirmation may authorise a step
        that assumes the container is gone. ARM's DELETE returns 204 for a resource that does
        not exist, so a successful call genuinely means absent.
        """
        try:
            await self._aca.delete_app(name=app_name)
        except (AcaError, AcaTransientError):  # fmt: skip  # ruff py314 strips parens
            _log.exception("ACA self-clean teardown failed", app_name=app_name)
            return False
        return True

    async def _create_with_retry(
        self, app_name: str, env: dict[str, str], tags: dict[str, str]
    ) -> str:
        delay = _ACA_RETRY_START_SECONDS
        last: Exception | None = None
        for attempt in range(_ACA_MAX_ATTEMPTS):
            try:
                return await self._aca.create_app(name=app_name, env=env, tags=tags)
            except AcaTransientError as exc:
                last = exc
                if attempt >= _ACA_MAX_ATTEMPTS - 1:
                    break
                await _asleep(delay)
                delay = min(delay * 2, _ACA_RETRY_MAX_SECONDS)
            except AcaError as exc:
                last = exc
                break
        # Terminal after the container may partially exist: self-clean any half-created
        # revision (idempotent) so nothing invisible-to-the-reaper leaks, then raise.
        await self._safe_teardown(app_name)
        raise SandboxError("ACA container provisioning failed") from last

    async def _provision_container(
        self, user_uuid: uuid.UUID, app_name: str, app_env: dict[str, str]
    ) -> SandboxHandle:
        """Create the container, write the C5 registry hash at container-create (before
        any fallible post-create step, so a mid-provision death is reaper-visible), and
        return a `ready=False` handle. Self-cleans on a post-create failure."""
        token = secrets.token_urlsafe(_SUPERVISOR_TOKEN_BYTES)
        # The supervisor bearer lives ONLY in the container env (C1 keeps it out of the
        # scrubbed child env) and in-process; Redis stores a token_ref, never the token.
        env = {**app_env, _SUPERVISOR_TOKEN_ENV: token}
        # C10 identity, resolved BEFORE the create so a container never exists untagged. The app_id
        # comes from `app_env` for the same reason `restore_from_snapshot` reads it there: the
        # frozen C2 signature carries no app_id, and C9 guarantees the variable (a KeyError here
        # would mean the C9 contract was broken upstream, which is worth failing loudly on rather
        # than provisioning an anonymous container to paper over).
        tags = sandbox_tags(user_id=user_uuid, app_id=uuid.UUID(app_env["BIAL_APP_ID"]))
        fqdn = await self._create_with_retry(app_name, env, tags)
        token_ref = self._register_token(token)
        self._app_owners[app_name] = user_uuid
        try:
            await self._write_registry(
                user_uuid, app_name=app_name, fqdn=fqdn, token_ref=token_ref
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
            preview_url=f"https://{fqdn}/",
            ready=False,
        )

    async def provision_new(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        handle = await self._provision_container(uuid.UUID(user_id), app_name, app_env)
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
            # The reaper marked this ending BEFORE teardown (C5) — do NOT reconnect to a
            # dying container. Raise before probing.
            raise SandboxGoneError("sandbox is ending")
        fqdn = reg.get(REGISTRY_FIELD_FQDN, "")
        app_name = reg.get(REGISTRY_FIELD_APP_NAME, "")
        token_ref = reg.get(REGISTRY_FIELD_TOKEN_REF)
        token = self._token_refs.get(token_ref) if token_ref else None
        if token is None and token_ref and app_name:
            # A restart empties the in-process map. This USED to raise `SandboxGoneError`, which
            # the caller answers by restoring — and restore tears the live container down before
            # pulling the last SAVED bundle. So every deploy silently rolled every citizen with
            # an open sandbox back to their last save. Same data loss as SL-20, reached through a
            # door that opens on a schedule rather than on a mistake.
            #
            # An unresolvable ref says nothing about the container. The bearer's durable home is
            # the container's own ACA env, where it was injected at create; this map was only
            # ever a cache of it. So re-read it and carry on attaching.
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
            preview_url=f"https://{fqdn}/",
            ready=False,
        )
        await self._probe_with_retry(handle)
        self._app_owners[app_name] = user_uuid
        # `ready` reflects the ACTUAL dev-server state on reattach (C2 SandboxHandle.ready) —
        # a resumed, already-ready sandbox drives BRAIN's initial-load preview trigger. A
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
    ) -> SandboxHandle:
        user_uuid = uuid.UUID(user_id)
        # C9 supplies the app_id via app_env (the frozen C2 signature carries no app_id).
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
        # still-running container is never orphaned by the restore's fresh create (C2).
        existing = await self._read_registry(user_uuid)
        if existing is not None:
            old_app_name = existing.get(REGISTRY_FIELD_APP_NAME)
            if old_app_name and not await self._safe_teardown(old_app_name):
                # ABORT rather than provision over it (U18). `_provision_container` overwrites
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
        handle = await self._provision_container(user_uuid, app_name, app_env)
        try:
            await self._restore_snapshot_into(handle, bundle)
        except Exception:
            # Mid-restore death runs MORE fallible steps than provision — self-clean the
            # just-created container, then clear its registry ONLY IF the container is
            # confirmed gone (C4, U18).
            #
            # The registry drop used to be unconditional. `_safe_teardown` swallows an
            # `AcaError`, so a refused delete still dropped the record — orphaning a container
            # that was probably still running. `teardown()` three methods below has always had
            # this right: "Keep the registry so the reaper retries this teardown; don't orphan."
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
    the singleton, not a `Depends` — is test-injectable (KTD-9)."""
    global _sandbox_singleton
    _sandbox_singleton = client


def reset_sandbox_for_tests() -> None:
    """Drop the singleton so a suite that builds clients with different configs never
    reuses a stale one across tests."""
    global _sandbox_singleton
    _sandbox_singleton = None
