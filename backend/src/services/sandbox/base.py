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
import datetime as dt
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Final, Literal

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


SANDBOX_NAME_PREFIX = "sbx-"
"""The prefix every sandbox container app carries (`app_name_for`).

Defined here rather than inline at the mint site because two sides need to agree on it and
they cannot import each other: `manager.app_name_for` WRITES it, and `AcaControlPlane.
list_sandbox_app_names` READS it back to tell our containers from the deployed apps and
unrelated workloads sharing the resource group. A drift between those two would make the
orphan reconciler quietly report nothing."""


# --- ARM identity tags (contract C10, ADR-0029 §2) ---------------------------
#
# A container must be judgeable WITHOUT REDIS (R1). The registry hash is the one C5 family with no
# TTL, it has been lost at least twice, and a container whose record is gone is anonymous:
# unreachable by the product, invisible to every automatic path, and billing at ~$0.108/hr forever.
# So identity lives on the ARM resource, written into the creation envelope so it exists from the
# first moment.
#
# These keys sit here for the same reason `SANDBOX_NAME_PREFIX` does — three writers and (soon) a
# destructive reader have to agree on them and cannot import each other: `sandbox/aca.py` stamps
# them at create, `deploy/aca_publish.py` re-asserts them on every publish PUT, the backfill fills
# them in for containers that predate all this, and the reclamation classifier reads them back. A
# drift in one key is not a typo: it is either a container that never becomes reclaimable, or one
# reclaimed on a misunderstanding.

TAG_KIND: Final = "bial-kind"
"""What the resource IS. Today only the `sbx-`/`pub-` name prefix says this, which is a convention,
rather than a record. Reclamation acts on `KIND_BUILD_SANDBOX` and nothing else."""

TAG_USER_ID: Final = "bial-user-id"
"""The owning user's UUID, in plaintext. R1 requires judging a container without the store, which
rules out an opaque reference needing a database lookup; a UUID is an identifier, not a secret
(ADR-0006), and the resource group is internal-only. Signed off as ADR-0029 accepted risk (b) — it
does surface in cost exports, and that was decided rather than overlooked."""

TAG_APP_ID: Final = "bial-app-id"
"""The app UUID this container serves. Note the name is NOT a substitute: `app_name_for` keeps only
28 of the app_id's 32 hex characters, so a sandbox name is lossy and this tag is the only lossless
back-reference the resource carries."""

TAG_CONTROL_PLANE: Final = "bial-control-plane"
"""Which control plane created it — the environment segment, the tag half of R22. A dev control
plane pointed at the wrong subscription must not be able to judge a production container."""

TAG_CREATED_AT: Final = "bial-created-at"
"""OUR creation timestamp (ISO-8601), never Azure's `systemData.createdAt` (R2).

Azure's behaviour on recreate-under-an-existing-name is undocumented, with no normative statement
either way — and a container that retained an original timestamp across a recreate would read as
permanently overdue, i.e. instantly destroy-eligible while being seconds old. The precedent is
`appdb/provision.py::_stamp_provisioned_at`: when the substrate's own timestamp is untrustworthy,
author your own."""

TAG_BACKFILLED_AT: Final = "bial-backfilled-at"
"""Present ONLY on a container whose identity was reconstructed after the fact. It marks the
`TAG_CREATED_AT` above as synthetic, so the tier clock can tell a real age from a manufactured one
and err toward waiting."""

TAG_RECLAIM_STAGED_AT: Final = "bial-reclaim-staged-at"
"""The two-pass staging marker (ADR-0029 §5). RESERVED — pinned here so U15 inherits the spelling
instead of re-opening it; nothing writes it yet.

Deliberately NOT named or parseable as `ending`: the attach path refuses an `ending` sandbox BEFORE
it probes, and a container merely staged for a second look is still fully attachable. A citizen
coming back to it must get their sandbox, not a refusal."""

KIND_BUILD_SANDBOX: Final = "build-sandbox"
KIND_PUBLISHED_APP: Final = "published-app"

MAX_TAG_VALUE_LENGTH: Final = 256
"""ARM's per-tag-value ceiling. Enforced HERE rather than discovered from an ARM 400 halfway
through a provision — at this boundary the error can name the offending tag."""


