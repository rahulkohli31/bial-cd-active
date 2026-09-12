"""The frozen sandbox-client surface: the `SandboxClient` ABC, `SandboxHandle`, the typed
`FileOp` request union + results, and the typed exceptions — the control-plane wrapper over
the supervisor's HTTP API (`sandbox/supervisor/app.py`).

An `abc.ABC`, not a `Protocol` (mirrors `ObjectStorage`): an incomplete implementation fails
at instantiation. No vendor type crosses this port; every call goes to
`https://{handle.fqdn}/_sup/<endpoint>` with `Authorization: Bearer {handle.token}`.
TWO ADDRESSES, NOT INTERCHANGEABLE: `preview_url` is PUBLIC — the router address a browser
gets, carrying the key (per-app subdomains would need a wildcard cert BIAL refused).
`app_root_url`/`fqdn` are PRIVATE, direct to the container — BIAL's ACA has no public DNS.
The control plane always uses the private pair; `preview_url` would leave the VNet.
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
    """Base for any sandbox-client failure (a supervisor timeout/4xx, an ACA error, an
    unreachable supervisor). A non-zero `ExecResult.exit` is NOT one of these — it
    is a normal return."""


class SandboxNotReadyError(SandboxError):
    """The readiness poll timed out, or an op needs a ready dev server before it is
    ready. RETRYABLE — the caller may poll again."""


class SandboxGoneError(SandboxError):
    """The Redis registry lists the sandbox as active but the container is unreachable
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


def base_path_for(app_name: str) -> str:
    """The path a generated app is served under, e.g. `/a/sbx-<28 hex>`.

    THE KEY IS THE CONTAINER'S NAME: the router holds no registry, so an unknown key fails
    as a DNS miss, not a lookup miss.

    NO TRAILING SLASH — measured, not stylistic: Next 308-redirects `/<base>/` to `/<base>`,
    so a slashed value reads as a redirect, not the app, and is rejected as a `basePath`.
    """
    return f"/a/{app_name}"


# --- ARM identity tags -------------------------------------------------------
#
# A container must be judgeable WITHOUT REDIS. A container whose registry record is gone is
# anonymous: unreachable by the product, invisible to every automatic path, and billing at
# ~$0.108/hr forever. So identity lives on the ARM resource, written into the creation envelope so
# it exists from the first moment.
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
"""The owning user's UUID, in plaintext. A container must be judgeable without the coordination
store, which rules out an opaque reference that would need a database lookup; a UUID is an
identifier, not a secret, and the resource group is internal-only. This is a deliberately
accepted trade-off — it does surface in cost exports."""

TAG_APP_ID: Final = "bial-app-id"
"""The app UUID this container serves. Note the name is NOT a substitute: `app_name_for` keeps only
28 of the app_id's 32 hex characters, so a sandbox name is lossy and this tag is the only lossless
back-reference the resource carries."""

TAG_CONTROL_PLANE: Final = "bial-control-plane"
"""Which control plane created it — the environment segment. A dev control
plane pointed at the wrong subscription must not be able to judge a production container."""

