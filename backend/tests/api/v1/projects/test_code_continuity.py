"""Code continuity across sessions (U11, R21, KD-9).

The project's ONE app carries `current_code` as the source of truth: a builder session's code
PATCH writes it back, and the write-back is owner-scoped. Its live consumers are
`projects/router.py::description:generate` and `apps/router.py` — asserted here.

RETIRED (003-U2): the relay-side READ of `current_code` — the "current app code (continue from
this)" seed a builder-kind `/v1/claude` turn used to get — is gone. It existed for the era when
a builder turn streamed a single JSX file through the relay and had to continue from the last
one; that page now drives a build SESSION whose agent gets code from the restored workspace
snapshot. No production caller ever reached it after that change (BuilderPage does not import
`useClaudeAPI`), so the tests below were its only remaining callers. The retirement is pinned in
`tests/api/v1/claude/test_interview_protocol.py::test_builder_interview_turn_does_not_carry_the_code_seed`
— a `not in` assertion here would be vacuous, since nothing injects code into a prompt any more.
"""

from __future__ import annotations

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
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


async def test_build_writeback_mirrors_to_the_project_app(client, db_session) -> None:
    """A builder chat's code PATCH mirrors onto the project's ONE app (KD-9). This is the
    write-back half of code continuity, and the only writer of `current_code` there is."""
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv_a = await _builder_conv(db_session, user.id, project.id)
    await _provision(client, headers, conv_a.id, project.id)

    code = {"source": "export default () => <div>VERSION_ONE</div>", "entry": "App"}
    patch = await client.patch(
        f"/v1/conversations/{conv_a.id}", json={"code": code}, headers=headers
    )
    assert patch.status_code == 200
    app = await db_session.scalar(select(AppRegistry).where(AppRegistry.project_id == project.id))
    assert app is not None and app.current_code == {"current": code}

    # The mirror is project-scoped, not chat-scoped: a SECOND builder chat in the same project
    # writes to the same app row rather than forking a per-chat copy.
    conv_b = await _builder_conv(db_session, user.id, project.id)
    later = {"source": "export default () => <div>VERSION_TWO</div>", "entry": "App"}
    assert (
        await client.patch(f"/v1/conversations/{conv_b.id}", json={"code": later}, headers=headers)
    ).status_code == 200
    await db_session.refresh(app)
    assert app.current_code == {"current": later}


async def test_fresh_project_has_no_code_until_a_build_patches_it(client, db_session) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv = await _builder_conv(db_session, user.id, project.id)
    await _provision(client, headers, conv.id, project.id)

    app = await db_session.scalar(select(AppRegistry).where(AppRegistry.project_id == project.id))
    assert app is not None and app.current_code is None  # provision alone stores no code

    code = {"source": "SEED_ME_NOW", "entry": "App"}
    await client.patch(f"/v1/conversations/{conv.id}", json={"code": code}, headers=headers)
    await db_session.refresh(app)
    assert app.current_code == {"current": code}


