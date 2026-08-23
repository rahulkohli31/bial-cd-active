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
import enum
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
        """No owner, no app, no age, or NOT OURS TO JUDGE ⇒ REPORT IT, NEVER DESTROY IT
        (ADR-0029 §3, tier four).

        This is the escalate-never-destroy invariant in one predicate, and it is the reason the
        backfill refuses to guess an owner from a lossy name. A container the platform cannot prove
        it owns stays here forever, which is a bill an operator can see and act on — strictly
        better than the alternative, which is deleting somebody's unsaved work on a near-miss name
        match.

        THE CONTROL-PLANE CLAUSE IS R22, AND IT BELONGS HERE RATHER THAN IN THE CLASSIFIER. A
        container stamped by a different control plane is not this one's to reason about: a dev
        deployment pointed at a resource group that also holds production-stamped sandboxes can
        read them, and must not be able to sentence them. Leaving that check to whoever consumes
        this property would make the safety of the fleet depend on every future caller remembering
        a rule the property's own name says it already enforces. Failing this clause open — an
        `ENVIRONMENT` rename, say — makes the whole fleet escalate-only and destroys nothing, which
        is the correct direction to be wrong in."""
        return (
            self.user_id is None
            or self.app_id is None
            or self.created_at is None
            or self.control_plane != control_plane_segment()
        )

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


@dataclass(frozen=True)
class FleetMember:
    """One container app as a reclamation pass sees it — the whole of what Azure is allowed to
    tell us about a sandbox (C10 §2, R3).

    THE PROJECTION IS THE SECURITY BOUNDARY, not a convenience. ARM's list endpoint returns
    `properties.template.containers[].env` in **plaintext** for every app in the group —
    `SUPERVISOR_TOKEN`, `BIAL_DATABASE_URL`, `BIAL_BLOB_SAS` — unrequested and unredacted (only
    `configuration.secrets` are masked). Every reclamation pass enumerates the whole fleet, and
    passes log, report and escalate. Narrowing to these five fields at the boundary keeps those
    values out of every caller, every log line and every operator report *by construction*, rather
    than by each downstream reader remembering. That is why this is a frozen dataclass and not the
    SDK's `ContainerApp`.

    `tags` is NORMALIZED and never `None`: ARM omits the key entirely on an untagged app, and that
    shape is precisely the orphan population.

    `arm_created_at` is Azure's `systemData.createdAt`, and it is **evidence for a human, never an
    age for the tier clock**. R2 exists to distrust it — its behaviour when a name is recreated is
    undocumented, so a container that looks nineteen days old may not be. The clock runs off the
    self-stamped `bial-created-at` tag (`identity.created_at`). This field earns its place on the
    escalation path only, where "this unowned container has existed since March" is exactly what
    the person adjudicating it wants to know and nothing is destroyed on the strength of it."""

    name: str
    tags: Mapping[str, str]
    running_status: str | None
    fqdn: str | None
    arm_created_at: dt.datetime | None

    @property
    def identity(self) -> SandboxIdentity:
        """What this container says about itself, judged without the coordination store."""
        return identity_from_tags(self.tags)


def control_plane_segment() -> str:
    """This process's environment segment — the `TAG_CONTROL_PLANE` value (R22).

    Delegated to `src.core.runtime_env`, a leaf with no module-scope imports: `src.config` reaches
    `src.settings.api`, which reaches the sandbox config, so asking it directly at module
    level here would close that cycle. The same workaround used to be spelled out here and in
    `redis/keys.py`, twice.

    Kept as its own named function rather than calling the accessor at each site: what this
    answers is which control plane is entitled to JUDGE a container, which is a different question
    from which environment's coordination keys to read, and the two are free to diverge."""
    from src.core.runtime_env import environment_segment

    return environment_segment()


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