TAG_CREATED_AT: Final = "bial-created-at"
"""OUR creation timestamp (ISO-8601), never Azure's `systemData.createdAt`.

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
"""The two-pass staging marker. RESERVED — pinned here so the code that eventually writes it
inherits the spelling instead of re-opening it; nothing writes it yet.

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
    to judge it on when Redis is gone."""

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
        """No owner, no app, no age, or NOT OURS TO JUDGE ⇒ REPORT IT, NEVER DESTROY IT.
        The escalate-never-destroy invariant in one predicate — why the backfill refuses to guess
        an owner from a lossy name. An unprovable container stays here forever (a visible bill),
        never deleting someone's unsaved work on a near-miss.
        THE CONTROL-PLANE CLAUSE LIVES HERE, NOT IN THE CLASSIFIER: a dev deployment sharing a
        resource group with production-stamped sandboxes can read them but must never sentence
        them. Failing this clause open (e.g. an `ENVIRONMENT` rename) makes the whole fleet
        escalate-only — the correct direction to be wrong in."""
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
    """One container app as a reclamation pass sees it — all Azure will tell us about a sandbox.

    THE PROJECTION IS THE SECURITY BOUNDARY: ARM's list endpoint returns `containers[].env` in
    PLAINTEXT (tokens, DB URL, blob SAS), unrequested and unredacted — only
    `configuration.secrets` is masked. These five fields keep secrets out of every caller, log
    line and operator report *by construction* — a frozen dataclass, not the SDK's `ContainerApp`.
    `tags` is normalized, never `None`. `arm_created_at` is evidence for a HUMAN only — never the
    tier clock's age, which runs off `identity.created_at`."""

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
    """This process's environment segment — the `TAG_CONTROL_PLANE` value.

    Delegated to `src.core.runtime_env` (a leaf, no module-scope imports) to avoid the import
    cycle: `src.config` → `src.settings.api` → sandbox config would close at module level here.

    Its own function, not an inline accessor call: this answers WHICH control plane may JUDGE a
    container — a different question from which environment's coordination keys to read, and the
    two are free to diverge."""
    from src.core.runtime_env import environment_segment

    return environment_segment()


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def sandbox_tags(*, user_id: uuid.UUID, app_id: uuid.UUID) -> dict[str, str]:
    """The full ARM-tag identity for a build sandbox, stamped at create.

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
    """Identity for a PUBLISHED app — deliberately shorter than `sandbox_tags`. Two omissions:
    no `TAG_CREATED_AT` (every publish is a full `PUT`, so a timestamp here would be rewritten
    each time — an age that resets on ship is not an age); no `TAG_USER_ID` (reclamation covers
    build sandboxes only, and nothing reads it on a published app).
    `TAG_KIND` IS here: it makes "citizen's live app, not a sandbox" a RECORD rather than a
    `pub-` naming convention — the distinction any future destructive pass must get right, and
    it must ride the envelope because the `PUT` would otherwise strip anything applied out of
    band."""
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
    passed back into every operation.

    `app_root_url` below is a DERIVED property, not a sixth field — the handle's shape is
    unchanged and every `dataclasses.replace` call site keeps working."""

    fqdn: str
    """The container's ACA ingress FQDN, host only, NO scheme (e.g. `app-xyz.westeurope.
    azurecontainerapps.io`). `/_sup/*` and `app_root_url` derive from it — `preview_url` does
    NOT. On an internal environment this name
    has no public DNS at all, so it is a private address despite ACA calling it public."""
    token: str
    """The per-session supervisor bearer token, sent as `Authorization: Bearer
    {token}` to `/_sup/*`. Held IN-PROCESS only; NEVER persisted raw — the Redis
    registry stores a `token_ref` (a reference), not this value."""
    app_name: str
    """The app/container identifier (one-app-per-project); == the registry's `app_name`."""
    preview_url: str
    """THE PUBLIC ADDRESS — `https://<apps-host>/a/<app_name>/`, the browsable preview the portal
    frames cross-origin. Never carries the bearer token.

    Use `app_root_url` for anything the CONTROL PLANE does — a probe pointed here would leave
    the VNet and traverse the public gateway."""
    ready: bool
    """Dev-server readiness snapshot (mirrors the supervisor's `/dev/status.ready` — A
    REQUEST TO THE APP ROOT ACTUALLY SUCCEEDED) at handle construction; refreshed by
    `wait_ready` / `dev_status`.

    The supervisor answers from a served HTTP response alone and consults no child-process
    state — so `ready` True alongside `running` False is a NORMAL state, not a contradiction.
    A stdout-marker-plus-child-alive check would get this wrong in both directions: the marker
    fires once `next dev` is LISTENING, before the first route has compiled, and a dev server
    the agent started itself would be invisible to a child-state check forever."""

    @property
    def app_root_url(self) -> str:
        """Where THIS CONTAINER serves the app's own pages, on the direct private address.

        Not `preview_url`. Once an app runs under a base path, its root belongs to no route and
        answers 404 — so a control-plane probe that keeps asking for `/` reads the framework's
        own not-found page and reports it as what the app is serving. Self-heal then converts
        that into "make sure `app/page.tsx` exists" and the model burns metered tokens repairing
        a file that was never wrong.
        """
        return f"https://{self.fqdn}{base_path_for(self.app_name)}"


@dataclass(frozen=True)
class ExecResult:
    """Mirrors the supervisor's `POST /exec`. A non-zero `exit` is a NORMAL return, never an
    exception — self-heal reads `exit`/`stderr` off a 200."""

    stdout: str
    stderr: str
    exit: int


