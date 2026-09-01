"""U24 — the at-limit experience: secure the work, then say so in the citizen's own words.

WHAT THIS REPLACES, and why the replacement needs its own file. The old refusal read "You have
used today's token budget. Your changes are still in the workspace — click Save to keep them",
which did three things wrong at once. It asserted that the work was still there, having checked
nothing. It made the citizen responsible for making it durable, in the middle of a paragraph they
had every reason to skim. And it left the actual durability to the turn's exit-path autosave, which
is deliberately best-effort and deliberately swallowed — so on the day it failed, the one sentence
that should have been alarming was the same boilerplate as every other day.

The tests here pin the ORDER (the copy is taken and confirmed before the citizen is told and before
the turn's `finally` hands the container to the reclamation path), the HONESTY (the reassurance is
said only when a copy actually landed), and the REGISTER (no file path, command, library or
framework term reaches the reader). The last of those is checked over the RENDERED sentence rather
than the template, because the configured support address is substituted in at render time and is
the one part of this message that comes from outside the copy module.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass

import pytest

from src.config import settings
from src.db.models.user_limit import UserLimit
from src.services.build_sessions import snapshot as snapshot_module
from src.services.build_sessions.alarms import RECOVERY_WRITE_DID_NOT_LAND_EVENT
from src.services.sandbox import SandboxClient, SandboxError
from src.services.sandbox.base import ExecResult, SandboxHandle
from src.services.storage import recovery_key
from src.services.turns import copy as copy_module
from src.services.turns.copy import (
    AT_LIMIT_TEXT,
    COULD_NOT_KEEP_A_COPY,
    KEPT_A_COPY,
    SPENT_ENOUGH_TEXT,
)
from src.services.usage import gate as gate_module
from src.services.usage.gate import (
    DailyTokenLimitExceededError,
    at_limit_ending,
    record_usage,
)
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle

APP = uuid.UUID("0198f2c0-2424-7000-8000-0000000a71b1")
ON_RECORD = "a" * 40
THIS_TURN = "b" * 40
UNRELATED = "c" * 40

# Long enough that a slow suite cannot expire a session mid-request.
_TTL_SECONDS = 300

_HANDLE = SandboxHandle(
    fqdn="app-x.centralindia.azurecontainerapps.io",
    token="t",
    app_name="app-x",
    preview_url="https://app-x.centralindia.azurecontainerapps.io/",
    ready=True,
)


@dataclass
class _Workspace:
    """A stand-in for the orchestrator's `SandboxSession`, satisfying `SecurableWorkspace`.

    Structural, exactly as the production seam is: if `SandboxSession` ever renames one of these
    three attributes, the type gates catch it at the engine's call site rather than here.

    `sandbox_client` is annotated as the ABC rather than as the fake, and that is not pedantry: a
    protocol member declared as a mutable attribute is checked INVARIANTLY, so narrowing it here
    makes this class stop satisfying `SecurableWorkspace` — the test would then be exercising a
    shape production could never hand in."""

    sandbox_client: SandboxClient
    handle: SandboxHandle
    app_id: uuid.UUID


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    monkeypatch.setattr(snapshot_module, "get_storage", lambda: fake)
    return fake


@pytest.fixture
def alarms(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    """Every structlog error the gate raises during a test, in order.

    Captured on the GATE's logger rather than the snapshot module's, because the `failed` arm is
    raised from the call site — it is the only place that knows the write threw."""
    raised: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(gate_module._log, "error", lambda event, **kw: raised.append((event, kw)))
    return raised


def _container(*, bundles: str, head: str, ancestry: str = "0 0") -> FakeSandboxClient:
    """A container that commits, bundles `bundles`, and answers the ancestry probe.

    `bundles` and `head` are separate because the commit step runs FIRST inside the write: a dirty
    tree becomes a new commit before anything is bundled, so what lands is not necessarily where
    HEAD was when the turn started."""
    client = FakeSandboxClient()
    payload = base64.b64encode(a_git_bundle(bundles)).decode()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            answered = ancestry if "merge-base" in cmd[-1] else ""
            return ExecResult(stdout=f"{head}@@@@4@@{answered}", stderr="", exit=0)
        if cmd[0] == "base64":
            return ExecResult(stdout=payload, stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    return client


async def _seed_recovery(store: FakeStorage, sha: str = ON_RECORD) -> None:
    await store.put(recovery_key(APP), a_git_bundle(sha), metadata={"head_sha": sha})


async def _head_in_recovery_slot(store: FakeStorage) -> str | None:
    meta = await store.head(recovery_key(APP))
    return (meta.metadata or {}).get("head_sha") if meta else None


# =============================================================================
# The sentence
# =============================================================================


async def test_the_at_limit_sentence_says_what_happened_when_it_comes_back_and_who_to_ask(
    store: FakeStorage,
) -> None:
    """★ AE18. The three facts a citizen needs, and the platform used to supply none of them
    properly: what happened, when they can carry on, and a real address to ask for more.

    "Contact your administrator" was the previous answer to the third, and it names a ROLE. A
    citizen has no way to turn a role into an address, so the sentence ended in a dead end at
    exactly the moment they most needed a way out of it.

    Deleting this test would let the message lose any one of the three and still ship, because
    every other test here is about the securing rather than the words.

    Mutation check: drop `{contact}` from `AT_LIMIT_TEXT` and this goes red."""
    await _seed_recovery(store)
    workspace = _Workspace(_container(bundles=THIS_TURN, head=ON_RECORD), _HANDLE, APP)

    ending = await at_limit_ending(workspace)

    assert "budget" in ending.message, "what happened, in the reader's own vocabulary"
    assert "midnight" in ending.message, "when they can carry on"
    assert settings.SUPPORT_CONTACT_EMAIL in ending.message, "a real address, not a role"
    # The address is the CONFIGURED one, never a constant baked into the copy — that is the whole
    # reason the setting exists, and a hardcoded fallback would satisfy the assertion above.
    assert settings.SUPPORT_CONTACT_EMAIL not in AT_LIMIT_TEXT


async def test_the_message_carries_no_file_path_command_library_or_framework_term(
    store: FakeStorage,
) -> None:
    """R31's testable half, applied to the RENDERED sentence rather than to the template.

    `test_no_sentence_this_plan_shows_a_citizen_carries_developer_jargon` already sweeps the copy
    module, but it can only see the template — and the one substitution this message makes comes
    from deployment configuration, which nobody reviews as prose. A support address configured as
    `ops@…/srv/logs` would sail past that guard and land in front of a citizen.

    Mutation check: put any of the terms below into `AT_LIMIT_TEXT` and this goes red."""
    await _seed_recovery(store)
    workspace = _Workspace(_container(bundles=THIS_TURN, head=ON_RECORD), _HANDLE, APP)

    both_halves = [
        (await at_limit_ending(workspace)).message,
        AT_LIMIT_TEXT.format(kept=COULD_NOT_KEEP_A_COPY, contact=settings.SUPPORT_CONTACT_EMAIL),
    ]

    forbidden = (
        ".tsx",
        ".ts",
        ".json",
        "app/",
        "src/",
        "npm",
        "npx",
        "git ",
        "tsc",
        "Next.js",
        "React",
        "typescript",
        "TypeScript",
        "localhost",
        "http://",
        "https://",
        "container",
        "bundle",
        "snapshot",
        "token",
        "quota",
        "stack trace",
        "console",
        "compile",
    )
    for sentence in both_halves:
        for term in forbidden:
            assert term not in sentence, f"{term!r} reached a citizen in {sentence!r}"


def test_the_two_halves_of_the_promise_are_separate_constants() -> None:
    """The reassurance is conditional, so it cannot live inside the sentence that always renders.

    A single string carrying "your app is safe" would make the platform assert it on the one path
    where it might not be true, and a citizen acts on that reassurance by closing the tab.

    Mutation check: fold `KEPT_A_COPY` into `AT_LIMIT_TEXT` and this goes red."""
    assert "{kept}" in AT_LIMIT_TEXT
    # Both halves are exposed as module-level SENTENCES, which is what puts them inside the copy
    # module's own jargon sweep — that guard iterates `vars(copy)` for strings with a space in
    # them, so a half inlined at its call site would be outside it by construction.
    sentences = {
        name: value
        for name, value in vars(copy_module).items()
        if not name.startswith("_") and isinstance(value, str) and " " in value
    }
    assert sentences["KEPT_A_COPY"] == KEPT_A_COPY
    assert sentences["COULD_NOT_KEEP_A_COPY"] == COULD_NOT_KEEP_A_COPY


# =============================================================================
# The securing, which happens BEFORE the citizen is told
# =============================================================================


async def test_the_work_is_stored_before_the_citizen_is_told_they_are_at_the_limit(
    store: FakeStorage, alarms: list[tuple[str, dict[str, object]]]
) -> None:
    """★ THE POINT OF THE UNIT. The recovery slot holds THIS turn's tree by the time the message
    exists — not by the time the exit path gets round to its best-effort autosave, and not by the
    time the citizen notices the word "Save".

    Ordering matters because of what comes next in the turn: the `finally` pardons the container
    and frees the slot, after which the reclamation path may take it. Anything not stored by then
    is stored on a machine somebody else is entitled to reclaim.

    Mutation check: make `at_limit_ending` return the sentence without calling
    `write_recovery_copy` and this goes red — the slot still holds the older tree."""
    await _seed_recovery(store)
    workspace = _Workspace(_container(bundles=THIS_TURN, head=ON_RECORD), _HANDLE, APP)

    ending = await at_limit_ending(workspace)

    assert await _head_in_recovery_slot(store) == THIS_TURN
    assert ending.work_is_secured is True
    assert KEPT_A_COPY in ending.message
    assert alarms == [], "a copy that landed must not alarm"


async def test_a_recovery_write_that_fails_still_tells_the_citizen_and_alarms_it(
    store: FakeStorage, alarms: list[tuple[str, dict[str, object]]]
) -> None:
    """★ The write can fail, and neither of the two easy answers is acceptable.

    Raising would turn "you have used your budget" into a crash, for a citizen who did nothing
    wrong. Swallowing is what the old exit path did, and it is precisely why nobody could say
    afterwards whether 2026-08-18 was a failure to CHECK the workspace or a failure to make it
    DURABLE — a write that never landed left no trace an operator would ever look for.

    So: the citizen is told, the sentence stops claiming their work is safe, and the failure gets
    the pinned event with `reason="failed"` — the arm that can only be raised from a call site,
    because only the call site knows the write threw.

    Mutation check: swap the `except Exception` arm for a bare `raise` (or delete the `_log.error`)
    and this goes red."""
    await _seed_recovery(store)
    client = FakeSandboxClient()

    def wedged(cmd: list[str]) -> ExecResult:
        raise SandboxError("the container stopped answering")

    client.exec_handler = wedged
    workspace = _Workspace(client, _HANDLE, APP)

    ending = await at_limit_ending(workspace)

    assert ending.work_is_secured is False
    assert COULD_NOT_KEEP_A_COPY in ending.message
    assert "budget" in ending.message, "the citizen is still told what happened"
    assert [event for event, _ in alarms] == [RECOVERY_WRITE_DID_NOT_LAND_EVENT]
    assert alarms[0][1]["reason"] == "failed"
    assert alarms[0][1]["app_id"] == str(APP)
    # Untouched: a failed write must never be the thing that destroys the copy on record.
    assert await _head_in_recovery_slot(store) == ON_RECORD


async def test_a_refused_promotion_never_claims_the_work_is_safe(
    store: FakeStorage, alarms: list[tuple[str, dict[str, object]]]
) -> None:
    """A tree with no ancestry to the copy on record is DIVERTED by U3's guard, not promoted.

    The bytes survive under the divert prefix, but what a restore would hand this citizen is still
    the older tree — so "nothing you did today is lost" would be false in the most damaging way
    available: reassuring, and specifically about the work that is not there.

    The alarm is NOT re-raised here. `write_recovery_copy` already fired it on the way past with
    the two heads that explain the refusal attached, and a second copy of the one event an
    operator counts would double every diverted turn.

    Mutation check: treat `DIVERTED` as secured and this goes red."""
    await _seed_recovery(store)
    workspace = _Workspace(
        _container(bundles=UNRELATED, head=UNRELATED, ancestry="0 1"), _HANDLE, APP
    )

    ending = await at_limit_ending(workspace)

    assert ending.work_is_secured is False
    assert COULD_NOT_KEEP_A_COPY in ending.message
    assert await _head_in_recovery_slot(store) == ON_RECORD
    assert alarms == [], "the guard already alarmed; the call site must not double-count it"


async def test_a_turn_that_never_took_a_container_is_told_the_same_thing_without_alarming(
    store: FakeStorage, alarms: list[tuple[str, dict[str, object]]]
) -> None:
    """An Ask or Plan turn can reach the cap too, and it has nothing to secure.

    Nothing went wrong here, which is why `COULD_NOT_KEEP_A_COPY` is worded as a request to save
    rather than as an announcement of a fault — and why this path must not raise the operator
    alarm, whose whole value is that it only fires when a write genuinely did not land.

    Mutation check: raise the alarm on the `workspace is None` branch and this goes red."""
    ending = await at_limit_ending(None)

    assert ending.work_is_secured is False
    assert "budget" in ending.message
    assert alarms == [], "nothing was attempted, so nothing failed"


# =============================================================================
# The second send, and the route-level refusal it meets
# =============================================================================


async def test_a_second_send_at_the_limit_is_refused_before_any_turn_exists(
    client, db_session
) -> None:
    """★ The refusal is written ONCE. A citizen who sends again — and they do, because the first
    message is easy to read as a transient hiccup — must not stack a second identical paragraph
    into their transcript.

    What guarantees it is that the two refusals come from different places. The FIRST is an
    in-turn ending, produced by `at_limit_ending` at a model step. Every subsequent send never
    gets that far: the route's own `enforce_daily_limit` answers 429 before a turn is created, so
    there is no turn to append anything.

    Asserted on the transcript rather than on the status code alone, because "the route refused"
    and "nothing was written" are different claims and only the second one is the promise.

    Mutation check: move the route's `enforce_daily_limit` to after the turn is persisted and this
    goes red."""
    import sqlalchemy as sa

    from src.db.models.conversation import ChatKind
    from src.db.models.message import Message
    from src.services.auth.csrf import issue_csrf_token
    from src.services.auth.session_jwt import mint_session_jwt

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=10))
    await db_session.flush()
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=0)

    jwt = mint_session_jwt(user.id, user.token_version, _TTL_SECONDS)
    csrf = issue_csrf_token(user.id, user.token_version)
    headers = {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}

    resp = await client.post(
        f"/v1/conversations/{conv.id}/turns",
        headers=headers,
        json={"message": {"text": "please carry on", "attachmentTexts": [], "attachmentIds": []}},
    )

    assert resp.status_code == 429
    rows = await db_session.scalars(sa.select(Message).where(Message.conversation_id == conv.id))
    assert list(rows) == [], "a refused send appends nothing at all"


async def test_the_route_level_refusal_body_stays_byte_stable(client, db_session) -> None:
    """The SPA's interceptor reads all five keys (`useClaudeAPI.js`), and this unit touches the
    module that renders them. Flattening the body into the plain error envelope — or renaming the
    code — breaks the client's handling of the one response it most needs to recognise.

    Mutation check: drop `remaining` from `DailyTokenLimitExceededError.as_response` and this goes
    red."""
    from src.db.models.conversation import ChatKind
    from src.services.auth.csrf import issue_csrf_token
    from src.services.auth.session_jwt import mint_session_jwt

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=10))
    await db_session.flush()
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=0)

    jwt = mint_session_jwt(user.id, user.token_version, _TTL_SECONDS)
    csrf = issue_csrf_token(user.id, user.token_version)
    resp = await client.post(
        f"/v1/conversations/{conv.id}/turns",
        headers={"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf},
        json={"message": {"text": "again", "attachmentTexts": [], "attachmentIds": []}},
    )

    assert resp.status_code == 429
    body = resp.json()
    assert set(body["error"]) == {"message", "code", "limit", "used", "remaining"}
    assert body["error"]["code"] == "daily_token_limit_exceeded"
    assert body["error"]["limit"] == 10
    assert body["error"]["used"] == 10
    assert body["error"]["remaining"] == 0


def test_the_in_turn_refusal_and_the_route_refusal_are_different_objects() -> None:
    """The route answers with a machine-readable 429 the SPA parses; the in-turn ending answers
    with a sentence a person reads. Collapsing them would put the 429's "contact your
    administrator to enable a higher plan" in front of a citizen mid-build, which is the register
    this whole unit exists to remove."""
    body = DailyTokenLimitExceededError(limit=10, used=11).as_response().body
    assert b"daily_token_limit_exceeded" in body
    assert "administrator" not in AT_LIMIT_TEXT


# =============================================================================
# The fail-first guard on the support contact
# =============================================================================


def _api_env(*, without: str | None = None) -> dict[str, str]:
    """A complete, minimal API environment, optionally with exactly one variable removed."""
    env: dict[str, str] = {
        "ENVIRONMENT": "development",
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/citizen_one_test",
        "AUTH__TENANT_ID": "11111111-1111-1111-1111-111111111111",
        "AUTH__CLIENT_ID": "22222222-2222-2222-2222-222222222222",
        "AUTH__SESSION_SECRET": "unit-test-session-secret-0123456789abcdef",
        "AUTH__REDIRECT_URI": "http://localhost:8000/api/v1/auth/callback",
        "SUPERADMIN_EMAILS": "admin@bial.com",
        "SUPPORT_CONTACT_EMAIL": "help@bial.com",
        # Required of every role with no default — a profile built without it would fail for a
        # reason that has nothing to do with the support contact this file is about.
        "APPS_BASE_URL": "https://citizenapps.bialairport.com",
    }
    if without is not None:
        del env[without]
    return env


def _boot(env: dict[str, str]):
    """Construct `ApiSettings` from EXACTLY `env`, with the env file disabled.

    Scrubbing the real environment is not ceremony: without it the developer's own exported
    variables (and `.env.test` behind them) quietly supply whatever the block omits, and a
    "refuses without X" assertion passes for the wrong reason — which is the failure mode this
    guard is supposed to catch in production configuration."""
    import os

    from src.settings import ApiSettings

    saved = dict(os.environ)
    os.environ.clear()
    os.environ["PATH"] = saved.get("PATH", "")
    os.environ.update(env)
    try:
        return ApiSettings(  # ty: ignore[missing-argument]
            _env_file=None  # ty: ignore[unknown-argument]  # pyright: ignore[reportCallIssue]
        )
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_the_api_refuses_to_start_without_a_support_contact() -> None:
    """★ The fail-first guard, and the deployment consequence is the intended one: this must be
    set in the App Service configuration before the release ships or the API does not boot.

    That is the cheaper failure by a wide margin. The alternative is a default, which can only be
    a placeholder address — and a placeholder address sends a citizen who is already stuck to a
    mailbox nobody reads. That failure surfaces as silence, weeks later, from the person least
    able to escalate it.

    Mutation check: give `SUPPORT_CONTACT_EMAIL` any default and this goes red."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="SUPPORT_CONTACT_EMAIL"):
        _boot(_api_env(without="SUPPORT_CONTACT_EMAIL"))

    # The control: the same block WITH the variable boots, so the failure above is about this
    # field and not about something else the scrubbed environment is missing.
    assert _boot(_api_env()).SUPPORT_CONTACT_EMAIL == "help@bial.com"


