"""Code continuity across sessions — what SURVIVES the U4 reset.

RETIRED WITH 0024: the conversations-PATCH `code` mirror (the SPA's write-back path into
`app_registry.current_code`) died with the `conversations.code` column — the PATCH now 400s
(pinned in `tests/api/v1/conversations/test_conversations.py::test_patch_code_is_retired_400`).
`current_code` therefore has NO writer on this branch; code truth lives in the build snapshots,
and the remaining readers (`projects/router.py::description:generate`, `apps/router.py`) treat
a NULL as "no code yet". TODO(U5+): either re-establish a writer from the build pipeline or
retire the readers with the column.

What stays pinned here:
  * submit never touches `current_code` (the open-sandbox artifact is the bundle copy);
  * the relay's project-context system prompt is owner-scoped (ADR-0004) — the description,
    the live cross-user surface on that seam, never leaks across users.
"""

from __future__ import annotations

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import ConversationKind
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.appdata import resolve_app_for_project
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


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


async def _provision(db_session, user_id, project_id) -> str:
    """Mint the project's app the way the build session does (`POST /apps/provision` was
    removed in U6). Commits so the endpoints under test read it through their own session."""
    app_id = await resolve_app_for_project(db_session, user_id, project_id)
    await db_session.commit()
    return str(app_id)


async def test_submit_no_longer_touches_current_code(
    client, app, db_session, set_chat_model
) -> None:
    # INERTNESS GUARD (flipped, APPROVAL R19): submit used to backstop `current_code`
    # from its request body. The open-sandbox submit carries NO source — the artifact
    # is the server-side bundle copy — so `current_code` stays exactly what it was
    # (here: NULL, its permanent state now that the PATCH mirror is retired).
    from src.api.deps import storage_dependency, storage_or_none_dependency
    from src.services.storage import snapshot_key
    from tests.fakes import FakeStorage

    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
    # `submit` documents a 503, so it takes the None-tolerant seam; bind both to one store.
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await _builder_conv(db_session, user.id, project.id)

    app_id = await _provision(db_session, user.id, project.id)

    import uuid as _uuid

    store.objects[snapshot_key(_uuid.UUID(app_id))] = (
        b"# v2 git bundle\n" + b"ce" * 20 + b" HEAD\n\nPACK-continuity"
    )
    # V4: submit's body is now required — an all-No answer set; this test's focus is
    # `current_code`, not the questionnaire.
    submit_body = {
        "answers": {
            "credentialsSecrets": False,
            "healthData": False,
            "personalInformation": False,
            "financialData": False,
            "confidentialBusinessData": False,
            "publicData": False,
            "notes": None,
        }
    }
    submit = await client.post(f"/v1/apps/{app_id}/submit", headers=headers, json=submit_body)
    assert submit.status_code == 200

    # The retired backstop stays retired: current_code is untouched by submit.
    row = await db_session.scalar(select(AppRegistry).where(AppRegistry.project_id == project.id))
    assert row is not None
    assert row.current_code is None

    # The documented consequence: with no code recorded, description:generate still 409s.
    set_chat_model(TestModel(custom_output_text="never reached"))
    gen = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert gen.status_code == 409


async def test_cross_user_cannot_read_another_users_project_context(
    client, db_session, set_chat_model
) -> None:
    """`_project_context_system`'s owner-scoped conversation lookup is what stops user B from
    grounding a turn in user A's project (ADR-0004).

    Asserted on the project DESCRIPTION, not on code: with the relay's code seed retired
    (003-U2) there is nothing code-shaped left in a system prompt, so an old
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
    await _provision(db_session, owner.id, project.id)

    # The owner's OWN turn does get the description — otherwise the assertion below could pass
    # simply because nothing ever injects a description (the vacuity trap this test just escaped).
    model, captured = _capturing_stream_model()
    set_chat_model(model)
    assert (
        await client.post(
            "/v1/claude",
            headers=owner_headers,
            json={"conversationId": str(conv_a.id), "message": {"text": "continue the build"}},
        )
    ).status_code == 200
    assert "OWNER_SECRET_DESCRIPTION" in captured["instructions"]

    # User B references A's conversation id → owner-scoped lookup misses → U7's non-leaking
    # 404 (the old stream-without-context arm retired with the browser payload).
    b_headers, _ = await _auth(db_session)
    model, captured = _capturing_stream_model()
    set_chat_model(model)
    resp = await client.post(
        "/v1/claude",
        headers=b_headers,
        json={"conversationId": str(conv_a.id), "message": {"text": "continue the build"}},
    )
    assert resp.status_code == 404
    assert "OWNER_SECRET_DESCRIPTION" not in captured.get("instructions", "")