@dataclass(frozen=True)
class DevStatus:
    """Mirrors the supervisor's `GET /dev/status`. `port` is always 3000 (the `next dev` port).
    `exit_code` is the dead child's post-mortem (None while alive, never started, or when
    talking to a pre-exit_code supervisor image) — 137 is the OOM-killer's signature."""

    running: bool
    ready: bool
    port: int
    exit_code: int | None = None
    #: THE STATUS THE APP ROOT ANSWERED WITH, or `None` when nothing answered — and also `None`
    #: from a supervisor image that predates the field, which is why it is optional and defaulted
    #: rather than required.
    #:
    #: `ready` AND THIS ARE NOT THE SAME QUESTION. `ready` is fail-open by the supervisor's own
    #: design: ANY response counts, 4xx and 5xx included, so that a compile error cannot wedge it
    #: False and mislead the model. For the citizen's PREVIEW that is too generous — a dev server
    #: answering 404 because the agent has not written `app/page.tsx` yet is "ready" and has
    #: nothing to show, and framing it is how a blank document ends up under a live-preview label.
    #: Anything that decides whether to FRAME must read this; anything asking "is the dev server
    #: alive at all" should go on reading `ready`.
    root_status: int | None = None

    @property
    def shows_a_page(self) -> bool:
        """Would a citizen opening this preview right now see a PAGE?

        THE QUESTION THE FRAME MUST ASK, and it is not `ready`. `ready` is the supervisor's
        fail-open "something answered on the dev port", which counts a 404 and a 500 on purpose so
        that a compile error cannot wedge it False and mislead the model. A build spends the
        seconds between the dev server binding and the agent writing `app/page.tsx` answering 404s
        — genuinely ready, with nothing to show — and framing that window is how a blank document
        ends up on screen under a live-preview label. Measured on 2026-09-10: the app root was
        still 404ing while the agent said "Now the app pages", and the pane framed it.

        `None` READS AS TODAY'S BEHAVIOUR, deliberately. A supervisor image built before
        `root_status` existed cannot answer this, and treating "cannot say" as "no page" would
        refuse to frame every container in the existing fleet — a false negative at fleet scale,
        which is worse than the window it would close. It self-expires: containers turn over, and
        from the next sandbox image onward the field is always present.

        A 3xx COUNTS. A redirect off the root is the app choosing where its first page lives, and
        the browser will follow it; only 4xx and 5xx mean the citizen gets nothing.
        """
        if not self.ready:
            return False
        return self.root_status is None or self.root_status < 400


@dataclass(frozen=True)
class DevLogs:
    """Mirrors the supervisor's `GET /dev/logs`. `next_cursor` is its wire field `next`
    (renamed only to avoid shadowing the builtin); pass it back as `since` for only-new lines."""

    lines: list[str]
    next_cursor: int


class CompileState(enum.StrEnum):
    """What the app's dev server is doing right now, derived from its HMR socket.

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
    """Mirrors the supervisor's `GET /dev/compile`.

    `errors` is de-coloured, secret-redacted, bounded — NOTHING READS IT YET (only `state` is
    consumed today; kept because re-deriving it later means re-reading a log tail). When it
    does get a consumer it is AGENT INPUT, never user output — it is app-authored text.
    `reason` says WHY `state` is `UNKNOWN`, else `None`. `connect_generation` counts the
    supervisor's successful socket connects, so the drift alarm fires once per connect, not
    once per poll."""

    state: CompileState
    errors: tuple[str, ...] = ()
    reason: str | None = None
    connect_generation: int = 0

    @property
    def protocol_drifted(self) -> bool:
        """A SUCCESSFUL connect that produced no frame this client recognises — the one
        reading that means the upstream protocol moved, as opposed to the socket being down.
        Named here so no call site re-derives it from the reason string.

        THE SUPERVISOR IS THE ONE THAT DECIDES THIS, and it ships baked into the sandbox image on
        a different clock from this process — so read a firing against the image tag before
        reading it as a rename. A supervisor baked before 2026-09-10 counted the Turbopack
        handshake frames (`turbopack-connected`, `isrManifest`) as unreadable traffic and raised
        this once per container, always at `connect_generation == 1`, against a dev server that
        was merely slow to reach its first `sync`. That is a FALSE positive with a known shape, not
        a reason to soften the check: nothing here is loosened to hide it, because a platform that
        stops saying it cannot read the signal, while it still cannot read it, is worse than the
        noise. Re-bake the image; the alarm then means what it says again."""
        return self.state is CompileState.UNKNOWN and self.reason == _DRIFT_REASON

    @property
    def config_tampered(self) -> bool:
        """The app is not serving under the path the platform assigned it.

        Almost always: the model edited or deleted the platform-owned `next.config.ts` and the
        app went back to answering at `/`. This exists so the failure NAMES ITSELF. Without it
        the only observable symptom is the app's own root answering 404, which the serving
        verdict reports as "the root route does not resolve — make sure `app/page.tsx` exists" —
        sending the model to repair a file that was never wrong, three retries deep.
        """
        return self.state is CompileState.UNKNOWN and self.reason == _TAMPERED_REASON


_DRIFT_REASON: Final = "no_recognised_frame"
"""The supervisor's word for the canary firing. Matched, not re-spelled, in one place."""

