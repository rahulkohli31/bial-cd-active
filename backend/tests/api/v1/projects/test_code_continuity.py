"""Code continuity across sessions (U11, R21, KD-9).

The project's ONE app carries `current_code` as the source of truth: a builder session's
code PATCH writes it back, a later builder session seeds from it, and the write-back is
owner-scoped. The seed is asserted against the system prompt the (faked) model receives.
"""

from __future__ import annotations

from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import ConversationKind
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_CHAT = [{"role": "user", "content": "continue the build"}]


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session):
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


def _capturing_stream_model():
    captured: dict[str, str] = {}

    async def _stream(messages, info):
        captured["instructions"] = info.instructions or ""
        yield "streamed"

    return FunctionModel(stream_function=_stream), captured


async def _builder_conv(db_session, user_id, project_id):
    return await ConversationFactory.create(
        db_session, user_id, project_id=project_id, kind=ConversationKind.BUILDER
    )


async def _provision(client, headers, conversation_id, project_id) -> None:
    resp = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(conversation_id), "projectId": str(project_id)},
        headers=headers,
    )
    assert resp.status_code == 201


async def test_build_writeback_then_seed_across_conversations(
    client, db_session, set_chat_model
) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv_a = await _builder_conv(db_session, user.id, project.id)
    await _provision(client, headers, conv_a.id, project.id)

    # Build in A → PATCH the code → it mirrors to the project app's current_code.
    code = {"source": "export default () => <div>VERSION_ONE</div>", "entry": "App"}
    patch = await client.patch(
        f"/v1/conversations/{conv_a.id}", json={"code": code}, headers=headers
    )
    assert patch.status_code == 200
    app = await db_session.scalar(select(AppRegistry).where(AppRegistry.project_id == project.id))
    assert app is not None and app.current_code == {"current": code}

    # Open a NEW builder conversation B in the same project → its build turn is seeded from
    # the project's current_code (A's latest), not a blank slate.
    conv_b = await _builder_conv(db_session, user.id, project.id)
    model, captured = _capturing_stream_model()
    set_chat_model(model)
    resp = await client.post(
        "/v1/claude", headers=headers, json={"messages": _CHAT, "conversationId": str(conv_b.id)}
    )
    assert resp.status_code == 200
    assert "VERSION_ONE" in captured["instructions"]


async def test_fresh_project_seeds_empty_then_populates(
    client, db_session, set_chat_model
) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv = await _builder_conv(db_session, user.id, project.id)
    await _provision(client, headers, conv.id, project.id)

    # First build turn: no current_code yet → seeds empty (no code injected).
    model, captured = _capturing_stream_model()
    set_chat_model(model)
    resp = await client.post(
        "/v1/claude", headers=headers, json={"messages": _CHAT, "conversationId": str(conv.id)}
    )
    assert resp.status_code == 200
    assert "current app code" not in captured["instructions"]

    # The build produces code → PATCH → current_code is now populated.
    code = {"source": "SEED_ME_NOW", "entry": "App"}
    await client.patch(f"/v1/conversations/{conv.id}", json={"code": code}, headers=headers)
    app = await db_session.scalar(select(AppRegistry).where(AppRegistry.project_id == project.id))
    assert app is not None and app.current_code == {"current": code}


async def test_second_build_advances_current_code(client, db_session, set_chat_model) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv = await _builder_conv(db_session, user.id, project.id)
    await _provision(client, headers, conv.id, project.id)

    first = {"source": "V1", "entry": "App"}
    await client.patch(f"/v1/conversations/{conv.id}", json={"code": first}, headers=headers)
    second = {"source": "V2", "entry": "App"}
    await client.patch(f"/v1/conversations/{conv.id}", json={"code": second}, headers=headers)

    app = await db_session.scalar(select(AppRegistry).where(AppRegistry.project_id == project.id))
    assert app is not None and app.current_code == {"current": second}  # write-back each build


async def test_planning_conversation_does_not_seed_or_write(
    client, db_session, set_chat_model
) -> None:
    # Only builder sessions carry code — a planning chat neither seeds nor writes current_code.
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    builder = await _builder_conv(db_session, user.id, project.id)
    await _provision(client, headers, builder.id, project.id)
    # Seed the app with code via the builder.
    await client.patch(
        f"/v1/conversations/{builder.id}",
        json={"code": {"source": "BUILDER_CODE", "entry": "App"}},
        headers=headers,
    )
    planning = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ConversationKind.PLANNING
    )
    model, captured = _capturing_stream_model()
    set_chat_model(model)
    resp = await client.post(
        "/v1/claude", headers=headers, json={"messages": _CHAT, "conversationId": str(planning.id)}
    )
    assert resp.status_code == 200
    # A planning chat gets no code seed (builder-only, U11).
    assert "current app code" not in captured["instructions"]


async def test_cross_user_cannot_seed_from_another_users_project(
    client, db_session, set_chat_model
) -> None:
    # User A owns a project with built code.
    owner = await UserFactory.create(db_session)
    owner_headers = _cookie(mint_session_jwt(owner.id, owner.token_version, _TTL))
    project = await ProjectFactory.create(db_session, owner.id)
    conv_a = await _builder_conv(db_session, owner.id, project.id)
    await _provision(client, owner_headers, conv_a.id, project.id)
    await client.patch(
        f"/v1/conversations/{conv_a.id}",
        json={"code": {"source": "OWNER_SECRET_CODE", "entry": "App"}},
        headers=owner_headers,
    )

    # User B references A's conversation id in a chat turn → owner-scoped lookup misses → no
    # seed (B can neither read A's conversation nor seed from A's project app).
    b_headers, _ = await _auth(db_session)
    model, captured = _capturing_stream_model()
    set_chat_model(model)
    resp = await client.post(
        "/v1/claude", headers=b_headers, json={"messages": _CHAT, "conversationId": str(conv_a.id)}
    )
    assert resp.status_code == 200
    assert "OWNER_SECRET_CODE" not in captured["instructions"]