@pytest.mark.parametrize("value", ["", "   ", "not-an-address", "@bial.com", "help@"])
def test_a_support_contact_nobody_could_write_to_fails_the_same_way_a_missing_one_does(
    value: str,
) -> None:
    """A no-default field only guarantees that SOMETHING was supplied, and what an operator
    supplies under time pressure is `SUPPORT_CONTACT_EMAIL=` — present, empty, and accepted by a
    bare `str`. The point of the setting is that the sentence ends in a working address, so an
    empty or address-shaped-in-name-only value is the same misconfiguration.

    Mutation check: delete `_reject_a_support_address_nobody_could_write_to` and this goes red for
    every case."""
    from pydantic import ValidationError

    env = _api_env()
    env["SUPPORT_CONTACT_EMAIL"] = value
    with pytest.raises(ValidationError, match="SUPPORT_CONTACT_EMAIL"):
        _boot(env)


def test_a_configured_address_is_stripped_rather_than_trusted_verbatim() -> None:
    """A trailing space in an App Service configuration value is invisible in the portal UI and
    survives into the rendered sentence. Stripping is the difference between a clean address and
    one that a click-to-mail turns into a bounce."""
    env = _api_env()
    env["SUPPORT_CONTACT_EMAIL"] = "  help@bial.com  "
    assert _boot(env).SUPPORT_CONTACT_EMAIL == "help@bial.com"


