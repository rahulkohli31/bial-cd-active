"""The frozen sandbox-client surface (contract C2): the `SandboxClient` ABC, the
`SandboxHandle` value type, the typed `FileOp` request union + result value types,
and the typed exceptions. This is the control-plane-side wrapper over the C1
supervisor HTTP API.

Mirrors the `ObjectStorage` port (ADR-0009): an `abc.ABC`, NOT a `Protocol`, so
nominal subtyping makes an IDE jump land on the concrete backend and an incomplete
implementation fails at instantiation with a runtime `TypeError`. Stage 0 (U7)
renders this frozen surface with empty bodies; Track SESSION-API supplies the
concrete ACA/helper client in Wave 1, and Track BRAIN imports it READ-ONLY (calls
a subset through an injected client — it never implements or edits this file).

No vendor type crosses this port. Every supervisor call the client makes goes to
`https://{handle.fqdn}/_sup/<endpoint>` with `Authorization: Bearer {handle.token}`
(Caddy strips `/_sup`, so the supervisor sees the C1 paths); `handle.preview_url`
is the un-prefixed `next dev` root `https://{handle.fqdn}/` the portal frames (C8).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- typed exceptions --------------------------------------------------------


class SandboxError(Exception):
    """Base for any sandbox-client failure (a C1 timeout/4xx, an ACA error, an
    unreachable supervisor). A non-zero `ExecResult.exit` is NOT one of these — it
    is a normal return (C1)."""


class SandboxNotReadyError(SandboxError):
    """The readiness poll timed out, or an op needs a ready dev server before it is
    ready. RETRYABLE — the caller may poll again."""


class SandboxGoneError(SandboxError):
    """The C5 registry lists the sandbox as active but the container is unreachable
    / torn down (ACA revision gone, dead FQDN). Signals the caller to RESTORE (or
    provision) rather than retry. Terminal for that handle."""


# --- value types (no vendor type crosses the port) ---------------------------


@dataclass(frozen=True)
class SandboxHandle:
    """The frozen 5-field handle returned by every provision/attach/restore call and
    passed back into every operation (C2)."""

    fqdn: str
    """Public ACA ingress FQDN, host only, NO scheme (e.g. `app-xyz.westeurope.
    azurecontainerapps.io`). All `/_sup/*` calls and `preview_url` derive from it."""
    token: str
    """The per-session supervisor bearer token, sent as `Authorization: Bearer
    {token}` to `/_sup/*`. Held IN-PROCESS only; NEVER persisted raw — the C5
    registry stores a `token_ref` (a reference), not this value."""
    app_name: str
    """The app/container identifier (one-app-per-project); == C5 registry `app_name`."""
    preview_url: str
    """The un-prefixed `next dev` root `https://{fqdn}/` — the browsable preview the
    portal frames cross-origin (C8). Never carries the bearer token."""
    ready: bool
    """Dev-server readiness snapshot (mirrors C1 `/dev/status.ready` — marker seen +
    process alive) at handle construction; refreshed by `wait_ready` / `dev_status`."""


@dataclass(frozen=True)
class ExecResult:
    """Mirrors C1 `POST /exec`. A non-zero `exit` is a NORMAL return, never an
    exception — self-heal reads `exit`/`stderr` off a 200 (C1)."""

    stdout: str
    stderr: str
    exit: int


@dataclass(frozen=True)
class DevStatus:
    """Mirrors C1 `GET /dev/status`. `port` is always 3000 (the `next dev` port).
    `exit_code` is the dead child's post-mortem (None while alive, never started, or when
    talking to a pre-exit_code supervisor image) — 137 is the OOM-killer's signature."""

    running: bool
    ready: bool
    port: int
    exit_code: int | None = None


@dataclass(frozen=True)
class DevLogs:
    """Mirrors C1 `GET /dev/logs`. `next_cursor` is the C1 wire field `next` (renamed
    only to avoid shadowing the builtin); pass it back as `since` for only-new lines."""

    lines: list[str]
    next_cursor: int


@dataclass(frozen=True)
class FileResult:
    """Mirrors the C1 `POST /files` per-action response. `detail` carries the
    action-specific body (`content` for view, `replacements` for str_replace, …)."""

    ok: bool
    detail: dict[str, object]


# --- FileOp: the typed C1 /files request (discriminated on `action`) ---------
#
# A discriminated union over view | str_replace | create | insert carrying the C1
# fields. Each variant declares only its own required fields (fail-first — a
# str_replace with no `old_str` cannot be constructed), unlike C1's flat all-optional
# `FilesBody`; the client serializes the variant back to the C1 wire shape.


class _FileOpBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str


class FileView(_FileOpBase):
    action: Literal["view"] = "view"
    view_range: list[int] | None = None


class FileStrReplace(_FileOpBase):
    action: Literal["str_replace"] = "str_replace"
    old_str: str
    new_str: str


class FileCreate(_FileOpBase):
    action: Literal["create"] = "create"
    file_text: str


class FileInsert(_FileOpBase):
    action: Literal["insert"] = "insert"
    insert_line: int
    insert_text: str


FileOp = Annotated[
    FileView | FileStrReplace | FileCreate | FileInsert,
    Field(discriminator="action"),
]
"""The typed C1 `/files` request. `files()` takes one `FileOp` and returns one
`FileResult`; validation discriminates on `action` (C1 / C2)."""


