"""The at-limit experience, said in the citizen's own words.

The refusal this replaced named a ROLE to contact and told the citizen to click Save. The tests
here pin the REGISTER (no file path, command, library or framework term reaches the reader) and
the three facts the sentence has to carry. The register check runs over the RENDERED sentence
rather than the template, because the configured support address is substituted in at render time
and is the one part of this message that comes from outside the copy module.
"""

from __future__ import annotations

import pytest

from src.config import settings
from src.db.models.user_limit import UserLimit
from src.services.turns.copy import AT_LIMIT_TEXT, SPENT_ENOUGH_TEXT
from src.services.usage.gate import DailyTokenLimitExceededError, record_usage
from tests.factories import ConversationFactory, UserFactory

# Long enough that a slow suite cannot expire a session mid-request.
_TTL_SECONDS = 300


def _at_limit_sentence() -> str:
    return AT_LIMIT_TEXT.format(contact=settings.SUPPORT_CONTACT_EMAIL)


# =============================================================================
# The sentence
# =============================================================================


def test_the_at_limit_sentence_says_what_happened_when_it_comes_back_and_who_to_ask() -> None:
    """The three facts a citizen needs, and the platform used to supply none properly: what
    happened, when they can carry on, and a real address to ask for more — "contact your
    administrator" named a ROLE, a dead end at exactly the moment they most needed a way out.

    Mutation check: drop `{contact}` from `AT_LIMIT_TEXT` and this goes red."""
    message = _at_limit_sentence()

    assert "budget" in message, "what happened, in the reader's own vocabulary"
    assert "midnight" in message, "when they can carry on"
    assert settings.SUPPORT_CONTACT_EMAIL in message, "a real address, not a role"
    # The address is CONFIGURED, never hardcoded — a baked-in fallback would still pass the
    # assertion above.
    assert settings.SUPPORT_CONTACT_EMAIL not in AT_LIMIT_TEXT


def test_the_ending_promises_nothing_about_whether_the_work_was_kept() -> None:
    """★ NO CLAIM ABOUT DURABILITY, on either ending. The container survives an at-limit turn,
    so nothing here has stored anything — and a reassurance the platform did not earn is the one
    a citizen acts on by closing the tab.

    Mutation check: fold a "we've kept a copy of your app" clause back into either sentence and
    this goes red."""
    for sentence in (_at_limit_sentence(), SPENT_ENOUGH_TEXT):
        lowered = sentence.lower()
        assert "kept a copy" not in lowered
        assert "nothing you did today is lost" not in lowered
        assert "{" not in sentence, "every field is filled by the time a citizen reads it"


def test_the_message_carries_no_file_path_command_library_or_framework_term() -> None:
    """The half of the no-developer-jargon rule that checks the RENDERED sentence, not the
    template: `test_no_sentence_this_plan_shows_a_citizen_carries_developer_jargon` already
    sweeps the copy module, but only the template — the one substitution here comes from
    deployment configuration, which nobody reviews as prose (e.g. `ops@…/srv/logs`).

    Mutation check: put any of the terms below into `AT_LIMIT_TEXT` and this goes red."""
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
    for sentence in (_at_limit_sentence(), SPENT_ENOUGH_TEXT):
        for term in forbidden:
            assert term not in sentence, f"{term!r} reached a citizen in {sentence!r}"


def test_the_spend_bound_and_the_daily_budget_are_different_sentences() -> None:
    """The daily budget resets at midnight; the spend bound does not stop the citizen at all. A
    shared sentence would tell somebody who can carry on right now to wait until tomorrow."""
    assert "working" in SPENT_ENOUGH_TEXT
    assert "midnight" not in SPENT_ENOUGH_TEXT
    assert "midnight" in _at_limit_sentence()


# =============================================================================
# The second send, and the route-level refusal it meets
# =============================================================================


async def test_a_second_send_at_the_limit_is_refused_before_any_turn_exists(
    client, db_session
) -> None:
    """★ The refusal is written ONCE: a citizen who sends again must not stack a second
    identical paragraph into their transcript. The two refusals come from different places —
    the FIRST is an in-turn ending; every later send never reaches a turn at all, since the
    route's `enforce_daily_limit` answers 429 first — asserted on the transcript, not the status
    code alone, because "refused" and "nothing was written" are different claims.

    Mutation check: move the route's `enforce_daily_limit` to after the turn is persisted and
    this goes red."""
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
    """The SPA's interceptor reads all five keys, and this unit touches the
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
        # Required with no default by every role — omitting it would fail for a reason unrelated
        # to the support contact this file is about.
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
        return ApiSettings(
            _env_file=None  # pyright: ignore[reportCallIssue]
        )
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_the_api_refuses_to_start_without_a_support_contact() -> None:
    """★ The fail-first guard: this must be set in the App Service configuration before the
    release ships, or the API does not boot — the cheaper failure by a wide margin. The
    alternative is a default, which can only be a placeholder address that sends a citizen who
    is already stuck to a mailbox nobody reads, surfacing as silence weeks later from the person
    least able to escalate it.

    Mutation check: give `SUPPORT_CONTACT_EMAIL` any default and this goes red."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="SUPPORT_CONTACT_EMAIL"):
        _boot(_api_env(without="SUPPORT_CONTACT_EMAIL"))

    # The control: the same block WITH the variable boots — the failure above is about this
    # field, not something else the scrubbed environment is missing.
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
