"""U5 — BRAIN persists its transcript per step (`entry_kind='step'` rows).

Cadence contract (see `_run_one`): one row per model step — `[the step's request, its
response]` — persisted after that step's tools have executed. A step's tool RETURNS travel in
the NEXT step's request (that is where pydantic-ai's graph appends them), so a crash loses at
most the in-flight step, and the load seam's dangling-call repair covers the orphaned calls.
Concatenated in seq order the rows replay through `load_history`.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.models.message import Message, MessageEntryKind
from src.services.messages.store import load_history
from src.services.orchestrator.harness import BuildOrchestrator, BuildSpec
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.services.orchestrator.fake_sandbox import FakeSandbox
from tests.services.orchestrator.model_harness import scripted_model, text_turn, tool_turn


def _threaded_orchestrator(
    model, billing_factory, conversation_id: uuid.UUID | None
) -> tuple[BuildOrchestrator, uuid.UUID]:
    """A BuildOrchestrator whose run-context provider carries the U5 conversation thread."""
    app_id = uuid.uuid4()

    async def _provider(session_id: uuid.UUID) -> BuildSpec:
        return BuildSpec(
            prompt="build a records app", app_id=app_id, conversation_id=conversation_id
        )

    orchestrator = BuildOrchestrator(
        model=model,
        session_factory=billing_factory,
        run_context_provider=_provider,
        readiness_max_polls=5,
        readiness_poll_s=0.0,
    )
    return orchestrator, app_id


async def _step_rows(db_session, conversation_id) -> list[Message]:
    return list(
        (
            await db_session.execute(
                sa.select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.seq)
            )
        ).scalars()
    )


async def _thread(db_session):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    return user, conversation


async def test_three_step_build_persists_incremental_batches(
    db_session, billing_factory, sink
) -> None:
    """Plan U5 scenario: a 3-model-step FunctionModel build persists 3 incremental batches in
    order — one `[request, response]` pair per step (returns ride in the next step's request)."""
    user, conversation = await _thread(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    model = scripted_model(
        [
            tool_turn("write_file", {"path": "app/page.tsx", "file_text": "export {}\n"}),
            tool_turn("declare_done", {"summary": "done"}),
            text_turn("done"),
        ]
    )
    orchestrator, _ = _threaded_orchestrator(model, billing_factory, conversation.id)
    session_id = uuid.uuid4()

    result = await orchestrator.run_build(session_id, user.id, fake, sink)
    assert result.reason == "completed"

    rows = await _step_rows(db_session, conversation.id)
    assert [row.entry_kind for row in rows] == [MessageEntryKind.STEP] * 3
    assert [row.seq for row in rows] == [0, 1, 2]
    assert [len(row.payload) for row in rows] == [2, 2, 2]
    assert all(row.meta == {"kind": "build_step", "sessionId": str(session_id)} for row in rows)

    # The concatenated rows replay as one coherent native history.
    async def _no_refs(attachment_id: str) -> tuple[str, str]:
        raise AssertionError("no attachments in this build")

    history = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    assert len(history) == 6  # req, resp, returns, resp, returns, resp
    assert isinstance(history[-1], ModelResponse)


async def test_crash_between_steps_keeps_prior_steps_durable(
    db_session, billing_factory, sink
) -> None:
    """Kill the model on step 3: the first two steps are already rows; the build escalates."""
    user, conversation = await _thread(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    calls = {"n": 0}

    def exploding(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return tool_turn("write_file", {"path": "app/a.ts", "file_text": "export {}\n"})
        if calls["n"] == 2:
            return tool_turn("write_file", {"path": "app/b.ts", "file_text": "export {}\n"})
        raise RuntimeError("model fell over mid-build")

    orchestrator, _ = _threaded_orchestrator(
        FunctionModel(exploding), billing_factory, conversation.id
    )

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)
    assert result.status is BuildSessionStatus.FAILED

    rows = await _step_rows(db_session, conversation.id)
    assert len(rows) == 2  # the in-flight third step is the only loss
    assert [len(row.payload) for row in rows] == [2, 2]


async def test_step_write_failure_escalates_transcript_write_failed(
    db_session, billing_factory, sink, monkeypatch
) -> None:
    """The write-before-DONE policy, build edition: a persist failure fails the build loudly
    with its own escalation reason instead of letting workspace and record diverge."""
    user, conversation = await _thread(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True

    async def doomed_append(*args, **kwargs):
        raise RuntimeError("tablespace full")

    monkeypatch.setattr("src.services.orchestrator.harness.append_batch", doomed_append)
    model = scripted_model(
        [tool_turn("write_file", {"path": "app/a.ts", "file_text": "x"}), text_turn()]
    )
    orchestrator, _ = _threaded_orchestrator(model, billing_factory, conversation.id)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)

    assert result.status is BuildSessionStatus.FAILED
    escalations = [e for e in sink.events if e.type == "escalation"]
    assert len(escalations) == 1
    assert escalations[0].reason == "transcript_write_failed"
    assert await _step_rows(db_session, conversation.id) == []


async def test_no_conversation_build_persists_nothing(db_session, billing_factory, sink) -> None:
    """An API-only start (no conversation) still builds; there is simply no thread to write."""
    user = await UserFactory.create(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    model = scripted_model([tool_turn("declare_done", {"summary": "done"}), text_turn("done")])
    orchestrator, _ = _threaded_orchestrator(model, billing_factory, None)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)
    assert result.reason == "completed"

    count = await db_session.scalar(sa.select(sa.func.count()).select_from(Message))
    assert count == 0


async def test_secrets_never_land_in_step_rows(db_session, billing_factory, sink) -> None:
    """Plan U5 integration scenario: a DSN travelling through tool args is stored redacted —
    neither the whole DSN nor its parsed password appears anywhere in the rows."""
    user, conversation = await _thread(db_session)
    fake = FakeSandbox()
    fake.dev_ready = True
    dsn = "postgresql://bialapp_x:sw0rdfish-pa55@db.internal:5432/bialapp_x"
    model = scripted_model(
        [
            tool_turn(
                "write_file",
                {"path": "note.txt", "file_text": f"connection is {dsn} — do not share"},
            ),
            tool_turn("declare_done", {"summary": "wrote the note"}),
            text_turn("done"),
        ]
    )
    orchestrator, _ = _threaded_orchestrator(model, billing_factory, conversation.id)

    result = await orchestrator.run_build(uuid.uuid4(), user.id, fake, sink)
    assert result.reason == "completed"

    rows = await _step_rows(db_session, conversation.id)
    assert rows, "the build must have persisted steps"
    everything = "".join(str(row.payload) + str(row.meta) for row in rows)
    assert dsn not in everything
    assert "sw0rdfish-pa55" not in everything
