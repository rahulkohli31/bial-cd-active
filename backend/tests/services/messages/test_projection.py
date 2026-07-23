"""U6 — the one history→display derivation (`services/messages/projection.py`).

Rows are written through the REAL producers/store (`append_batch`, `write_build_started`,
`write_build_outcome`, `append_mode_switch_marker`) in the exact shapes U5 pinned
(`test_transcript_steps.py` / `test_producers.py`), so these tests break when the producer
contract drifts — which is the point. The golden build test doubles as U10's parity fixture:
the live stream must render THIS list for THIS transcript.
"""

from __future__ import annotations

import uuid

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.models.conversation import ConversationMode
from src.db.models.message import MessageEntryKind, MessageVisibility
from src.services.build_sessions.outcome import write_build_outcome, write_build_started
from src.services.messages.projection import (
    AssistantTextItem,
    BannerItem,
    BuildInProgressItem,
    PlanOptionsItem,
    StepItem,
    UserTextItem,
    project_rows,
)
from src.services.messages.store import (
    append_batch,
    append_mode_switch_marker,
    load_rows,
)
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

PREVIEW = "https://sbx-abc.westeurope.azurecontainerapps.io/"


async def _thread(db_session):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    return user, project, conversation


async def _step(db_session, user, conversation, session_id, messages) -> None:
    """One BRAIN step row, exactly as the U5 harness persists it."""
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=messages,
        entry_kind=MessageEntryKind.STEP,
        mode=ConversationMode.WRITE,
        meta={"kind": "build_step", "sessionId": str(session_id)},
    )


async def _rows(db_session, user, conversation):
    return await load_rows(
        db_session, user_id=user.id, conversation_id=conversation.id, include_hidden=True
    )


# --- the golden build (U10's parity fixture) ----------------------------------


async def test_finished_build_projects_the_golden_item_list(db_session) -> None:
    """A full build session → the exact friendly item list. U10's catch-up snapshot must
    reproduce THIS list for THIS transcript (live == reload, R8)."""
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()

    await write_build_started(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=session_id,
        started_seq=-1,
    )
    await _step(
        db_session,
        user,
        conversation,
        session_id,
        [
            ModelRequest(parts=[UserPromptPart(content="build me a visitor log")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": "app/page.tsx", "file_text": "export {}\n"},
                        tool_call_id="call-1",
                    )
                ]
            ),
        ],
    )
    await _step(
        db_session,
        user,
        conversation,
        session_id,
        [
            ModelRequest(
                parts=[ToolReturnPart(tool_name="write_file", content="ok", tool_call_id="call-1")]
            ),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="run_command",
                        args={"command": ["npm", "install", "zod"]},
                        tool_call_id="call-2",
                    )
                ]
            ),
        ],
    )
    await _step(
        db_session,
        user,
        conversation,
        session_id,
        [
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="run_command", content="added 1 package", tool_call_id="call-2"
                    )
                ]
            ),
            ModelResponse(parts=[TextPart(content="All done — the visitor log is live.")]),
        ],
    )
    await write_build_outcome(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=session_id,
        status=BuildSessionStatus.ENDED,
        preview_url=PREVIEW,
        snapshot_committed=True,
        reason="completed",
        started_seq=-1,
    )

    items = project_rows(await _rows(db_session, user, conversation))

    golden = [
        ("user_text", "build me a visitor log"),
        ("step", "Updated app/page.tsx"),
        ("step", "Installing packages"),
        ("assistant_text", "All done — the visitor log is live."),
        ("banner", "Build finished."),
    ]

    def _headline(item: object) -> str:
        if isinstance(item, StepItem):
            return item.label
        assert isinstance(item, (UserTextItem, AssistantTextItem, BannerItem))  # fmt: skip
        return item.text

    flattened = [(item.type, _headline(item)) for item in items]
    assert flattened == golden

    # The closed session anchors nothing; steps carry state + details for the expander.
    assert not any(isinstance(item, BuildInProgressItem) for item in items)
    steps = [item for item in items if isinstance(item, StepItem)]
    assert [step.state for step in steps] == ["ok", "ok"]
    assert steps[0].detail.args is not None and "app/page.tsx" in steps[0].detail.args
    assert steps[1].detail.result == "added 1 package"
    banner = next(item for item in items if isinstance(item, BannerItem))
    assert banner.banner == "completed"
    assert banner.preview_url == PREVIEW
    assert banner.session_id == str(session_id)