class SandboxTagError(SandboxError):
    """A tag value ARM would reject. Terminal: retrying an over-long string does not shorten it."""


def checked_tags(tags: Mapping[str, str]) -> dict[str, str]:
    """Copy `tags`, refusing any value ARM's 256-character ceiling would reject.

    Every value the platform writes is a UUID, an ISO-8601 timestamp or a short constant, so in
    practice only `TAG_CONTROL_PLANE` (a free-text environment segment) can trip this. That is
    exactly why the check exists: an operator who sets a novel `ENVIRONMENT` should learn about it
    from a named error at the seam, not from a 400 on a container create that half-succeeded."""
    for key, value in tags.items():
        if len(value) > MAX_TAG_VALUE_LENGTH:
            raise SandboxTagError(
                f"tag {key!r} is {len(value)} characters; ARM rejects anything over "
                f"{MAX_TAG_VALUE_LENGTH}"
            )
    return dict(tags)


def _uuid_or_none(raw: str | None) -> uuid.UUID | None:
    """Parse a tag value that should be a UUID, treating a malformed one as ABSENT.

    Fail-closed in the direction that matters: an unparseable owner tag means the platform cannot
    prove who owns the container, and "cannot prove" must land in the escalate-only bucket rather
    than raise and take the whole fleet pass down with it (an unreadable signal escalates; it never
    expires into a decision)."""
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _timestamp_or_none(raw: str | None) -> dt.datetime | None:
    """Parse an ISO-8601 tag value, treating a malformed one as ABSENT — same fail-closed argument
    as `_uuid_or_none`. A naive timestamp is read as UTC, because that is what we write."""
    if raw is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


@dataclass(frozen=True)
class SandboxIdentity:
    """What a container app's ARM tags say about itself — the whole of what a reclamation pass gets
    to judge it on when Redis is gone (C10 §1)."""

    kind: str | None
    user_id: uuid.UUID | None
    app_id: uuid.UUID | None
    control_plane: str | None
    created_at: dt.datetime | None
    backfilled_at: dt.datetime | None
    reclaim_staged_at: dt.datetime | None

    @property
    def is_a_sandbox(self) -> bool:
        """Positively identified as a build sandbox. A published app or an untagged resource is
        not."""
        return self.kind == KIND_BUILD_SANDBOX

    @property
    def escalate_only(self) -> bool:
        """No owner, no app, or no age ⇒ REPORT IT, NEVER DESTROY IT (ADR-0029 §3, tier four).

        This is the escalate-never-destroy invariant in one predicate, and it is the reason the
        backfill refuses to guess an owner from a lossy name. A container the platform cannot prove
        it owns stays here forever, which is a bill an operator can see and act on — strictly
        better than the alternative, which is deleting somebody's unsaved work on a near-miss name
        match."""
        return self.user_id is None or self.app_id is None or self.created_at is None

    @property
    def was_backfilled(self) -> bool:
        """Its age is SYNTHETIC — stamped by the backfill, not by the code that created it. The
        tier clock must run from that stamp, so a backfilled container reads as new and serves its
        full clock before it is eligible for anything."""
        return self.backfilled_at is not None


def identity_from_tags(tags: Mapping[str, str] | None) -> SandboxIdentity:
    """Read a container app's identity off its ARM tags. NEVER raises.

    `None` is the input this function exists for. On an untagged app ARM omits the `tags` key
    entirely — not `{}`, not `null`, verified live against every app in `bial-dev-rg` — and that
    shape IS the orphan population. A parser that raised on it would blind the reclamation system
    to exactly the containers it was built to collect."""
    raw: Mapping[str, str] = tags or {}
    return SandboxIdentity(
        kind=raw.get(TAG_KIND),
        user_id=_uuid_or_none(raw.get(TAG_USER_ID)),
        app_id=_uuid_or_none(raw.get(TAG_APP_ID)),
        control_plane=raw.get(TAG_CONTROL_PLANE),
        created_at=_timestamp_or_none(raw.get(TAG_CREATED_AT)),
        backfilled_at=_timestamp_or_none(raw.get(TAG_BACKFILLED_AT)),
        reclaim_staged_at=_timestamp_or_none(raw.get(TAG_RECLAIM_STAGED_AT)),
    )