# --- the frozen client ABC ---------------------------------------------------


class SandboxClient(abc.ABC):
    """The complete async sandbox-client surface (C2). SESSION-API implements every
    method (Wave 1); BRAIN calls the exec/files/dev subset through an injected
    client. Documented poll/timeout/retry defaults are the frozen semantics a mock
    must honor where observable.

    The one-per-user + rehydrate rule is a caller invariant (SESSION-API resolves it
    against the C5 registry + lock before any work): a returning user ATTACHES or
    RESTORES — re-provisioning a user who already holds a live sandbox is a contract
    violation (double allocation)."""

    @abc.abstractmethod
    async def provision_new(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        """Provision a BRAND-NEW container for `user_id`. `app_env` carries the app's
        injected environment (`BIAL_APP_ID`, `BIAL_PORTAL_ORIGIN`, the blob coordinates,
        and the per-project `BIAL_DATABASE_URL`) — every name chosen to survive the C1
        child-env scrub allowlist (D5).
        Returns a handle with `ready=False`. The caller MUST already hold the C5
        one-per-user lock. Transient provisioning errors retried with capped
        exponential backoff."""
        ...

    @abc.abstractmethod
    async def wait_ready(
        self, handle: SandboxHandle, *, timeout_s: float = 120.0
    ) -> SandboxHandle:
        """Poll `GET /_sup/dev/status` until `ready` (marker-seen + process-alive,
        C1) or `timeout_s`. Poll cadence: start ~0.5s, exponential backoff capped at
        ~5s. On timeout raises `SandboxNotReadyError`. Returns a handle with `ready=True`."""
        ...

    @abc.abstractmethod
    async def attach_existing(self, user_id: str) -> SandboxHandle:
        """IDEMPOTENT reconnect to the user's already-running sandbox (FQDN +
        `token_ref`→token from the C5 registry) WITHOUT re-provisioning. If the
        registry lists the user but the container is unreachable raises
        `SandboxGoneError` (the caller should restore)."""
        ...

    @abc.abstractmethod
    async def restore_from_snapshot(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        """Provision a FRESH container and restore the C4 git-bundle snapshot onto
        its local disk (git ops over `/_sup/exec`), then RE-INJECT the C9 app-data
        credential from `app_env`. Returns a handle (`ready=False` until
        `wait_ready` / `dev_start`). See C4 for the pull ordering."""
        ...

    @abc.abstractmethod
    async def exec(
        self,
        handle: SandboxHandle,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout_s: int = 900,
    ) -> ExecResult:
        """Run `cmd` (a list, no shell) under `cwd` (workspace-relative) via
        `POST /_sup/exec`. A non-zero `exit` is a NORMAL `ExecResult`, not an
        exception; a C1 timeout (504) surfaces as `SandboxError`."""
        ...

    @abc.abstractmethod
    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        """One `FileOp` → one `FileResult` via `POST /_sup/files`. C1's 422
        (str_replace 0/N matches) and 400 (missing sub-field / escape / unknown
        action) surface as `SandboxError`."""
        ...

    @abc.abstractmethod
    async def dev_start(
        self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
    ) -> int:
        """Start `next dev` (default `["npm", "run", "dev"]`) via `POST /_sup/dev/start`;
        returns the pid. IDEMPOTENT: C1's 409 "already running" is treated as success
        (returns the existing pid via a status probe) rather than raising."""
        ...

    @abc.abstractmethod
    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        """Current `{running, ready, port, exit_code}` via `GET /_sup/dev/status` (`port`
        always 3000; `exit_code` is the dead child's post-mortem, None while alive or on an
        older supervisor image)."""
        ...

    @abc.abstractmethod
    async def dev_logs(self, handle: SandboxHandle, *, since: int = 0) -> DevLogs:
        """Cursor tail via `GET /_sup/dev/logs?since=N`; pass `DevLogs.next_cursor`
        back as `since` for only-new lines."""
        ...

    @abc.abstractmethod
    async def teardown(self, handle: SandboxHandle) -> None:
        """IDEMPOTENT teardown of the container (delete revision/container). Safe to
        call when already gone (no-op) — required by the C4 snapshot-then-teardown
        and C5 reaper ordering."""
        ...

    # --- outside the frozen set: a courtesy, not a contract ------------------

    async def someone_has_to_go_first(self, handle: SandboxHandle) -> int | None:
        """Pay the app's first route compile so the citizen's browser does not (U3, R3).

        DELIBERATELY NOT abstract, and the reason is a real distinction rather than
        convenience: every method above mirrors one supervisor endpoint, and
        `test_abstractmethod_set_equals_the_c2_contract` pins that set so the C2 surface
        cannot drift. This is not a supervisor call at all — it is an ordinary GET at the
        app's public root, through the same Caddy the citizen's iframe uses. Adding it to
        the frozen set would claim the supervisor grew an endpoint it did not.

        The default declines. A client that fronts no real container has no first route to
        compile, and `None` says exactly that. Non-load-bearing by construction (R6): an
        implementation that overrides it must still never let the call raise, and no caller
        may make a preview frame conditional on what it returns."""
        return None