_TAMPERED_REASON: Final = "config_tampered"
"""The supervisor's word for "the served base path is not the injected one". Same rule as
`_DRIFT_REASON`: matched here, never re-spelled at a call site."""


SERVED_HEAD_MAX_CHARS: Final = 2_000
"""How much of the app's own root response `what_is_it_serving` keeps.

Bounded HERE rather than at the call site because the response is produced by unreviewed,
agent-authored code in the citizen's sandbox: the read has to stop somewhere the app cannot
choose. Two thousand characters is enough to hold a Next document's `<head>` and the opening of
its body — which is what a person answering "what was it actually serving?" reads — and far too
little to be worth streaming at.

Sized against its consumer, not against HTML: this is the raw evidence stored beside the
derived health verdict."""


@dataclass(frozen=True)
class ServedPage:
    """What the app's OWN root answered, head only — `/a/<app-name>`, not the container root
    (which belongs to no route once a base path is set, so a naive `/` probe would read the
    framework's 404; see `SandboxHandle.app_root_url`).
    The SERVING half of the health verdict; unlike `someone_has_to_go_first` (headers only)
    this also reads a bounded body prefix, since "it answered" and "what it answered" differ.
    `None` from the method (never a made-up status) means "we could not ask" — the verdict
    reads that as `INDETERMINATE`, never `UNHEALTHY`: a serving app must never read as broken
    because our own request timed out."""

    status: int
    head: str


@dataclass(frozen=True)
class FileResult:
    """Mirrors the supervisor's `POST /files` per-action response. `detail` carries the
    action-specific body (`content` for view, `replacements` for str_replace, …)."""

    ok: bool
    detail: dict[str, object]


# --- FileOp: the typed /files request (discriminated on `action`) ------------
#
# A discriminated union over view | str_replace | create | insert carrying the
# supervisor's fields. Each variant declares only its own required fields (fail-first — a
# str_replace with no `old_str` cannot be constructed), unlike the supervisor's flat
# all-optional `FilesBody`; the client serializes the variant back to that wire shape.


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


class FileCreateBytes(_FileOpBase):
    """Place a REAL FILE in the workspace — the only op here that is not text.

    Its sibling `FileCreate` writes through `write_text` and rewrites every CRLF to LF, which is
    right for source and silently corrupts a binary: an Office file is a ZIP archive and carries
    that byte pair constantly. A separate op rather than a flag, so the no-normalisation rule
    belongs to the op a caller chose rather than to a branch they might miss.

    `file_b64` is base64 of the real bytes. The supervisor holds NO size ceiling of its own —
    the decoded length is bounded once, at the upload door, and a second number here would be a
    limit nobody could see from the place that produces the bytes.
    """

    action: Literal["create_bytes"] = "create_bytes"
    file_b64: str


FileOp = Annotated[
    FileView | FileStrReplace | FileCreate | FileInsert | FileCreateBytes,
    Field(discriminator="action"),
]
"""The typed `/files` request. `files()` takes one `FileOp` and returns one
`FileResult`; validation discriminates on `action`."""


# --- the frozen client ABC ---------------------------------------------------