async def test_repair_nudges_never_render_as_user_bubbles(db_session) -> None:
    """Only a session's FIRST step row carries the user's own instruction; later step-row
    prompts are the harness's repair/continue nudges and must not be put in the user's
    mouth."""
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    await _step(
        db_session,
        user,
        conversation,
        session_id,
        [
            ModelRequest(parts=[UserPromptPart(content="build me a form")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": "app/a.ts", "file_text": "x"},
                        tool_call_id="c1",
                    )
                ]
            ),
        ],
    )
    await _step(
        db_session,
        user,
        conversation,
        session_id,
        [
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="write_file", content="ok", tool_call_id="c1"),
                    UserPromptPart(content="The build is not green yet — fix the type error."),
                ]
            ),
            ModelResponse(parts=[TextPart(content="Fixed.")]),
        ],
    )

    items = project_rows(await _rows(db_session, user, conversation))
    user_bubbles = [item for item in items if isinstance(item, UserTextItem)]
    assert [bubble.text for bubble in user_bubbles] == ["build me a form"]


# --- visibility ----------------------------------------------------------------


async def test_reads_are_hidden_steps_with_detail(db_session) -> None:
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    await _step(
        db_session,
        user,
        conversation,
        session_id,
        [
            ModelRequest(parts=[UserPromptPart(content="what's in the app?")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="read_file", args={"path": "app/page.tsx"}, tool_call_id="r1"
                    ),
                    ToolCallPart(
                        tool_name="run_command",
                        args={"command": ["grep", "-rn", "visitors", "app/"]},
                        tool_call_id="r2",
                    ),
                ]
            ),
        ],
    )
    await _step(
        db_session,
        user,
        conversation,
        session_id,
        [
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="read_file", content="export {}", tool_call_id="r1"),
                    ToolReturnPart(
                        tool_name="run_command", content="app/db.ts:3: visitors", tool_call_id="r2"
                    ),
                ]
            ),
            ModelResponse(parts=[TextPart(content="It tracks visitors.")]),
        ],
    )

    items = project_rows(await _rows(db_session, user, conversation))
    steps = [item for item in items if isinstance(item, StepItem)]
    assert [step.hidden for step in steps] == [True, True]
    assert steps[0].label == "Read app/page.tsx"
    assert steps[1].label == "Inspected the app's files"
    assert steps[1].detail.result == "app/db.ts:3: visitors"


async def test_hidden_rows_render_nothing_but_stay_auditable(db_session) -> None:
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    await append_mode_switch_marker(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        old_mode=ConversationMode.PLAN,
        new_mode=ConversationMode.WRITE,
    )
    await write_build_started(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=session_id,
        started_seq=-1,
    )
    await write_build_outcome(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=session_id,
        status=BuildSessionStatus.ENDED,
        preview_url=None,
        snapshot_committed=True,
        reason="completed",
    )

    rows = await _rows(db_session, user, conversation)
    items = project_rows(rows)
    # The marker and the (closed) started row render nothing; only the outcome banner shows.
    assert [item.type for item in items] == ["banner"]
    # …but the audit read still has all three rows.
    assert len(rows) == 3
    hidden = [row for row in rows if row.visibility is MessageVisibility.HIDDEN]
    assert len(hidden) == 2


# --- lifecycle -----------------------------------------------------------------