# =============================================================================
# U13 / R91 — the same securing path, carrying a second sentence
# =============================================================================
#
# The per-run spend bound has to end a turn exactly the way the daily budget does: copy taken
# here, on the way out of the model loop, confirmed before the turn's `finally` pardons the
# container. Writing a second function to do that would put a second snapshot→teardown ordering
# on the one path in this codebase where getting the ordering wrong loses a citizen's tree — so
# `at_limit_ending` takes the sentence, and everything above it stays as it was.


async def test_the_daily_budget_endings_bytes_are_unchanged_by_the_parameterisation(
    store: FakeStorage,
) -> None:
    """★★ THE REGRESSION THAT PROTECTS THE INCIDENT PATH.

    The daily-budget caller passes no sentence and must come out BYTE-IDENTICAL — not "still
    contains the right words", byte-identical — so that adding a parameter to this function is
    provably a no-op for the path that already shipped.

    Mutation check: make the `sentence is None` arm fall through to any other template and this
    goes red on an exact comparison, which no later edit can quietly weaken into a substring."""
    await _seed_recovery(store)
    workspace = _Workspace(_container(bundles=THIS_TURN, head=ON_RECORD), _HANDLE, APP)

    ending = await at_limit_ending(workspace)

    assert ending.message == AT_LIMIT_TEXT.format(
        kept=KEPT_A_COPY, contact=settings.SUPPORT_CONTACT_EMAIL
    )


