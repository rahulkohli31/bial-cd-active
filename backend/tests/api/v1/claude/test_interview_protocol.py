"""The server-side interview protocol on builder threads (003-U2).

`_project_context_system` appends the ask-then-brief protocol to every BUILDER-kind relay turn,
tamper-proof (the client's own `system` rides along unchanged). Asserted against the system
prompt the faked model actually receives — the site that makes the decision, not a passthrough.
"""

from __future__ import annotations

from pydantic_ai.models.function import FunctionModel

from src.api.v1.claude.prompts import BUILD_BRIEF_FENCE_TAG, BUILD_INTERVIEW_PROTOCOL
from src.config import settings
from src.db.models.conversation import ConversationKind
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.appdata import resolve_app_for_project
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_CHAT = [{"role": "user", "content": "I need a visitor app"}]


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


async def _turn(client, db_session, set_chat_model, kind, *, system=None, description=None):
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id, description=description)
    conv = await ConversationFactory.create(db_session, user.id, project_id=project.id, kind=kind)
    model, captured = _capturing_stream_model()
    set_chat_model(model)
    body: dict[str, object] = {"messages": _CHAT, "conversationId": str(conv.id)}
    if system is not None:
        body["system"] = system
    resp = await client.post("/v1/claude", headers=headers, json=body)
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
        system="CLIENT PROMPT",
        description="Visitor management for T1.",
    )

    assert BUILD_INTERVIEW_PROTOCOL in instructions
    # The client's own system prompt is preserved, not replaced — the append is additive.
    assert "CLIENT PROMPT" in instructions
    assert "Visitor management for T1." in instructions


async def test_planning_turn_unchanged(client, db_session, set_chat_model) -> None:
    instructions = await _turn(
        client,
        db_session,
        set_chat_model,
        ConversationKind.PLANNING,
        system="CLIENT PROMPT",
        description="Visitor management for T1.",
    )

    # Planning keeps its own client-side interview; the build protocol is builder-only.
    assert BUILD_INTERVIEW_PROTOCOL not in instructions
    assert (
        instructions
        == "CLIENT PROMPT\n\nProject context — Test Project:\nVisitor management for T1."
    )


async def test_protocol_is_appended_even_when_client_sends_no_system(
    client, db_session, set_chat_model
) -> None:
    """Tamper-proofing has to survive the simplest tamper: sending no system prompt at all."""
    instructions = await _turn(client, db_session, set_chat_model, ConversationKind.BUILDER)

    assert BUILD_INTERVIEW_PROTOCOL in instructions


# --- drift pins ---------------------------------------------------------------


def test_protocol_pins_the_brief_fence_contract() -> None:
    """The fence tag is shared state with the portal's parser: if the protocol stops naming it,
    the model stops emitting a parseable brief and the build button silently never appears —
    a drift with no failure mode a user could report. Pinned on both sides."""
    assert BUILD_BRIEF_FENCE_TAG == "bial:build-brief"
    assert f"```{BUILD_BRIEF_FENCE_TAG}" in BUILD_INTERVIEW_PROTOCOL


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
    await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    code = {"source": "export default () => <div>VERSION_ONE</div>", "entry": "App"}
    assert (
        await client.patch(f"/v1/conversations/{conv.id}", json={"code": code}, headers=headers)
    ).status_code == 200

    model, captured = _capturing_stream_model()
    set_chat_model(model)
    assert (
        await client.post(
            "/v1/claude",
            headers=headers,
            json={"messages": _CHAT, "conversationId": str(conv.id)},
        )
    ).status_code == 200

    assert "VERSION_ONE" not in captured["instructions"]
    assert "current app code" not in captured["instructions"]


# --- issue #28: the `system` cap ----------------------------------------------


async def test_system_within_cap_streams(client, db_session, set_chat_model) -> None:
    instructions = await _turn(
        client, db_session, set_chat_model, ConversationKind.PLANNING, system="x" * (64 * 1024)
    )
    assert "x" * 100 in instructions


async def test_oversized_system_400(client, db_session, set_chat_model) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    # A model must be bound: the 503 not-configured gate sits BEFORE body parsing, so an
    # unbound model would mask the 400 this test is about.
    set_chat_model(_capturing_stream_model()[0])

    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "messages": _CHAT,
            "conversationId": str(conv.id),
            "system": "x" * (64 * 1024 + 1),
        },
    )

    # This router's body-contract violations raise AppApiError(400) → the `error` envelope,
    # never FastAPI's 422 `detail` shape (the established envelope for this endpoint).
    assert resp.status_code == 400
    assert "system" in resp.json()["error"]["message"]


async def test_system_cap_counts_utf8_bytes_not_characters(
    client, db_session, set_chat_model
) -> None:
    """A multi-byte character costs its real tokens, so the cap counts bytes — a char-counting
    cap would let a 3x-larger prompt through."""
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    set_chat_model(_capturing_stream_model()[0])

    # 22k aircraft × 3 bytes = 66 KiB > the cap, while being only 22k CHARACTERS — comfortably
    # under it if the cap counted characters.
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={"messages": _CHAT, "conversationId": str(conv.id), "system": "✈" * (22 * 1024)},
    )

    assert resp.status_code == 400
