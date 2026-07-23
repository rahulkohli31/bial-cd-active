"""The server-side interview protocol on builder threads (003-U2).

`_project_context_system` appends the ask-then-brief protocol to every BUILDER-kind relay turn,
tamper-proof (the client's own `system` rides along unchanged). Asserted against the system
prompt the faked model actually receives — the site that makes the decision, not a passthrough.
"""

from __future__ import annotations

from pydantic_ai.models.function import FunctionModel

from src.api.v1.claude.prompts import (
    ASSISTANT_IDENTITY_PROMPT,
    BUILD_BRIEF_FENCE_TAG,
    BUILD_INTERVIEW_PROTOCOL,
    PLANNING_SYSTEM_PROMPT,
    PORTAL_SELF_DESCRIPTION,
)
from src.config import settings
from src.db.models.conversation import ConversationKind
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.appdata import resolve_app_for_project
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_PROMPT = "I need a visitor app"


async def _auth(db_session):
    user = await UserFactory.create(db_session)
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    return {"Cookie": f"session={jwt}"}, user


def _capturing_stream_model():
    captured: dict[str, str] = {}

    async def _stream(messages, info):
        captured["instructions"] = info.instructions or ""
        yield "streamed"

    return FunctionModel(stream_function=_stream), captured


async def _turn(client, db_session, set_chat_model, kind, *, description=None):
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id, description=description)
    conv = await ConversationFactory.create(db_session, user.id, project_id=project.id, kind=kind)
    model, captured = _capturing_stream_model()
    set_chat_model(model)
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(conv.id), "message": {"text": _PROMPT}},
    )
    assert resp.status_code == 200
    return captured["instructions"]


# --- the protocol lands on builder threads, and only builder threads ----------


async def test_builder_turn_carries_protocol_and_project_context(
    client, db_session, set_chat_model
) -> None:
    instructions = await _turn(
        client,
        db_session,
        set_chat_model,
        ConversationKind.BUILDER,
        description="Visitor management for T1.",
    )

    assert BUILD_INTERVIEW_PROTOCOL in instructions
    # U7: the server SELECTS the base prompt by kind — a builder thread opens with the
    # assistant identity line (the ex-client THREAD_SYSTEM_PROMPT, now server-owned).
    assert instructions.startswith(ASSISTANT_IDENTITY_PROMPT)
    assert "Visitor management for T1." in instructions


async def test_planning_turn_gets_no_protocol_but_keeps_grounding(
    client, db_session, set_chat_model
) -> None:
    instructions = await _turn(
        client,
        db_session,
        set_chat_model,
        ConversationKind.PLANNING,
        description="Visitor management for T1.",
    )

    # Planning runs its own (server-selected, U7) planning interview; the build protocol is
    # builder-only.
    assert BUILD_INTERVIEW_PROTOCOL not in instructions
    # #6/R5: the truthful portal self-description rides on EVERY turn, planning included —
    # the walkthrough's invented-features answer came from a non-builder chat. Exact
    # composition pinned: base-by-kind, self-description, project grounding — nothing else.
    assert instructions == (
        PLANNING_SYSTEM_PROMPT
        + f"\n\n{PORTAL_SELF_DESCRIPTION}"
        + "\n\nProject context — Test Project:\nVisitor management for T1."
    )


async def test_protocol_rides_every_builder_turn_unconditionally(
    client, db_session, set_chat_model
) -> None:
    """U7 closes the tamper door for good: there is no client `system` field left to omit or
    replace — the protocol composes from the conversation's own kind, every builder turn."""
    instructions = await _turn(client, db_session, set_chat_model, ConversationKind.BUILDER)

    assert BUILD_INTERVIEW_PROTOCOL in instructions


# --- drift pins ---------------------------------------------------------------


def test_protocol_pins_the_brief_fence_contract() -> None:
    """The fence tag is shared state with the portal's parser: if the protocol stops naming it,
    the model stops emitting a parseable brief and the build button silently never appears —
    a drift with no failure mode a user could report. Pinned on both sides."""
    assert BUILD_BRIEF_FENCE_TAG == "bial:build-brief"
    assert f"```{BUILD_BRIEF_FENCE_TAG}" in BUILD_INTERVIEW_PROTOCOL


# --- #6/R5: the truthful portal self-description ------------------------------