async def test_the_spend_bound_secures_the_tree_before_its_sentence_exists(
    store: FakeStorage, alarms: list[tuple[str, dict[str, object]]]
) -> None:
    """★ THE ORDERING, asserted for the NEW caller rather than inherited from the old one.

    The recovery slot holds THIS turn's tree by the time the message exists. It matters because
    of what comes next: the turn's `finally` pardons the container and frees the slot, after
    which the reclamation path may take it, and anything not stored by then is stored on a
    machine somebody else is entitled to reclaim.

    Mutation check: give the spend bound its own securing function that composes the sentence
    first, and this goes red on the slot's head — which is precisely why `at_limit_ending` took
    a parameter instead of gaining a sibling."""
    await _seed_recovery(store)
    workspace = _Workspace(_container(bundles=THIS_TURN, head=ON_RECORD), _HANDLE, APP)

    ending = await at_limit_ending(workspace, sentence=SPENT_ENOUGH_TEXT)

    assert await _head_in_recovery_slot(store) == THIS_TURN
    assert ending.work_is_secured is True
    assert KEPT_A_COPY in ending.message
    # It really is the spend sentence — a shared function makes passing the wrong one easy.
    assert "working" in ending.message
    assert "midnight" not in ending.message
    assert alarms == [], "a copy that landed must not alarm"