class SandboxClient(abc.ABC):
    """The complete async sandbox-client surface. Documented poll/timeout/retry
    defaults are the frozen semantics a mock must honor where observable.

    The one-per-user + rehydrate rule is a caller invariant: the caller must resolve it
    against the Redis registry + lock before any work — a returning user ATTACHES or
    RESTORES — re-provisioning a user who already holds a live sandbox is a contract
    violation (double allocation)."""

    @abc.abstractmethod
    async def provision_new(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        """Provision a BRAND-NEW container for `user_id`. `app_env` is the app's injected
        environment, every name in it chosen to survive the supervisor's child-env scrub
        allowlist; what belongs in it is `manager._resolve_sandbox`'s to decide.

        Returns a handle with `ready=False`. The caller MUST already hold the Redis
        one-per-user lock. Transient provisioning errors retried with capped
        exponential backoff."""
        ...

    @abc.abstractmethod
    async def wait_ready(
        self, handle: SandboxHandle, *, timeout_s: float = 120.0
    ) -> SandboxHandle:
        """Poll `GET /_sup/dev/status` until `ready` (a request to the app root actually
        succeeded) or `timeout_s`. Poll cadence: start ~0.5s, exponential backoff capped
        at ~5s. On timeout raises `SandboxNotReadyError`. Returns a handle with `ready=True`.

        Returning therefore means a page HAS been served — but not that THIS route is
        compiled, and not that the app is healthy (the supervisor's probe fails open on a 500
        by design, so a compile error cannot wedge readiness forever)."""
        ...

    @abc.abstractmethod
    async def attach_existing(self, user_id: str) -> SandboxHandle:
        """IDEMPOTENT reconnect to the user's already-running sandbox (FQDN +
        `token_ref`→token from the Redis registry) WITHOUT re-provisioning. If the
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
        over `/_sup/exec`), then RE-INJECT the app-data credential from `app_env`. Returns a
        handle (`ready=False` until `wait_ready` / `dev_start`).

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
        exception; a supervisor timeout (504) surfaces as `SandboxError`."""
        ...

    @abc.abstractmethod
    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        """One `FileOp` → one `FileResult` via `POST /_sup/files`. The supervisor's 422
        (str_replace 0/N matches) and 400 (missing sub-field / escape / unknown
        action) surface as `SandboxError`."""
        ...

    @abc.abstractmethod
    async def dev_start(
        self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
    ) -> int:
        """Start `next dev` (default `["npm", "run", "dev"]`) via `POST /_sup/dev/start`;
        returns the pid. IDEMPOTENT: the supervisor's 409 "already running" is treated as success
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
        call when already gone (no-op) — required by the snapshot-then-teardown
        and reaper ordering."""
        ...

    # --- outside the frozen set: a courtesy, not a contract ------------------

    async def compile_state(self, handle: SandboxHandle) -> CompileReport:
        """What the app's dev server is compiling — the supervisor's `GET /dev/compile`.

        DELIBERATELY NOT abstract, same reason as `someone_has_to_go_first` below:
        `test_abstractmethod_set_equals_the_pinned_contract` pins the abstract set, so a new
        capability arrives non-abstract with a safe default rather than amending it.
        The default declines to `UNKNOWN`. A client fronting no real container has no dev server
        to ask, and "no idea" is a first-class answer callers must hold on to, not read as
        clean."""
        return CompileReport(state=CompileState.UNKNOWN, reason="no_sandbox_client")

    async def what_is_it_serving(self, handle: SandboxHandle) -> ServedPage | None:
        """The app's public root, status plus a bounded head of what it answered.

        DELIBERATELY NOT ABSTRACT, same reason as `someone_has_to_go_first` below.
        NOT `someone_has_to_go_first`: that method is non-load-bearing and stops at headers; a
        health verdict IS a gating decision and needs the body too.
        Default: `None` (no root to ask) — read as `INDETERMINATE`, never broken."""
        return None

    async def someone_has_to_go_first(self, handle: SandboxHandle) -> int | None:
        """Pay the app's first route compile so the citizen's browser does not.

        DELIBERATELY NOT abstract: every method above mirrors one supervisor endpoint, and the
        pinned-contract test keeps that set frozen. This is an ordinary GET at the app's public
        root (same Caddy the iframe uses) — adding it would claim an endpoint the supervisor
        never grew.
        Default: `None` (no container to compile for). NON-LOAD-BEARING BY CONSTRUCTION: an
        override must never raise, and no caller may gate a preview frame on its return."""
        return None
