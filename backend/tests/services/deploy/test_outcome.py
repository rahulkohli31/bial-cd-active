"""The two rows one failed publish writes, and who reads each.

A dependency failure's citizen sentence names nothing technical on purpose, which leaves the
agent that will be asked to repair it with nothing to repair from — unless the diagnosis rides a
row the model reads and the projection does not. That split is what this file pins: `load_history`
carries no visibility predicate, `load_rows` does, and the difference between them is the whole
guarantee.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

from pydantic_ai.messages import ModelRequest, UserPromptPart

from src.db.models.message import MessageVisibility
from src.services.deploy.images import ImageBuildError
from src.services.deploy.outcome import write_deploy_outcome
from src.services.deploy.service import _DeployFailedError
from src.services.messages.store import load_history, load_rows
from tests.factories import ConversationFactory, UserFactory

_DEPENDENCY_DRIFT_LOG = (
    Path(__file__).parents[1] / "orchestrator" / "fixtures" / "publish_dependency_drift.log"
).read_text()

_COMPILE_FAILURE_LOG = (
    "   ▲ Next.js 16.2.10\n"
    "Failed to compile.\n\n"
    "./app/page.tsx:12:5\n"
    "Type error: Property 'foo' does not exist on type 'Item'.\n"
)


async def _no_rehydration(_refs: Sequence[str]) -> dict[str, tuple[str, str]]:
    """`load_history` requires a rehydrator; nothing written here carries an attachment."""
    return {}


async def _chat(db):
    user = await UserFactory.create(db)
    conversation = await ConversationFactory.create(db, user_id=user.id)
    return user, conversation


async def _publish_failed(db, user, conversation, log: str) -> _DeployFailedError:
    """Settle one failed publish into the chat exactly as the pipeline's failure funnel does."""
    failure = _DeployFailedError.from_build(
        ImageBuildError("the image build failed", log_tail=log)
    )
    await write_deploy_outcome(
        db,
        user_id=user.id,
        conversation_id=conversation.id,
        deployment_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        succeeded=False,
        message=failure.citizen_message,
        detail=failure.detail,
        model_detail=failure.model_detail,
    )
    return failure


def _model_text(history) -> str:
    """Every string the model would read, whichever slot it arrived in."""
    return "\n".join(
        part.content
        for message in history
        for part in message.parts
        if isinstance(getattr(part, "content", None), str)
    )


async def test_the_model_is_handed_the_builders_own_diagnosis(db_session) -> None:
    """What the repair run reads. The sentence the citizen got names no fault by design, so
    without this row the agent is asked to fix a build it was told nothing about."""
    user, conversation = await _chat(db_session)
    await _publish_failed(db_session, user, conversation, _DEPENDENCY_DRIFT_LOG)

    history = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_rehydration
    )
    text = _model_text(history)

    assert "does not satisfy" in text, f"the model's history carries no diagnosis: {text!r}"
    assert "@azure/identity" in text
    # It reaches the model in the slot the platform hands over evidence in, not as the
    # assistant's own words.
    assert any(
        isinstance(message, ModelRequest)
        and any(
            isinstance(part, UserPromptPart)
            and isinstance(part.content, str)
            and "does not satisfy" in part.content
            for part in message.parts
        )
        for message in history
    )


async def test_the_citizen_is_shown_no_diagnosis_on_the_publish_that_withheld_it(
    db_session,
) -> None:
    """The register guarantee, asserted on the read the portal actually makes. A row written
    visible instead of hidden reaches the screen, where the package name and the two version
    numbers are exactly what the written sentence exists to keep out."""
    user, conversation = await _chat(db_session)
    failure = await _publish_failed(db_session, user, conversation, _DEPENDENCY_DRIFT_LOG)

    visible = await load_rows(db_session, user_id=user.id, conversation_id=conversation.id)
    shown = "\n".join(
        part.get("content", "")
        for row in visible
        for message in row.payload
        for part in message.get("parts", [])
        if isinstance(part.get("content"), str)
    )

    assert failure.citizen_message in shown
    assert "does not satisfy" not in shown, shown
    assert "@azure" not in shown, shown
    assert all(row.visibility is MessageVisibility.VISIBLE for row in visible)