async def test_a_failed_copy_changes_the_spend_sentence_and_alarms_it_too(
    store: FakeStorage, alarms: list[tuple[str, dict[str, object]]]
) -> None:
    """The failure trade is the same on both endings, and it has to be: the citizen is told
    either way, the reassurance stops being made, and an operator gets the pinned event.

    Raising instead would turn a bounded run into a crash for a citizen who did nothing wrong;
    swallowing is what left the 2026-08-18 reframe unfalsifiable."""
    await _seed_recovery(store)
    client = FakeSandboxClient()

    def wedged(cmd: list[str]) -> ExecResult:
        raise SandboxError("the container stopped answering")

    client.exec_handler = wedged
    workspace = _Workspace(client, _HANDLE, APP)

    ending = await at_limit_ending(workspace, sentence=SPENT_ENOUGH_TEXT)

    assert ending.work_is_secured is False
    assert COULD_NOT_KEEP_A_COPY in ending.message
    assert KEPT_A_COPY not in ending.message
    assert [event for event, _ in alarms] == [RECOVERY_WRITE_DID_NOT_LAND_EVENT]


async def test_a_turn_with_no_container_reaches_the_spend_bound_without_a_fault(
    alarms: list[tuple[str, dict[str, object]]],
) -> None:
    """A planning turn can reach a bound too, and it has nothing to secure. That is the one case
    where the reassurance is withheld without anything having gone wrong — which is why the
    wording asks the reader to save rather than announcing a fault, and why it must not alarm."""
    ending = await at_limit_ending(None, sentence=SPENT_ENOUGH_TEXT)

    assert ending.work_is_secured is False
    assert COULD_NOT_KEEP_A_COPY in ending.message
    assert alarms == [], "no container is not a missed recovery write"