class CompileState(enum.StrEnum):
    """What the app's dev server is doing right now, derived from its HMR socket (R17/R18).

    FOUR values, not three, and the fourth is the point. `UNKNOWN` means the platform has no
    idea: the supervisor has not connected to the socket yet, it is down between reconnects,
    the container runs an image that predates `/dev/compile` (a 404), or the transport failed.
    Every one of those must read as "no idea" and never as `CLEAN` — a caller that treats an
    absent signal as good news uncovers the preview over the exact error screen the signal
    exists to hide."""

    BUILDING = "building"
    CLEAN = "clean"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CompileReport:
    """Mirrors C1 `GET /dev/compile`.

    `errors` is the de-coloured, secret-redacted compile output, bounded in the container.
    NOTHING READS IT YET, and the docstring says so rather than describing an intention: the
    only consumer today is the preview cover, which needs `state` alone. The field is carried
    because the container is the only place the text exists in structured form and re-deriving
    it later would mean re-reading a log tail — but a reader looking for where the agent gets
    compile detail will find it on the `tsc` / dev-server-tail path, not here.

    When it does get a consumer it is AGENT INPUT, never user output — it is app-authored text.
    `reason` says WHY the state is `UNKNOWN` and is None when it is not.

    `connect_generation` counts the supervisor's successful socket
    connects, so the control plane raises the protocol-drift alarm once per connect rather
    than once per poll."""

    state: CompileState
    errors: tuple[str, ...] = ()
    reason: str | None = None
    connect_generation: int = 0

    @property
    def protocol_drifted(self) -> bool:
        """A SUCCESSFUL connect that produced no frame this client recognises — the one
        reading that means the upstream protocol moved, as opposed to the socket being down.
        Named here so no call site re-derives it from the reason string."""
        return self.state is CompileState.UNKNOWN and self.reason == _DRIFT_REASON


_DRIFT_REASON: Final = "no_recognised_frame"
"""The supervisor's word for the canary firing. Matched, not re-spelled, in one place."""


SERVED_HEAD_MAX_CHARS: Final = 2_000
"""How much of the app's own root response `what_is_it_serving` keeps.

Bounded HERE rather than at the call site because the response is produced by unreviewed,
agent-authored code in the citizen's sandbox: the read has to stop somewhere the app cannot
choose. Two thousand characters is enough to hold a Next document's `<head>` and the opening of
its body — which is what a person answering "what was it actually serving?" reads — and far too
little to be worth streaming at.

Sized against its consumer, not against HTML: this is the raw evidence stored beside U6's derived
verdict, per the 2026-08-02 learning where a derived metric produced a false accusation the raw
field disproved in one step."""


@dataclass(frozen=True)
class ServedPage:
    """What the app's public root answered, head only (R9, U6).

    The SERVING half of the health verdict. Its sibling `someone_has_to_go_first` reads headers
    and stops; this one reads a bounded prefix of the body as well, because "the app answered"
    and "what the app answered with" are different questions and only the second one can be
    stored as evidence.

    `None` from the method — never a `ServedPage` with a made-up status — is how "we could not
    ask" is said, and it is the input that makes a verdict `INDETERMINATE` rather than
    `UNHEALTHY`. An app that is in fact serving must never be called broken because our own
    request timed out."""

    status: int
    head: str


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

    async def compile_state(self, handle: SandboxHandle) -> CompileReport:
        """What the app's dev server is compiling right now — C1 `GET /dev/compile` (R17/R18).

        DELIBERATELY NOT abstract, for the same reason as `someone_has_to_go_first` below:
        `test_abstractmethod_set_equals_the_c2_contract` pins the C2 set so the contract
        cannot drift, and adding a member to that frozen set is a cross-track break. Follow
        the precedent — a new capability arrives non-abstract with a safe default — rather
        than amending `_C2_METHODS`.

        The default declines, and declining is `UNKNOWN`. A client fronting no real container
        has no dev server to ask, and the whole contract of this signal is that "no idea" is a
        first-class answer which callers must hold on rather than read as clean."""
        return CompileReport(state=CompileState.UNKNOWN, reason="no_sandbox_client")

    async def what_is_it_serving(self, handle: SandboxHandle) -> ServedPage | None:
        """The app's public root, status plus a bounded head of what it answered (U6, R9).

        DELIBERATELY NOT ABSTRACT, and for the same reason as `someone_has_to_go_first` below
        rather than for convenience: every abstract method on this class mirrors one supervisor
        endpoint, and `test_abstractmethod_set_equals_the_c2_contract` pins that set so the C2
        surface cannot drift. This is not a supervisor call — it is an ordinary GET at the app's
        own root through the same Caddy the citizen's iframe uses. That is also what makes the
        serving half of the health verdict work against containers running an image that predates
        this plan: no new endpoint, so nothing to rebuild.

        WHY NOT `someone_has_to_go_first`. That method is contractually non-load-bearing (R6) —
        it gates nothing, and its docstring says no caller may make a preview conditional on what
        it returns. The health verdict is precisely a caller making a decision on the answer, so
        reusing it would quietly convert a promise into a lie. It also stops at the headers, and
        the verdict's evidence field needs the body's opening.

        The default declines. A client fronting no real container has no root to ask, and `None`
        is the honest answer — which the verdict reads as `INDETERMINATE`, never as broken."""
        return None

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