async def test_builder_turn_also_carries_the_self_description(
    client, db_session, set_chat_model
) -> None:
    instructions = await _turn(client, db_session, set_chat_model, ConversationKind.BUILDER)

    assert PORTAL_SELF_DESCRIPTION in instructions
    # Identity precedes the interview protocol: the model learns what it is part of before
    # it learns how to run the conversation.
    assert instructions.index(PORTAL_SELF_DESCRIPTION) < instructions.index(
        BUILD_INTERVIEW_PROTOCOL
    )


def test_self_description_names_only_surfaces_the_portal_actually_has() -> None:
    """The R5 drift pin. Every surface the clause names must exist as a route in
    `portal/src/App.jsx` (Dashboard, Projects, chat, builder preview, Help, Admin) — and
    the clause must carry its two teeth: the closed-world line (no other tabs/pages) and
    the say-so-plainly instruction, which are what actually stop the model from inventing
    a Settings page to send the user to. If the portal grows a surface, extend the clause
    AND this list together."""
    for real_surface in ("Dashboard", "Projects list", "Help page", "Admin review area"):
        assert real_surface in PORTAL_SELF_DESCRIPTION
    assert "live preview" in PORTAL_SELF_DESCRIPTION
    assert "submit-for-review" in PORTAL_SELF_DESCRIPTION
    # The closed-world line — the sentence doing the actual R5 work.
    assert "There are no other tabs" in PORTAL_SELF_DESCRIPTION
    # Uncertainty resolves to honesty, never to inventing a destination.
    assert "say so plainly" in PORTAL_SELF_DESCRIPTION
    # The closed-world sentence names the classic hallucinated destinations by name —
    # trimming any of them out re-opens the door the walkthrough walked through.
    denial = PORTAL_SELF_DESCRIPTION[PORTAL_SELF_DESCRIPTION.index("There are no other") :]
    for named_denial in ("file browsers", "settings screens", "export menus"):
        assert named_denial in denial


def test_protocol_forbids_app_side_authentication() -> None:
    """The never-authenticate guardrail is prose today, so it is one careless reword from gone.
    Generated apps get their session from the portal (C9); an app that builds its own login is
    both broken and a security hole (`sandboxed-app-auth-session-injection` learning)."""
    assert "NEVER specify a login" in BUILD_INTERVIEW_PROTOCOL
    assert "authentication" in BUILD_INTERVIEW_PROTOCOL
    # And it must not instruct the opposite anywhere.
    lowered = BUILD_INTERVIEW_PROTOCOL.lower()
    assert "add a login" not in lowered
    assert "implement authentication" not in lowered


def test_protocol_bans_asking_and_briefing_in_one_turn() -> None:
    """The card is rendered from the fence; questions alongside it would ask the user to answer
    and confirm the same turn, which the parser cannot represent."""
    assert "NEVER emit questions and a brief in the same reply" in BUILD_INTERVIEW_PROTOCOL


# --- the code seed does not ride along ----------------------------------------


async def test_builder_interview_turn_does_not_carry_the_code_seed(
    client, db_session, set_chat_model
) -> None:
    """The retired U11 seed must not come back: it would push the project's whole source
    (up to 300k chars ≈ 75k tokens) against the daily cap on EVERY interview turn, to answer
    questions that need no code. The BUILD gets code from the restored workspace, not here."""
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ConversationKind.BUILDER
    )
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    # Seed `current_code` directly: the conversations-PATCH mirror died with 0024, and the
    # point here is that the RELAY never injects code — however the row came to hold it.
    from sqlalchemy import update

    from src.db.models.app_registry import AppRegistry

    await db_session.execute(
        update(AppRegistry)
        .where(AppRegistry.id == app_id)
        .values(
            current_code={
                "current": {
                    "source": "export default () => <div>VERSION_ONE</div>",
                    "entry": "App",
                }
            }
        )
    )
    await db_session.commit()

    model, captured = _capturing_stream_model()
    set_chat_model(model)
    assert (
        await client.post(
            "/v1/claude",
            headers=headers,
            json={"conversationId": str(conv.id), "message": {"text": _PROMPT}},
        )
    ).status_code == 200

    assert "VERSION_ONE" not in captured["instructions"]
    assert "current app code" not in captured["instructions"]