async def test_unclosed_build_started_projects_an_in_progress_anchor(db_session) -> None:
    """Crash/mid-build reload (R8): a started-but-never-closed session must anchor a truthful
    'a build was running here' item — not vanish."""
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    await write_build_started(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=session_id,
        started_seq=-1,
    )

    items = project_rows(await _rows(db_session, user, conversation))
    assert [item.type for item in items] == ["build_in_progress"]
    anchor = items[0]
    assert isinstance(anchor, BuildInProgressItem)
    assert anchor.session_id == str(session_id)


async def test_failed_build_projects_a_failure_banner(db_session) -> None:
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    await write_build_outcome(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=session_id,
        status=BuildSessionStatus.FAILED,
        preview_url=None,
        snapshot_committed=True,
        reason="build_failed",
    )

    items = project_rows(await _rows(db_session, user, conversation))
    assert len(items) == 1
    banner = items[0]
    assert isinstance(banner, BannerItem)
    assert banner.banner == "failed"
    assert banner.text == "The build failed: build_failed"
    assert banner.preview_url is None


# --- tool-failure + plan options -----------------------------------------------


async def test_retry_refusal_projects_a_failed_step(db_session) -> None:
    """A guard refusal (`ModelRetry` → RetryPromptPart) is a FAILED step — the walkthrough's
    blocked `DELETE FROM visitors` must render as blocked, not as quietly ok."""
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    await _step(
        db_session,
        user,
        conversation,
        session_id,
        [
            ModelRequest(parts=[UserPromptPart(content="clean up the test rows")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="run_command",
                        args={"command": ["psql", "-c", "DELETE FROM visitors"]},
                        tool_call_id="bad-1",
                    )
                ]
            ),
        ],
    )
    await _step(
        db_session,
        user,
        conversation,
        session_id,
        [
            ModelRequest(
                parts=[
                    RetryPromptPart(
                        content="This command was blocked: destructive SQL.",
                        tool_name="run_command",
                        tool_call_id="bad-1",
                    )
                ]
            ),
            ModelResponse(parts=[TextPart(content="Understood — verifying differently.")]),
        ],
    )

    items = project_rows(await _rows(db_session, user, conversation))
    steps = [item for item in items if isinstance(item, StepItem)]
    assert len(steps) == 1
    assert steps[0].state == "failed"
    assert steps[0].detail.result is not None and "blocked" in steps[0].detail.result


async def test_plan_options_three_states(db_session) -> None:
    """U11's stored resolutions: refine / build_failed re-arm, missing return = pending. The
    projection derives the card state purely from the stored call/return pair."""
    user, _, conversation = await _thread(db_session)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="plan a visitor log")]),
            ModelResponse(
                parts=[
                    TextPart(content="Here is the plan…"),
                    ToolCallPart(tool_name="present_plan_options", args={}, tool_call_id="opt-1"),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="present_plan_options", content="refine", tool_call_id="opt-1"
                    )
                ]
            ),
            ModelResponse(
                parts=[
                    TextPart(content="Refined plan…"),
                    ToolCallPart(tool_name="present_plan_options", args={}, tool_call_id="opt-2"),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="present_plan_options",
                        content="build_failed:lock_held",
                        tool_call_id="opt-2",
                    )
                ]
            ),
            ModelResponse(
                parts=[
                    TextPart(content="Trying again…"),
                    ToolCallPart(tool_name="present_plan_options", args={}, tool_call_id="opt-3"),
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        mode=ConversationMode.PLAN,
    )

    items = project_rows(await _rows(db_session, user, conversation))
    cards = [item for item in items if isinstance(item, PlanOptionsItem)]
    assert [(card.tool_call_id, card.state) for card in cards] == [
        ("opt-1", "refine"),
        ("opt-2", "build_failed"),
        ("opt-3", "pending"),
    ]
    assert cards[1].reason == "lock_held"