async def test_submit_no_longer_touches_current_code(
    client, app, db_session, set_chat_model
) -> None:
    # INERTNESS GUARD (flipped, APPROVAL R19): submit used to backstop `current_code`
    # from its request body. The open-sandbox submit carries NO source — the artifact
    # is the server-side bundle copy — so the backstop is gone: submit succeeds and
    # `current_code` stays exactly what the conversations-PATCH mirror last wrote
    # (here: NULL, because the PATCH landed before the app row existed).
    from src.api.deps import storage_dependency, storage_or_none_dependency
    from src.services.storage import snapshot_key
    from tests.fakes import FakeStorage

    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
    # `submit` documents a 503, so it takes the None-tolerant seam; bind both to one store.
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv = await _builder_conv(db_session, user.id, project.id)

    code = {"source": "export default () => <div>BACKSTOP_ME</div>", "entry": "App"}
    patch = await client.patch(
        f"/v1/conversations/{conv.id}", json={"code": code}, headers=headers
    )
    assert patch.status_code == 200  # no app row yet — the mirror is a no-op

    prov = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(conv.id), "projectId": str(project.id)},
        headers=headers,
    )
    assert prov.status_code == 201
    app_id = prov.json()["appId"]

    import uuid as _uuid

    store.objects[snapshot_key(_uuid.UUID(app_id))] = (
        b"# v2 git bundle\n" + b"ce" * 20 + b" HEAD\n\nPACK-continuity"
    )
    submit = await client.post(f"/v1/apps/{app_id}/submit", headers=headers)
    assert submit.status_code == 200

    # The retired backstop stays retired: current_code is untouched by submit.
    row = await db_session.scalar(select(AppRegistry).where(AppRegistry.project_id == project.id))
    assert row is not None
    assert row.current_code is None

    # The documented consequence: with no code mirrored yet, description:generate
    # still 409s — code continuity is the conversations-PATCH mirror's job alone now.
    set_chat_model(TestModel(custom_output_text="never reached"))
    gen = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert gen.status_code == 409


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


async def test_planning_conversation_does_not_write_code(client, db_session) -> None:
    """Only builder chats carry code: a planning chat's PATCH must not mirror onto the app.
    (Whether a planning TURN gets builder-only prompt additions is the relay's concern —
    `tests/api/v1/claude/test_interview_protocol.py::test_planning_turn_unchanged`.)"""
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    builder = await _builder_conv(db_session, user.id, project.id)
    await _provision(client, headers, builder.id, project.id)
    planning = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ConversationKind.PLANNING
    )

    patch = await client.patch(
        f"/v1/conversations/{planning.id}",
        json={"code": {"source": "PLANNING_CODE", "entry": "App"}},
        headers=headers,
    )
    assert patch.status_code == 200  # stored on the chat header…

    app = await db_session.scalar(select(AppRegistry).where(AppRegistry.project_id == project.id))
    assert app is not None and app.current_code is None  # …but never mirrored to the app


async def test_cross_user_cannot_read_another_users_project_context(
    client, db_session, set_chat_model
) -> None:
    """`_project_context_system`'s owner-scoped conversation lookup is what stops user B from
    grounding a turn in user A's project (ADR-0004).

    Asserted on the project DESCRIPTION, not on code: with the relay's code seed retired
    (003-U2) there is nothing code-shaped left in a system prompt, so the old
    `"OWNER_SECRET_CODE" not in instructions` assertion would now pass even if the `user_id`
    predicate were dropped — i.e. it would be green and prove nothing. The description is the
    live cross-user surface on this seam, so that is what this pins.
    """
    owner = await UserFactory.create(db_session)
    owner_headers = _cookie(mint_session_jwt(owner.id, owner.token_version, _TTL))
    project = await ProjectFactory.create(
        db_session, owner.id, description="OWNER_SECRET_DESCRIPTION"
    )
    conv_a = await _builder_conv(db_session, owner.id, project.id)
    await _provision(client, owner_headers, conv_a.id, project.id)

    # The owner's OWN turn does get the description — otherwise the assertion below could pass
    # simply because nothing ever injects a description (the vacuity trap this test just escaped).
    model, captured = _capturing_stream_model()
    set_chat_model(model)
    assert (
        await client.post(
            "/v1/claude",
            headers=owner_headers,
            json={"messages": _CHAT, "conversationId": str(conv_a.id)},
        )
    ).status_code == 200
    assert "OWNER_SECRET_DESCRIPTION" in captured["instructions"]

    # User B references A's conversation id → owner-scoped lookup misses → no context at all.
    b_headers, _ = await _auth(db_session)
    model, captured = _capturing_stream_model()
    set_chat_model(model)
    resp = await client.post(
        "/v1/claude", headers=b_headers, json={"messages": _CHAT, "conversationId": str(conv_a.id)}
    )
    assert resp.status_code == 200
    assert "OWNER_SECRET_DESCRIPTION" not in captured["instructions"]
