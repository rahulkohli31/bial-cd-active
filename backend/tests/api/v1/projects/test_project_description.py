"""Project description generation (U7, R15-R20, KD-5/KD-8).

Foundry-only agent path (a TestModel/FunctionModel stands in): fresh project rejects with
409, code-bearing project generates + persists, regenerate feeds the current description in,
the daily gate is honored, the fed code is bounded, and the result is length-capped.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import delete, select

from src.api.v1.claude.prompts import ASSISTANT_IDENTITY_PROMPT, PORTAL_SELF_DESCRIPTION
from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.project import Project
from src.db.models.token_usage import TokenUsage
from src.db.models.user_limit import UserLimit
from src.services.auth.session_jwt import mint_session_jwt
from src.services.usage.gate import record_usage
from tests.factories import (
    AppRegistryFactory,
    ConversationFactory,
    ProjectFactory,
    UserFactory,
)


def _capturing_stream_model():
    """A streaming FunctionModel that records the system prompt (agent instructions) the
    model actually received — the fake 'model client' the U8/U11 injection is asserted against."""
    captured: dict[str, str] = {}

    async def _stream(messages, info):
        captured["instructions"] = info.instructions or ""
        yield "streamed"

    return FunctionModel(stream_function=_stream), captured


_TTL = settings.auth.access_ttl_seconds
_CODE = {"current": {"source": "export default () => <div>VIP movement</div>", "entry": "App"}}


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session):
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


def _all_text(messages: list[ModelMessage]) -> str:
    """Flatten every string part across the model input (the built prompt lives here)."""
    out: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", []):
            content = getattr(part, "content", None)
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.extend(c for c in content if isinstance(c, str))
    return "\n".join(out)


async def test_generate_fresh_project_409(client, db_session, set_chat_model) -> None:
    set_chat_model(TestModel(custom_output_text="should not run"))
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)  # no app at all

    resp = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert resp.status_code == 409
    reloaded = await db_session.get(Project, project.id)
    assert reloaded is not None and reloaded.description is None  # nothing written


async def test_generate_null_current_code_409(client, db_session, set_chat_model) -> None:
    set_chat_model(TestModel(custom_output_text="should not run"))
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    # An app with NULL current_code — provisioned but never built.
    await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)

    resp = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert resp.status_code == 409


async def test_generate_from_code_persists(client, db_session, set_chat_model) -> None:
    set_chat_model(TestModel(custom_output_text="  Tracks VIP movements at the airport.  "))
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await AppRegistryFactory.create(
        db_session, user_id=user.id, project_id=project.id, current_code=_CODE
    )

    resp = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["description"] == "Tracks VIP movements at the airport."  # stripped
    reloaded = await db_session.get(Project, project.id)
    assert reloaded is not None
    assert reloaded.description == "Tracks VIP movements at the airport."


async def test_regenerate_feeds_current_description(client, db_session, set_chat_model) -> None:
    captured: dict[str, str] = {}

    def _capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["prompt"] = _all_text(messages)
        return ModelResponse(parts=[TextPart(content="A revised, sharper description.")])

    set_chat_model(FunctionModel(_capture))
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(
        db_session, user.id, description="An old, stale description the author wrote."
    )
    await AppRegistryFactory.create(
        db_session, user_id=user.id, project_id=project.id, current_code=_CODE
    )

    resp = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert resp.status_code == 200
    # The current description was fed to the model so it revises rather than discards (R19).
    assert "An old, stale description the author wrote." in captured["prompt"]
    # ...and the result overwrites.
    assert resp.json()["description"] == "A revised, sharper description."


async def test_generated_input_is_bounded(client, db_session, set_chat_model, monkeypatch) -> None:
    monkeypatch.setattr("src.services.projects.describe._CODE_CHAR_BUDGET", 40)
    captured: dict[str, str] = {}

    def _capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["prompt"] = _all_text(messages)
        return ModelResponse(parts=[TextPart(content="bounded")])

    set_chat_model(FunctionModel(_capture))
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    big_source = "SECRET_MARKER_" + ("x" * 500)
    await AppRegistryFactory.create(
        db_session,
        user_id=user.id,
        project_id=project.id,
        current_code={"current": {"source": big_source, "entry": "App"}},
    )

    resp = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert resp.status_code == 200
    # The code was truncated to the budget — the full 500-char body is not present.
    assert "[... code truncated" in captured["prompt"]
    assert big_source not in captured["prompt"]


async def test_generated_result_is_length_capped(client, db_session, set_chat_model) -> None:
    set_chat_model(TestModel(custom_output_text="a" * 5000))  # over MAX_PROJECT_DESCRIPTION
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await AppRegistryFactory.create(
        db_session, user_id=user.id, project_id=project.id, current_code=_CODE
    )

    resp = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()["description"]) == 2000  # capped at MAX_PROJECT_DESCRIPTION


async def test_generation_bills_usage(client, db_session, set_chat_model) -> None:
    # Generation is a normal billed turn (Q5): a successful run MUST fold its tokens into
    # today's usage — dropping record_usage would otherwise keep the suite green.
    set_chat_model(TestModel(custom_output_text="Billed description."))
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await AppRegistryFactory.create(
        db_session, user_id=user.id, project_id=project.id, current_code=_CODE
    )

    resp = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert resp.status_code == 200
    row = await db_session.scalar(select(TokenUsage).where(TokenUsage.user_id == user.id))
    assert row is not None  # a usage row was written for the turn
    # The billable total is input + output (cache is already inside input_tokens).
    total = row.input_tokens + row.output_tokens
    assert total > 0


async def test_blank_generation_clears_to_null_not_empty_string(
    client, db_session, set_chat_model
) -> None:
    # KD-8: the empty string is never persisted — a blank generation lands as NULL,
    # matching the schema-boundary normalization on the author path.
    set_chat_model(TestModel(custom_output_text="   "))
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id, description="Old text.")
    await AppRegistryFactory.create(
        db_session, user_id=user.id, project_id=project.id, current_code=_CODE
    )

    resp = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["description"] is None
    reloaded = await db_session.get(Project, project.id)
    assert reloaded is not None
    assert reloaded.description is None


async def test_generation_model_failure_is_explicit_500_envelope(
    client, db_session, set_chat_model, monkeypatch
) -> None:
    # A Foundry/model failure must surface as this route's OWN 500 envelope (mirroring the
    # chat relay's pre-delta failure), never the generic `{detail}` handler — and the
    # rolled-back transaction must leave the stored description untouched.
    set_chat_model(TestModel(custom_output_text="unused — the agent raises first"))
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id, description="Old text.")
    await AppRegistryFactory.create(
        db_session, user_id=user.id, project_id=project.id, current_code=_CODE
    )

    from src.services.projects import describe

    class _FoundryFellOver:
        async def run(self, *args, **kwargs):
            raise RuntimeError("upstream model failed")

    monkeypatch.setattr(describe, "chat_agent", _FoundryFellOver())

    resp = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert resp.status_code == 500
    assert resp.json() == {"error": {"message": "The description generation failed."}}
    reloaded = await db_session.get(Project, project.id)
    assert reloaded is not None
    assert reloaded.description == "Old text."  # nothing persisted from the failed turn


async def test_generation_respects_daily_gate_429(client, db_session, set_chat_model) -> None:
    set_chat_model(TestModel(custom_output_text="should not run"))
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await AppRegistryFactory.create(
        db_session, user_id=user.id, project_id=project.id, current_code=_CODE
    )
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=10))
    await db_session.flush()
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=0)

    resp = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "daily_token_limit_exceeded"


async def test_generate_cross_user_404(client, db_session, set_chat_model) -> None:
    set_chat_model(TestModel(custom_output_text="x"))
    headers, _ = await _auth(db_session)
    other = await UserFactory.create(db_session)
    victim = await ProjectFactory.create(db_session, other.id)
    resp = await client.post(f"/v1/projects/{victim.id}/description:generate", headers=headers)
    assert resp.status_code == 404


async def test_generate_requires_auth_401(client) -> None:
    resp = await client.post(f"/v1/projects/{uuid.uuid4()}/description:generate")
    assert resp.status_code == 401


async def test_generate_not_configured_returns_503(client, db_session) -> None:
    # No model override → the shared `chat_model` dependency returns None (Foundry unset in
    # test) → 503, fired BEFORE any app/code lookup (a fresh project is still a 503, not the
    # 409 "nothing to generate"). Mirrors claude test_not_configured_returns_503.
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    resp = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert resp.status_code == 503
    assert resp.json() == {"error": {"message": "Claude client not configured."}}


@pytest.mark.filterwarnings("ignore:transaction already deassociated:sqlalchemy.exc.SAWarning")
async def test_generate_losing_race_to_delete_is_404_and_rolls_back_billing(
    client, db_session, set_chat_model, monkeypatch
) -> None:
    # description:generate vs a concurrent project delete: the project (and its app/code) are
    # read, then the delete lands DURING generation — the realistic window, since the app-load
    # runs first (a project deleted before it just 409s). The flush's description UPDATE then
    # matches zero rows (StaleDataError). The loser must get the same non-leaking 404 a PATCH
    # one second later would — never a 500 — AND the usage row generation billed rides the same
    # rolled-back commit, so nothing is charged for the lost turn (KD-5 billing note).
    import importlib

    from src.services.projects import generate_project_description as real_generate

    # importlib, not `import … as`: the package __init__ re-exports the APIRouter under the
    # same name `router`, which shadows the submodule on attribute-style imports.
    proj_router = importlib.import_module("src.api.v1.projects.router")

    set_chat_model(TestModel(custom_output_text="lost to the race"))
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await AppRegistryFactory.create(
        db_session, user_id=user.id, project_id=project.id, current_code=_CODE
    )

    # Spy on the billing write so the test proves the lost turn actually reached generation
    # (billed via record_usage) BEFORE the commit failed — not that it short-circuited earlier.
    # Bind the real callable from its OWNING module: `describe` only imports it, and reading it
    # back off `describe` is an implicit re-export that mypy --strict rejects. Patch the name
    # where `describe` looks it up, by dotted path.
    from src.services.usage.gate import record_usage as real_record

    billed = {"n": 0}

    async def _spy_record(*args, **kwargs):
        billed["n"] += 1
        return await real_record(*args, **kwargs)

    monkeypatch.setattr("src.services.projects.describe.record_usage", _spy_record)

    async def _generate_then_lose_race(db, model, user_id, *, source, current_description):
        # The concurrent DELETE lands mid-generate (after the app/code were read, before our
        # commit). synchronize_session=False keeps this session's identity map unaware, so the
        # later flush still emits the now-zero-row UPDATE exactly as in production. The real
        # generation still runs (billing included) so the whole turn — description write AND
        # the usage row it billed — rides the one commit that then fails.
        await db.execute(
            delete(Project).where(Project.id == project.id),
            execution_options={"synchronize_session": False},
        )
        return await real_generate(
            db, model, user_id, source=source, current_description=current_description
        )

    monkeypatch.setattr(proj_router, "generate_project_description", _generate_then_lose_race)
    resp = await client.post(f"/v1/projects/{project.id}/description:generate", headers=headers)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Project not found."}}
    # The turn DID bill before losing the race, so record_usage rode the failed commit — the
    # usage write and the description write share one transaction (record_usage never commits
    # on its own; the success side is pinned by test_generation_bills_usage), so the 404 rolls
    # the billing back with it rather than charging for a turn that never landed.
    assert billed["n"] == 1


# --- U8: description injected as shared chat context (R16) ---------------------


async def test_description_injected_into_chat_context(client, db_session, set_chat_model) -> None:
    model, captured = _capturing_stream_model()
    set_chat_model(model)
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(
        db_session, user.id, name="VIP", description="Tracks VIP movements at BIAL."
    )
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
    )

    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(conv.id), "message": {"text": "hi"}},
    )
    assert resp.status_code == 200
    assert "Tracks VIP movements at BIAL." in captured["instructions"]


async def test_null_description_adds_no_project_block(client, db_session, set_chat_model) -> None:
    model, captured = _capturing_stream_model()
    set_chat_model(model)
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)  # NULL description
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
    )

    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(conv.id), "message": {"text": "hi"}},
    )
    assert resp.status_code == 200
    # No PROJECT additions for a null description — the composition is exactly the
    # server-composed base (U7) + the unconditional portal self-description (#6).
    #
    # THE PER-KIND BASE-PROMPT SELECTION IS GONE with the three-valued kind enum this test
    # used to pin against (`PLANNING_SYSTEM_PROMPT`) — every conversation, Plan included,
    # gets `ASSISTANT_IDENTITY_PROMPT` now (toolset gating, not the relay prompt, is what a
    # Plan kind actually restricts).
    assert captured["instructions"] == ASSISTANT_IDENTITY_PROMPT + "\n\n" + PORTAL_SELF_DESCRIPTION


async def test_unknown_conversation_id_is_a_404(client, db_session, set_chat_model) -> None:
    # U7: conversations are created BEFORE the first turn, so an unknown id is a client bug —
    # the old stream-anyway no-op arm is retired with the browser payload.
    model, _captured = _capturing_stream_model()
    set_chat_model(model)
    headers, _ = await _auth(db_session)
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(uuid.uuid4()), "message": {"text": "hi"}},
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Conversation not found."}}


async def test_two_conversations_share_the_same_description(
    client, db_session, set_chat_model
) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(
        db_session, user.id, description="Shared grounding text."
    )
    seen: list[str] = []
    for _ in range(2):
        conv = await ConversationFactory.create(
            db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
        )
        model, captured = _capturing_stream_model()
        set_chat_model(model)
        resp = await client.post(
            "/v1/claude",
            headers=headers,
            json={"conversationId": str(conv.id), "message": {"text": "hi"}},
        )
        assert resp.status_code == 200
        seen.append(captured["instructions"])
    assert all("Shared grounding text." in instructions for instructions in seen)