async def test_a_secret_in_the_builders_output_never_reaches_the_model_either(db_session) -> None:
    """The diagnosis egresses to a model holding a shell, which is a harder boundary than the
    deployment row's detail — so it is redacted before it is stored, not on the way out."""
    user, conversation = await _chat(db_session)
    leaky = _DEPENDENCY_DRIFT_LOG.replace(
        "npm error Invalid: lock file's @azure/identity@4.11.1",
        "npm error Invalid: DB_PASSWORD=hunter2 lock file's @azure/identity@4.11.1",
        1,
    )
    await _publish_failed(db_session, user, conversation, leaky)

    history = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_rehydration
    )
    text = _model_text(history)

    assert "hunter2" not in text, text
    # The diagnosis still reads — redaction masked the value, not the line.
    assert "does not satisfy" in text


async def test_a_failure_whose_sentence_names_the_fault_writes_only_one_row(db_session) -> None:
    """No second row for the classes the citizen sentence already covers — the same diagnostic
    twice in one history is tokens spent to say nothing new."""
    user, conversation = await _chat(db_session)
    failure = await _publish_failed(db_session, user, conversation, _COMPILE_FAILURE_LOG)

    assert failure.model_detail is None
    assert "Type error:" in failure.citizen_message
    every = await load_rows(
        db_session, user_id=user.id, conversation_id=conversation.id, include_hidden=True
    )
    assert len(every) == 1, [row.meta for row in every]


async def test_a_diagnosis_is_not_filed_against_a_failure_the_citizen_was_never_shown(
    db_session, monkeypatch
) -> None:
    """The hidden row opens by telling the model the failure "was reported to the person in
    plain words that name nothing technical". When the visible row never landed, that sentence
    is false — the model would repair from a diagnosis whose paired message does not exist.

    A retry is the other case and keeps the diagnosis: there the message really is on record
    from the first pass, which is why "already written" and "failed" cannot share one answer."""
    user, conversation = await _chat(db_session)
    deployment_id, app_id = uuid.uuid4(), uuid.uuid4()
    failure = _DeployFailedError.from_build(
        ImageBuildError("the image build failed", log_tail=_DEPENDENCY_DRIFT_LOG)
    )
    attempts = 0

    async def the_visible_row_never_lands(*args: object, **kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("the chat write went down")

    monkeypatch.setattr("src.services.deploy.outcome.append_batch", the_visible_row_never_lands)

    wrote = await write_deploy_outcome(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        deployment_id=deployment_id,
        app_id=app_id,
        succeeded=False,
        message=failure.citizen_message,
        detail=failure.detail,
        model_detail=failure.model_detail,
    )

    assert wrote is False
    # LIVENESS: the visible write really was attempted. Without this, "one attempt" could mean
    # the whole function returned early and the test would pass on a path it never exercised.
    assert attempts == 1, "the diagnostic row was written anyway"

    every = await load_rows(
        db_session, user_id=user.id, conversation_id=conversation.id, include_hidden=True
    )
    assert every == []


async def test_a_reconciler_racing_the_pipeline_double_writes_neither_row(db_session) -> None:
    """Both rows are guarded on their own marker, so a retry of the whole write adds nothing."""
    user, conversation = await _chat(db_session)
    deployment_id, app_id = uuid.uuid4(), uuid.uuid4()
    failure = _DeployFailedError.from_build(
        ImageBuildError("the image build failed", log_tail=_DEPENDENCY_DRIFT_LOG)
    )
    for _ in range(2):
        await write_deploy_outcome(
            db_session,
            user_id=user.id,
            conversation_id=conversation.id,
            deployment_id=deployment_id,
            app_id=app_id,
            succeeded=False,
            message=failure.citizen_message,
            detail=failure.detail,
            model_detail=failure.model_detail,
        )

    every = await load_rows(
        db_session, user_id=user.id, conversation_id=conversation.id, include_hidden=True
    )
    # A `None` meta raises here rather than reading as an absent row, which is the point.
    assert [(row.meta or {})["kind"] for row in every] == ["deploy_outcome", "deploy_diagnostic"]