def control_plane_segment() -> str:
    """This process's environment segment — the `TAG_CONTROL_PLANE` value (R22).

    Imported inside the function, and it is not laziness for its own sake: `src.config` reaches
    `src.settings.capabilities`, which reaches the sandbox config, and a module-level import here
    would close that cycle. Resolved per call rather than memoized, for the same reason
    `redis/keys.py::_environment` is: the segment is a property of the running settings, not of
    import order, which no deployment controls."""
    from src.config import settings

    return str(settings.ENVIRONMENT)


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def sandbox_tags(*, user_id: uuid.UUID, app_id: uuid.UUID) -> dict[str, str]:
    """The full C10 identity for a build sandbox, stamped at create.

    Every field the escalate-never-destroy rule needs is here, which is the whole point: a
    container created through this function is judgeable from ARM alone, with Redis down, by an
    operator reading the portal."""
    return checked_tags(
        {
            TAG_KIND: KIND_BUILD_SANDBOX,
            TAG_USER_ID: str(user_id),
            TAG_APP_ID: str(app_id),
            TAG_CONTROL_PLANE: control_plane_segment(),
            TAG_CREATED_AT: _now_iso(),
        }
    )


def published_app_tags(*, app_id: uuid.UUID) -> dict[str, str]:
    """Identity for a PUBLISHED app — deliberately a shorter set than `sandbox_tags`.

    Two omissions, both on purpose:

    * **No `TAG_CREATED_AT`.** `aca_publish.create_or_update` is a full `PUT` on every redeploy,
      so a timestamp here would be rewritten on each publish. An age that resets whenever the
      citizen ships is not an age; publishing a field that lies is worse than omitting it.
    * **No `TAG_USER_ID`.** Reclamation covers build sandboxes only (an origin scope boundary), and
      the deploy seam carries no user_id — threading one through purely to stamp it would add a
      query and a signature change for a field nothing reads.

    What IS here is the part that earns its place: `TAG_KIND` makes "this is a citizen's live
    application, not a sandbox" a RECORD rather than a `pub-` naming convention, which is the
    distinction any future destructive pass has to get right. And it is on the envelope rather than
    applied out of band precisely because that `PUT` would otherwise strip it."""
    return checked_tags(
        {
            TAG_KIND: KIND_PUBLISHED_APP,
            TAG_APP_ID: str(app_id),
            TAG_CONTROL_PLANE: control_plane_segment(),
        }
    )


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
    """Dev-server readiness snapshot (mirrors C1 `/dev/status.ready` — A REQUEST TO THE APP
    ROOT ACTUALLY SUCCEEDED) at handle construction; refreshed by `wait_ready` / `dev_status`.

    It used to mean "stdout marker seen AND the supervisor's own child is alive", which was
    wrong in both directions: `next dev` prints that marker once it is LISTENING, before the
    first route has compiled, so `ready` announced a blank page; and a dev server the agent
    started itself was invisible to it forever. The supervisor now answers from a served
    HTTP response and consults no child state at all — which is why `ready` True alongside
    `running` False is a NORMAL state, not a contradiction."""


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
        """Poll `GET /_sup/dev/status` until `ready` (a request to the app root actually
        succeeded, C1) or `timeout_s`. Poll cadence: start ~0.5s, exponential backoff capped
        at ~5s. On timeout raises `SandboxNotReadyError`. Returns a handle with `ready=True`.

        Returning therefore means a page HAS been served — but not that THIS route is
        compiled, and not that the app is healthy (the supervisor's probe fails open on a 500
        by design, so a compile error cannot wedge readiness forever)."""
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
        self,
        user_id: str,
        app_name: str,
        *,
        app_env: dict[str, str],
        source_key: str | None = None,
    ) -> SandboxHandle:
        """Provision a FRESH container and restore a git-bundle onto its local disk (git ops
        over `/_sup/exec`), then RE-INJECT the C9 app-data credential from `app_env`. Returns a
        handle (`ready=False` until `wait_ready` / `dev_start`). See C4 for the pull ordering.

        `source_key` names WHICH bundle to restore, defaulting to the app's saved snapshot.
        It exists so a recovery can pull the crash-recovery copy instead — the only reason that
        copy is written at all. Optional with a default rather than required, because every
        existing caller means "the saved one" and should keep reading that way."""
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
