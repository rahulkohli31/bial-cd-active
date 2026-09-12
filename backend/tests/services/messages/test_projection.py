"""The one history→display derivation (`services/messages/projection.py`).

Rows are written through the REAL producers/store (`append_batch`, `write_build_outcome`) in the
exact shapes pinned by `test_producers.py`, so these tests break
when the producer contract drifts — which is the point. The golden build test doubles as the
parity fixture: the live stream must render THIS list for THIS transcript.

The one exception is `build_started`: its production writer is DELETED with the build-start path,
but rows it already wrote are permanent in production transcripts and the projection's
`BuildInProgressItem` / `_closed_sessions` arms still read them. Those rows come from
`tests.fakes.write_legacy_build_started`, which is byte-identical to the writer that made them.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import get_args

import sqlalchemy as sa
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from sqlalchemy import event

from src.api.v1.build_sessions.schemas import BuildSessionStatus, ErrorSource
from src.api.v1.conversations.schemas import DiagnosticFrame
from src.db.models.attachment import Attachment
from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.build_sessions.outcome import write_build_outcome
from src.services.media.lanes import EXCEL_MEDIA_TYPE
from src.services.media.magic import chip_kind_for
from src.services.messages.projection import (
    PROPOSE_SLICE_TOOL,
    TELL_THE_USER_TOOL,
    TURN_TERMINAL_KIND,
    AssistantTextItem,
    BannerItem,
    BuildInProgressItem,
    DisplayItem,
    PlanOptionsItem,
    StepItem,
    TurnTerminalItem,
    UserTextItem,
    _friendly_area,
    _user_text_and_refs,
    classify_command,
    classify_file_step,
    classify_tool_call,
    command_only_inspects,
    project_conversation,
    project_rows,
)
from src.services.messages.store import (
    SCHEMA_VERSION,
    append_batch,
    dump_for_row,
    load_history,
    load_rows,
)
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import write_legacy_build_started

PREVIEW = "https://sbx-abc.westeurope.azurecontainerapps.io/"
# Matches `test_store_roundtrip.py`'s fixture — a real PNG magic prefix, so the store's own
# byte checks see what they expect.
_PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"fake-png-body"


async def _thread(db_session):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    return user, project, conversation


async def _step(db_session, user, conversation, session_id, messages) -> None:
    """One build-step row, exactly as the harness persists it."""
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=messages,
        entry_kind=MessageEntryKind.STEP,
        kind=ChatKind.BUILD,
        meta={"kind": "build_step", "sessionId": str(session_id)},
    )


async def _rows(db_session, user, conversation):
    return await load_rows(
        db_session, user_id=user.id, conversation_id=conversation.id, include_hidden=True
    )


# --- the golden build (the parity fixture) -------------------------------------


async def test_finished_build_projects_the_golden_item_list(db_session) -> None:
    """A full build session → the exact friendly item list. The catch-up snapshot must
    reproduce THIS list for THIS transcript (live == reload)."""
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()

    await write_legacy_build_started(
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
        # friendly AREA, never the raw path; friendly command copy, never the argv.
        ("step", "Building your app's main page"),
        ("step", "Setting up the tools your app needs"),
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

    # The closed session anchors nothing; a step carries a friendly label and a state, and
    # that is the whole of what it carries — the expander material it used to ship beside them
    # is gone from the wire (the field-set guard at the bottom of this file is where that is
    # pinned).
    assert not any(isinstance(item, BuildInProgressItem) for item in items)
    steps = [item for item in items if isinstance(item, StepItem)]
    assert [step.state for step in steps] == ["ok", "ok"]
    assert "app/page.tsx" not in steps[0].label
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


def _rendered(item: StepItem) -> str:
    """Everything a step actually puts on the wire, as one string.

    Asserted over the WHOLE serialised item rather than a named field, because "the result is
    not in `detail`" is a claim about a field and the claim worth making is about the item: a
    future field carrying the same payload under a new name has to fail these too."""
    return json.dumps(item.model_dump(mode="json"), ensure_ascii=False)


async def test_reads_are_visible_steps_that_say_only_what_they_touched(db_session) -> None:
    """`hidden` marks plumbing only: a write to a configuration file, and a housekeeping shell
    command. The whole read class is drawn, because marking every read hidden bought a build
    whose activity opened on a write with no account of what the agent had looked at to get
    there. That RAISES the stakes on the second half — a `grep` over the citizen's own app
    returns the citizen's own data, and the only thing keeping it off the screen is its absence
    from the item."""
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
    assert [step.hidden for step in steps] == [False, False]
    # THIS ASSERTION USED TO PIN THE LEAK. It read `== "Read app/page.tsx"`, which made
    # the raw path the EXPECTED output of a helper whose two neighbours exist to guarantee the
    # opposite. Flipped, not deleted: the absence is asserted, and paired with the liveness half
    # (a real friendly area still renders) so a read arm that started returning "" would not
    # pass by rendering nothing at all.
    assert "app/page.tsx" not in steps[0].label
    assert steps[0].label == "Looking at your app's main page"
    assert steps[1].label == "Inspected the app's files"
    # AND NEITHER STEP CARRIES WHAT THE READ RETURNED. A raw result like
    # `"app/db.ts:3: visitors"` is grep output over the citizen's own data, and this step is
    # one the citizen can see — `grep -rn` over an app is exactly the call whose result is
    # worth the least to a reader and the most to anyone else.
    assert "visitors" not in _rendered(steps[1])
    assert "app/db.ts" not in _rendered(steps[1])


async def test_a_turn_that_reads_three_files_then_writes_one_shows_four_steps(db_session) -> None:
    """Three reads and a write are four things the agent did, and the citizen sees four rows.

    THE FLAGS ARE THE CLAIM, not the item count: the projection always emitted four items, and a
    feed that draws only the visible ones is what turned that into a single row. So the flags are
    asserted as a list, in order, beside the labels that make each row worth drawing.
    Mutation-check: make `_step_label`'s read arm return `hidden=True` again and this goes red on
    the three reads while the write's own flag does not move."""
    user, _, conversation = await _thread(db_session)
    await _step(
        db_session,
        user,
        conversation,
        uuid.uuid4(),
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="read_file", args={"path": "app/page.tsx"}, tool_call_id="r1"
                    ),
                    ToolCallPart(
                        tool_name="read_file", args={"path": "app/layout.tsx"}, tool_call_id="r2"
                    ),
                    ToolCallPart(
                        tool_name="read_file", args={"path": "db/schema.ts"}, tool_call_id="r3"
                    ),
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": "app/dashboard/page.tsx", "file_text": "export {}\n"},
                        tool_call_id="w1",
                    ),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="read_file", content="export {}", tool_call_id="r1"),
                    ToolReturnPart(tool_name="read_file", content="export {}", tool_call_id="r2"),
                    ToolReturnPart(tool_name="read_file", content="export {}", tool_call_id="r3"),
                    ToolReturnPart(tool_name="write_file", content="ok", tool_call_id="w1"),
                ]
            ),
        ],
    )

    items = project_rows(await _rows(db_session, user, conversation))
    assert [item.type for item in items] == ["step", "step", "step", "step"]
    steps = [item for item in items if isinstance(item, StepItem)]
    assert [step.hidden for step in steps] == [False, False, False, False]
    # LIVENESS, AND THE FRIENDLY HALF IN ONE: four rows that all said nothing would satisfy the
    # flag assertion above, so each one is required to name the area it touched — and none of
    # them names the file, which is what `_friendly_area` is for.
    assert [step.label for step in steps] == [
        "Looking at your app's main page",
        "Looking at your app's overall look",
        "Looking at where your app stores information",
        "Building the dashboard page",
    ]
    assert [step.state for step in steps] == ["ok", "ok", "ok", "ok"]


async def test_a_configuration_write_and_a_housekeeping_command_stay_hidden(db_session) -> None:
    """The other half of `test_reads_are_visible_steps_that_say_only_what_they_touched`,
    and the reason it is a NARROWING rather than a deletion.

    A write to `package.json` and a `mkdir` are plumbing between the steps that matter. Both
    still render as ITEMS — the flag says "do not draw this", not "forget this happened" — so the
    audit read and the step's own state survive.

    The read beside them is the liveness half and the discriminator at once: a projection that
    flagged everything hidden would satisfy the two assertions this test is named for."""
    user, _, conversation = await _thread(db_session)
    await _step(
        db_session,
        user,
        conversation,
        uuid.uuid4(),
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="read_file", args={"path": "app/page.tsx"}, tool_call_id="r1"
                    ),
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": "package.json", "file_text": "{}"},
                        tool_call_id="c1",
                    ),
                    ToolCallPart(
                        tool_name="run_command",
                        args={"command": ["mkdir", "-p", "app/lib"]},
                        tool_call_id="h1",
                    ),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="read_file", content="export {}", tool_call_id="r1"),
                    ToolReturnPart(tool_name="write_file", content="ok", tool_call_id="c1"),
                    ToolReturnPart(tool_name="run_command", content="", tool_call_id="h1"),
                ]
            ),
        ],
    )

    items = project_rows(await _rows(db_session, user, conversation))
    steps = [item for item in items if isinstance(item, StepItem)]
    assert [(step.tool, step.hidden) for step in steps] == [
        ("read_file", False),
        ("write_file", True),
        ("run_command", True),
    ]
    assert [step.state for step in steps] == ["ok", "ok", "ok"]


async def test_a_housekeeping_command_that_failed_is_never_hidden(db_session) -> None:
    """★ The rule that nothing is hidden when something went wrong, whatever class it belongs to.

    A group opens itself saying one thing went wrong and then counts the rows the citizen can
    see; a hidden failure makes that count name a row nobody can find. The identical command that
    SUCCEEDED sits beside it, so this pins the conditional rather than a projection that simply
    stopped hiding housekeeping altogether.
    Mutation-check: drop the `and state != "failed"` conjunct where the step item is built and
    the failed row comes back hidden while its successful twin does not move."""
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    await _step(
        db_session,
        user,
        conversation,
        session_id,
        [
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="run_command",
                        args={"command": ["mkdir", "-p", "app/lib"]},
                        tool_call_id="ok-1",
                    ),
                    ToolCallPart(
                        tool_name="run_command",
                        args={"command": ["mv", "app/a.ts", "app/b.ts"]},
                        tool_call_id="bad-1",
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
                    ToolReturnPart(tool_name="run_command", content="", tool_call_id="ok-1"),
                    RetryPromptPart(
                        content="No such file: app/a.ts",
                        tool_name="run_command",
                        tool_call_id="bad-1",
                    ),
                ]
            ),
            ModelResponse(parts=[TextPart(content="That file is not there — let me look again.")]),
        ],
    )

    items = project_rows(await _rows(db_session, user, conversation))
    steps = [item for item in items if isinstance(item, StepItem)]
    assert [(step.state, step.hidden) for step in steps] == [("ok", True), ("failed", False)]
    # THE COUNT A GROUP ANNOUNCES IS A COUNT OF ROWS THE CITIZEN CAN OPEN, stated as the property
    # rather than as this fixture's arithmetic: nowhere in the projection is a failure hidden.
    assert not [step for step in steps if step.hidden and step.state == "failed"]
    # …and the failed row still says nothing about what went wrong. The retry body is the
    # harness explaining a refusal to the model; the citizen gets the state and the friendly
    # label, exactly as they do for a refusal the guard raised.
    failed = steps[1]
    assert failed.label == "Organized the app's files"
    assert "app/a.ts" not in _rendered(failed)


async def test_a_plan_turn_that_only_reads_has_a_non_empty_activity_group(db_session) -> None:
    """A planning turn spends itself reading. Every call below was flagged hidden while `hidden`
    meant "a read", so the whole activity group came back empty and the chat drew a turn that
    appeared to have done nothing while the agent was reading the app to answer.

    `entry_kind=TURN` with the Plan kind because that is the only shape the product can
    actually produce for a Plan chat; the STEP rows above belong to the Build loop."""
    user, _, conversation = await _thread(db_session)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="what would a visitor log take?")]),
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="list_files", args={"path": "app"}, tool_call_id="p1"),
                    ToolCallPart(
                        tool_name="read_file", args={"path": "app/page.tsx"}, tool_call_id="p2"
                    ),
                    ToolCallPart(
                        tool_name="run_command",
                        args={"command": ["cat", "app/layout.tsx"]},
                        tool_call_id="p3",
                    ),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="list_files", content="page.tsx", tool_call_id="p1"),
                    ToolReturnPart(tool_name="read_file", content="export {}", tool_call_id="p2"),
                    ToolReturnPart(
                        tool_name="run_command", content="export {}", tool_call_id="p3"
                    ),
                ]
            ),
            ModelResponse(
                parts=[TextPart(content="Three steps, and you already have the shell.")]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )

    items = project_rows(await _rows(db_session, user, conversation))
    steps = [item for item in items if isinstance(item, StepItem)]
    assert len(steps) == 3
    assert [step.hidden for step in steps] == [False, False, False]
    # LIVENESS on the prose either side of the group, so "three visible steps" is a claim about
    # a turn that rendered rather than about a projection that emitted steps and nothing else.
    assert [item.text for item in items if isinstance(item, UserTextItem)] == [
        "what would a visitor log take?"
    ]
    assert [item.text for item in items if isinstance(item, AssistantTextItem)] == [
        "Three steps, and you already have the shell."
    ]


async def test_hidden_rows_render_nothing_but_stay_auditable(db_session) -> None:
    # The mode-switch marker used to be the third hidden row here. It is gone with the switch
    # that wrote it (`tests/api/v1/conversations/test_mode_switch.py` is its inertness guard).
    # The `build_started` overlay carries the same property; nothing writes one any more either,
    # but rows already in production transcripts must still render as nothing and audit as one.
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    await write_legacy_build_started(
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
    # The (closed) started row renders nothing; only the outcome banner shows.
    assert [item.type for item in items] == ["banner"]
    # …but the audit read still has it: two rows, one of them hidden.
    assert len(rows) == 2
    hidden = [row for row in rows if row.visibility is MessageVisibility.HIDDEN]
    assert len(hidden) == 1


# --- lifecycle -----------------------------------------------------------------


async def test_unclosed_build_started_projects_an_in_progress_anchor(db_session) -> None:
    """A crash or a mid-build reload: a started-but-never-closed session must anchor a truthful
    'a build was running here' item — not vanish."""
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    await write_legacy_build_started(
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
    # FAILED, AND THAT IS ALL IT SAYS. The refusal text ("blocked …") never rides the step's
    # detail block; a retry-prompt body is the harness talking to the model about why a call was
    # refused, which is neither the citizen's business nor safe to assume it is sanitised.
    assert steps[0].state == "failed"
    assert "blocked" not in _rendered(steps[0])


async def test_plan_options_states_have_no_third_member(db_session) -> None:
    """`build_failed` is retired without a caller, so a resolution recorded before its
    retirement must not read back as a still-actionable third state. `_plan_options_state`'s
    catch-all reads ANYTHING it does not recognise — including a stray `build_failed:<reason>`
    overlay — as `refine`: the build behind it never happened, and the card is spent. The fixture
    is deliberately the PRE-MIGRATION shape, a prose message ahead of an empty-argument tool
    call.

    Mutation-check: revert the catch-all to `"build_failed"` and this goes red on opt-2's state,
    without any other test in this file moving."""
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
                        # Written by the retired recorder, before build_failed was retired —
                        # must still be a readable row on disk, and must not come back as a
                        # button with nothing behind it.
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
        kind=ChatKind.PLAN,
    )

    items = project_rows(await _rows(db_session, user, conversation))
    cards = [item for item in items if isinstance(item, PlanOptionsItem)]
    assert [(card.tool_call_id, card.state) for card in cards] == [
        ("opt-1", "refine"),
        ("opt-2", "refine"),  # SPENT, not re-armed — there is no re-arming any more
        ("opt-3", "pending"),
    ]
    # AND NO CARD CARRIES A REASON — the field is gone, not merely unset. `reason` existed to
    # name WHY a build could not start, on a card the failure had burned; nothing burns a card
    # any more, so the only value it could ever hold came from the retired state. It was left
    # behind as a field that no producer wrote and every response shipped as `null`.
    assert "reason" not in PlanOptionsItem.model_fields

    # INERTNESS GUARD: the type itself cannot produce the retired member, so a future
    # regression that reintroduces build_failed fails here even before a row is written.
    assert get_args(PlanOptionsItem.model_fields["state"].annotation) == (
        "pending",
        "refine",
        "build",
    )


async def test_the_plan_renders_from_the_offers_own_stored_call_args(db_session) -> None:
    """The offer's stored `args` is the single authoritative copy of the plan. The
    projection reads it out and renders it as an ordinary assistant message immediately
    ahead of the card, so live and reload show the same text from the same single copy
    rather than two writers agreeing to say the same thing.

    Mutation-check: stop reading `_plan_argument` in `_project_response_parts` and this goes
    red on the missing `AssistantTextItem` while the card itself still renders."""
    user, _, conversation = await _thread(db_session)
    plan = "1. Add a visitors table.\n2. Wire the intake form.\n3. Ship it."
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="present_plan_options",
                        args={"plan": plan},
                        tool_call_id="opt-1",
                    )
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )

    items = project_rows(await _rows(db_session, user, conversation))
    # Exactly two items — the plan's own text, immediately followed by its card. Nothing
    # rides in between, and nothing rides twice: there is no SECOND durable copy of the plan
    # anywhere (not in the tool call's rendered detail, not as a separate row).
    assert len(items) == 2
    text_item, card = items
    assert isinstance(text_item, AssistantTextItem) and text_item.text == plan
    assert isinstance(card, PlanOptionsItem) and card.tool_call_id == "opt-1"
    assert card.state == "pending"
    # SAME ROW: live and reload agree on more than just content — they agree on which row.
    assert text_item.seq == card.seq


async def test_a_call_with_no_plan_renders_no_text_and_still_renders_its_card(db_session) -> None:
    """Every card presented before the plan-args migration is exactly this shape: the tool call
    carries no argument at all. It must not render a phantom text item, and it must not lose
    its card either — revision 0035 resolved these rows rather than deleting them."""
    user, _, conversation = await _thread(db_session)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="present_plan_options", args={}, tool_call_id="opt-1")
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )

    items = project_rows(await _rows(db_session, user, conversation))
    assert not any(isinstance(item, AssistantTextItem) for item in items)
    cards = [item for item in items if isinstance(item, PlanOptionsItem)]
    assert len(cards) == 1 and cards[0].tool_call_id == "opt-1" and cards[0].state == "pending"


# --- the friendly classifier (one source of truth, live == reload) -------------


def test_classify_command_maps_the_pinned_commands() -> None:
    """This is the SAME translator the live emitter (`tools.py`) calls."""
    assert classify_command(["npm", "install", "zod"]) == (
        "Setting up the tools your app needs",
        False,
    )
    assert (
        classify_command(["pnpm", "add", "drizzle-orm"])[0]
        == "Setting up the tools your app needs"
    )
    # drizzle generate is DATA-SETUP; migrate is DATA-READY — distinct citizen copy.
    assert classify_command(["npx", "drizzle-kit", "generate"]) == (
        "Setting up where your app stores information",
        False,
    )
    assert classify_command(["npm", "run", "db:migrate"])[0] == "Getting your app's data ready"
    assert classify_command(["node", "db-migrate.mjs"])[0] == "Getting your app's data ready"
    assert classify_command(["tsc", "--noEmit"])[0] == "Making sure everything fits together"
    assert classify_command(["npm", "run", "build"])[0] == "Making sure everything fits together"
    assert classify_command(["npm", "run", "lint"])[0] == "Tidying things up"


def test_classify_command_shows_reads_and_hides_only_housekeeping() -> None:
    """The LABEL is asserted beside the hidden flag on both groups, so a classifier that
    returned an empty string could not satisfy the flags alone."""
    for read_only in (["ls", "-la"], ["grep", "-rn", "x", "app/"], ["cat", "app/page.tsx"]):
        label, hidden = classify_command(read_only)
        assert hidden is False
        assert label == "Inspected the app's files"
    for housekeeping in (["mkdir", "-p", "app/lib"], ["mv", "a", "b"], ["touch", "x.ts"]):
        label, hidden = classify_command(housekeeping)
        assert hidden is True
        assert label == "Organized the app's files"


def test_a_code_lane_attachment_still_has_a_chip_after_reload() -> None:
    """★ R23a, INVERTED FOR THE NEW FORMATS.

    A chip is rebuilt from a reference marker in the stored payload. A model-lane file leaves one
    because `_externalize_binaries` fires on its `BinaryContent`; a code-lane file never becomes
    one, so it left nothing and its chip vanished on reload — the exact regression this work
    claims to close, for the formats this work adds.

    Mutation receipt: read only `ATTACHMENT_REF_KIND` here and the spreadsheet's chip disappears
    while the image's survives.
    """
    payload = dump_for_row(
        [ModelRequest(parts=[UserPromptPart(content="what is in this?")])],
        file_attachment_ids=["att_sheet"],
    )
    text, refs = _user_text_and_refs(payload[0]["parts"][0]["content"])

    assert refs == ["att_sheet"]
    assert text == "what is in this?"  # the marker is not prose and never renders as it


def test_only_configuration_writes_and_housekeeping_are_hidden_on_the_shared_entry() -> None:
    """★ THE NARROWED SET, pinned once at the entry BOTH feeds call.

    A per-tool `hidden` that disagreed between the live emitter and the reload projection would
    show a citizen a step live that vanished on reload, or the reverse. So the whole visible set
    is asserted here rather than one member at a time — every inspection is drawn, and the two
    plumbing classes are not. The LABEL table is deliberately left open, matching the field-set
    guard at the bottom of this file: a tool registered tomorrow is classified in the same
    construction."""
    for tool, args in (
        ("read_file", '{"path": "app/page.tsx"}'),
        ("list_files", '{"path": "app"}'),
        ("search_files", '{"query": "visitors"}'),
        ("fetch_output_slice", '{"call_id": "c1"}'),
        ("run_command", '{"command": ["grep", "-rn", "visitors", "app/"]}'),
        ("read_attachment", '{"file": ".attachments/roster.xlsx"}'),
    ):
        label, hidden = classify_tool_call(tool, args)
        assert hidden is False, tool
        assert label.strip(), tool
    assert classify_tool_call("write_file", '{"path": "tsconfig.json"}')[1] is True
    assert classify_tool_call("run_command", '{"command": ["mkdir", "-p", "app/lib"]}')[1] is True


def test_reading_an_attachment_names_the_citizens_own_file() -> None:
    """★ THE ONE DELIBERATE EXCEPTION TO `_friendly_area`.

    Every other file label in this module hides the path on purpose: `components/GateTable.tsx`
    is the platform's own machinery and means nothing to the person reading. An attachment is the
    opposite — the citizen chose the file, named it, and can see a chip carrying that name. So the
    transcript says WHICH file was read, which is also the only way a turn that read three of them
    can be told apart afterwards.

    The raw-name fallback is what makes this a branch rather than a nicety: without it the step
    renders as "Used read_attachment", which is exactly the machinery leak the friendly mapping
    exists to prevent.

    Mutation receipt: delete the `ATTACHMENT_READ_TOOL` arm and the first assertion reads
    "Used read_attachment".
    """
    label, hidden = classify_tool_call("read_attachment", '{"file": ".attachments/roster.xlsx"}')

    assert label == "Reading roster.xlsx"
    assert hidden is False
    # A call with no argument at all still says something true rather than naming nothing.
    assert classify_tool_call("read_attachment", "{}")[0] == "Reading the attached file"


def test_a_read_binary_asked_to_write_is_never_drawn_as_an_inspection() -> None:
    """★ The read class is decided by argv[0], and two of its members write on a flag.

    `sed -i` rewrites a file in place and `find -delete` removes what it matched, so both would
    otherwise draw "Inspected the app's files" over a command that changed the citizen's app.
    THE FALLBACK IS THE ANSWER, not a new write label: anything the classifier cannot name
    confidently fails closed to "Working on your app". THE SPELLINGS ARE THE TEST — `-i.bak`
    carries its suffix on the flag and `-ni` bundles it with another short option. Mutation
    check: return the read label for any `argv[0] in _READ_ONLY_BINARIES` and every writing case
    below goes red while the reading cases stay green."""
    for writing in (
        ["sed", "-i", "s/a/b/", "app/page.tsx"],
        ["sed", "-i.bak", "s/a/b/", "app/page.tsx"],
        ["sed", "-ni", "1,5p", "app/page.tsx"],
        ["sed", "--in-place", "s/a/b/", "app/page.tsx"],
        ["find", "app", "-name", "*.tmp", "-delete"],
        ["find", "app", "-name", "*.tsx", "-exec", "sed", "-i", "s/a/b/", "{}", ";"],
    ):
        label, hidden = classify_command(writing)
        assert label == "Working on your app", writing
        assert hidden is False, writing
        assert command_only_inspects(writing) is False, writing
    # THE DISCRIMINATOR. The same two binaries reading, plus the one whose `-i` means something
    # else entirely — a `grep -i` is case-insensitive and must not be dragged into the fallback
    # by a flag list shared across the class.
    for reading in (
        ["sed", "-n", "1,5p", "app/page.tsx"],
        ["find", "app", "-name", "*.tsx"],
        ["grep", "-i", "visitors", "app/page.tsx"],
    ):
        label, hidden = classify_command(reading)
        assert label == "Inspected the app's files", reading
        assert hidden is False, reading
        assert command_only_inspects(reading) is True, reading


def test_classify_command_fails_closed_on_the_long_tail() -> None:
    """THE key correctness property. An unrecognized command surfaces the generic label with the
    argv DROPPED — no `npx`, `bash -c`, `python -c`, `$ `, or raw tokens in the visible label."""
    for argv in (
        ["npx", "some-tool"],
        ["bash", "-c", "rm -rf /tmp/x"],
        ["python3", "-c", "print(1)"],
    ):
        label, hidden = classify_command(argv)
        assert label == "Working on your app"
        assert hidden is False
        for leaked in ("npx", "bash", "-c", "python3", "$ ", "rm -rf", argv[-1]):
            assert leaked not in label


def test_friendly_area_maps_paths_to_areas_never_the_raw_path() -> None:
    assert _friendly_area("app/page.tsx") == ("your app's main page", False)
    assert _friendly_area("app/layout.tsx") == ("your app's overall look", False)
    assert _friendly_area("app/dashboard/page.tsx") == ("the dashboard page", False)
    assert _friendly_area("app/api/feedback/route.ts") == (
        "how your app saves and loads information",
        False,
    )
    assert _friendly_area("components/FeedbackBox.tsx") == (
        "the FeedbackBox part of the screen",
        False,
    )
    assert _friendly_area("app/globals.css") == ("your app's styling", False)
    assert _friendly_area("db/schema.ts") == ("where your app stores information", False)
    assert _friendly_area("package.json")[1] is True
    assert _friendly_area("drizzle.config.ts")[1] is True
    assert _friendly_area("tsconfig.json")[1] is True
    area, hidden = _friendly_area("lib/weird/thing.ts")
    assert area == "a part of your app"
    assert hidden is False
    assert "lib/weird" not in area


def test_classify_file_step_carries_the_verb_and_area() -> None:
    assert classify_file_step("write_file", "app/page.tsx") == (
        "Building your app's main page",
        False,
    )
    assert classify_file_step("edit_file", "app/api/x/route.ts") == (
        "Updating how your app saves and loads information",
        False,
    )
    assert classify_file_step("write_file", "package.json")[1] is True


# --- the complete set of messages a citizen sees -----------------------------------------


# THE SHARED VOCABULARY GUARD, written once — other surfaces assert against this same list.
#
# Four categories, from the acceptance criterion: a file path, a command, a library name, a
# framework term. Substring matching on a lowercased haystack, which over-matches on purpose: a
# guard that only catches the exact spellings we thought of is a guard that passes the day
# someone writes a new one.
_DEVELOPER_VOCABULARY: tuple[str, ...] = (
    # File paths and the extensions that give them away.
    "/",
    ".tsx",
    ".ts",
    ".css",
    ".json",
    "app/",
    "components/",
    "workspace",
    # Commands.
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "bash",
    "tsc",
    "eslint",
    "prettier",
    "drizzle-kit",
    "$ ",
    # Library and framework names.
    "next.js",
    "nextjs",
    "react",
    "tailwind",
    "shadcn",
    "drizzle",
    "typescript",
    "javascript",
    "webpack",
    "node.js",
    # The artefacts of a developer surface.
    "stack trace",
    "stderr",
    "stdout",
    "traceback",
    "compiler",
    "exit code",
    "console",
)


def assert_speaks_product_language(text: str, *, where: str) -> None:
    """No file path, command, library name, or framework term in a string a citizen reads.

    Exported by name so other surfaces can assert against the SAME list rather
    than each growing a private near-copy that drifts."""
    lowered = text.lower()
    hits = [word for word in _DEVELOPER_VOCABULARY if word in lowered]
    assert not hits, f"{where} leaks developer vocabulary {hits}: {text!r}"


async def test_nothing_a_citizen_reads_across_a_whole_build_is_addressed_to_a_developer(
    db_session,
) -> None:
    """Asserted over the COMPLETE rendered set, not only the agent's narration.

    This walks a full build — a read, a write, an install, a failed typecheck, the agent's
    closing line, the outcome banner — and then walks the platform's error copy for EVERY error
    class, and holds one rule over all of it. The user's OWN words are excluded, and that is not
    a loophole: echoing a prompt back is not the platform speaking developer.
    LIVENESS IS ASSERTED FIRST: a projection that returned nothing at all would satisfy every
    absence below, so the rendered set is required to be non-trivial before it is scanned."""
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()

    await write_legacy_build_started(
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
            ModelRequest(parts=[UserPromptPart(content="build me a visitor log in Next.js")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="read_file",
                        args={"path": "app/page.tsx"},
                        tool_call_id="c1",
                    ),
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": "app/api/visitors/route.ts", "file_text": "export {}\n"},
                        tool_call_id="c2",
                    ),
                    ToolCallPart(
                        tool_name="run_command",
                        args={"command": ["npm", "install", "drizzle-orm"]},
                        tool_call_id="c3",
                    ),
                    ToolCallPart(
                        tool_name="run_command",
                        args={"command": ["npx", "tsc", "--noEmit"]},
                        tool_call_id="c4",
                    ),
                    ToolCallPart(
                        tool_name="edit_file",
                        args={"path": "app/globals.css", "old": "a", "new": "b"},
                        tool_call_id="c5",
                    ),
                    ToolCallPart(
                        tool_name="list_files",
                        args={"path": "components"},
                        tool_call_id="c6",
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
                    ToolReturnPart(tool_name="read_file", content="export {}", tool_call_id="c1"),
                    ToolReturnPart(tool_name="write_file", content="ok", tool_call_id="c2"),
                    ToolReturnPart(
                        tool_name="run_command", content="added 1 package", tool_call_id="c3"
                    ),
                    ToolReturnPart(
                        tool_name="run_command",
                        content="app/page.tsx(1,1): error TS2307",
                        tool_call_id="c4",
                    ),
                    ToolReturnPart(tool_name="edit_file", content="ok", tool_call_id="c5"),
                    ToolReturnPart(tool_name="list_files", content="page.tsx", tool_call_id="c6"),
                ]
            ),
            ModelResponse(parts=[TextPart(content="Your visitor log is ready to try.")]),
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

    # The platform's own error surfaces belong to the same rendered set — they are the reason
    # this assertion had to cover more than the narration.
    platform_error_copy: list[tuple[str, str]] = []
    for source in ErrorSource:
        # NO `title=`, NO `cleaned_stack=` — the frame has no such fields to pass. The compiler's
        # first line and the de-noised stack stay on the `BuildError` the repair run reads, on
        # the server. What a citizen gets is derived from the error CLASS alone.
        frame = DiagnosticFrame(seq=1, source=source)
        platform_error_copy.append((f"{source.value} message", frame.user_message))
        platform_error_copy.append((f"{source.value} action", frame.user_action))

    rendered: list[tuple[str, str]] = [
        *(
            (f"step label {item.label!r}", item.label)
            for item in items
            if isinstance(item, StepItem)
        ),
        *(
            (f"assistant text {item.text!r}", item.text)
            for item in items
            if isinstance(item, AssistantTextItem)
        ),
        *((f"banner {item.text!r}", item.text) for item in items if isinstance(item, BannerItem)),
        *platform_error_copy,
    ]

    # LIVENESS: the build genuinely rendered, so the absences below mean something.
    labels = [item.label for item in items if isinstance(item, StepItem)]
    assert len(labels) == 6  # every tool call produced a row, and after the hidden-flag
    # change every one is drawn
    assert "Building how your app saves and loads information" in labels
    assert "Looking at your app's main page" in labels
    assert any(isinstance(item, BannerItem) for item in items)
    assert any(isinstance(item, AssistantTextItem) for item in items)
    assert len(rendered) >= 12
    # The error surfaces are LIVE too — an empty pair would sail through every absence check
    # below while rendering a citizen a blank error row.
    assert all(text.strip() for _, text in platform_error_copy)

    for where, text in rendered:
        assert_speaks_product_language(text, where=where)

    # …and the user's own words are untouched, which is what makes the exclusion honest rather
    # than a hole: the prompt still says exactly what they typed.
    prompts = [item.text for item in items if isinstance(item, UserTextItem)]
    assert prompts == ["build me a visitor log in Next.js"]


def test_the_portal_fallback_copy_and_the_server_last_resort_are_the_same_sentence() -> None:
    """The committed fallback is spelled in TWO codebases — the portal renders it when
    a frame carries no pair, and the server sends it for a class its table does not know. If the
    two drift, a citizen reads a different sentence depending on which side happened to supply
    it, and nothing anywhere would notice.

    Pinned against the literal words rather than against either implementation, so the test does
    not simply follow whichever side moved."""
    from src.services.orchestrator.errors import _UNCLASSIFIED

    assert _UNCLASSIFIED.message == "We hit a problem finishing that change."
    assert _UNCLASSIFIED.action == (
        "Try describing what you want again, or ask for something simpler."
    )


# --- The agent's prose reaches the citizen, in order ------------------------------------------
#
# A build that hits a compile error can make the model narrate its own debugging into the
# citizen's chat at length — naming implementation details like Drizzle, HMR, `globalThis`, React
# Server Components, and the platform's own word "harness" (the fixtures below are lifted from a
# real over-narrating run). A render-time drop of every text part that shares a response with a
# tool call is not the fix: it buys quiet by throwing away the explanation between the receipts,
# leaving a run of step labels with nothing joining them, which is the opposite of the voice this
# product has.
#
# THE DROP IS GONE, AND WHAT REPLACED IT IS NOT A NARROWER FILTER. Every text part the model
# emits renders, at the position it was written: a paragraph ahead of a tool call renders ahead
# of that step, one after it renders after. So these tests pin ORDER, as lists of items — a
# joined string or a membership check would pass whether or not the prose landed between the
# right steps, and the landing place is the whole of what changed.
#
# The plain-language guarantee did not move here to compensate. What the PLATFORM says is still
# held to the product's register by `assert_speaks_product_language` above, and the platform's
# own emitters are the only ones that can promise it: a step label carries no argv and a tool
# result is never transmitted. The model's own words are the model's, and an over-narrating run
# is a prompt problem and a model problem, not something the renderer hides on the way out.


async def test_build_prose_beside_a_tool_call_renders_ahead_of_that_step(db_session) -> None:
    """★ The observed shape — prose and a tool call in ONE response — read back after the turn.
    The sentence renders where it was written, ahead of the step it introduced: the label says
    what happened and the prose beside it says why, and a citizen reading the why UNDER the what
    is reading the turn in an order nobody wrote.

    Mutation-check: skip a text part whose response also holds a tool call — the one thing this
    projection must never do — and this goes red on the missing paragraph while the step still
    renders."""
    user, _project, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    prose = (
        "The SQL looks fine. Let me check if the migration was applied — "
        "maybe the pooled connection is caching a stale schema? `next dev` "
        "uses HMR and the DB pool is cached on globalThis."
    )
    await _step(
        db_session,
        user,
        conversation,
        session_id,
        [
            ModelResponse(
                parts=[
                    TextPart(content=prose),
                    ToolCallPart(
                        tool_name="write_file",
                        args='{"path": "app/page.tsx", "file_text": "x"}',
                        tool_call_id="call-1",
                    ),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="write_file",
                        content="Wrote `app/page.tsx`.",
                        tool_call_id="call-1",
                    )
                ]
            ),
        ],
    )
    items = project_rows(await _rows(db_session, user, conversation))

    # THE ORDER IS THE CLAIM, so it is asserted as a LIST of the items in the order they project.
    # The step is the liveness half — a projection that rendered nothing at all would satisfy
    # every "the prose is not missing" check that did not also require the step beside it.
    assert [item.type for item in items] == ["assistant_text", "step"]
    text_item, step_item = items
    assert isinstance(text_item, AssistantTextItem)
    assert text_item.text == prose
    assert isinstance(step_item, StepItem)
    assert step_item.label == "Building your app's main page"
    assert step_item.state == "ok"


async def test_build_text_with_no_tool_call_survives(db_session) -> None:
    """★ The plain answer. A Build turn the citizen typed a QUESTION into touches no file and
    never calls `declare_done` — this prose IS the answer, and losing it would leave them
    staring at nothing.

    It is kept because the shape is worth pinning on its own: a regression here loses the whole
    of an Ask turn's reply."""
    user, _project, conversation = await _thread(db_session)
    await _step(
        db_session,
        user,
        conversation,
        uuid.uuid4(),
        [ModelResponse(parts=[TextPart(content="Yes — the arrival time is stamped for you.")])],
    )
    items = project_rows(await _rows(db_session, user, conversation))

    texts = [i for i in items if isinstance(i, AssistantTextItem)]
    assert [t.text for t in texts] == ["Yes — the arrival time is stamped for you."]


async def test_a_plan_chat_renders_prose_beside_a_tool_call_exactly_as_a_build_chat_does(
    db_session,
) -> None:
    """★ One rule, and it still does not ask which kind of chat this is.

    THIS TEST USED TO ASSERT THE PROSE WAS DROPPED, on the grounds that a plan travels in the offer
    tool's argument and a mid-work word travels through `tell_the_user`. Those routes are still the
    right home for each, but they stopped being the ONLY way a word reaches the citizen. What that
    rule forbids is one response meaning two different things depending on the chat it sat in.
    `entry_kind=TURN`, not STEP, because that is the only shape the product can produce: every
    production writer of a STEP row stamps the Build kind, and a Plan turn persists as TURN."""
    user, _project, conversation = await _thread(db_session)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    TextPart(content="Here is what your visitor log will do."),
                    ToolCallPart(
                        tool_name="read_file", args='{"path": "app/page.tsx"}', tool_call_id="p1"
                    ),
                ]
            ),
            ModelRequest(
                parts=[ToolReturnPart(tool_name="read_file", content="1\tx", tool_call_id="p1")]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    items = project_rows(await _rows(db_session, user, conversation))

    # ORDER, ASSERTED AS A LIST — the paragraph, then the step it was written ahead of. The step
    # is the liveness half: a projection that returned nothing would satisfy any check that only
    # looked for the absence of a drop.
    assert [item.type for item in items] == ["assistant_text", "step"]
    text_item, step_item = items
    assert isinstance(text_item, AssistantTextItem)
    assert text_item.text == "Here is what your visitor log will do."
    assert isinstance(step_item, StepItem)
    assert step_item.tool == "read_file"


# --- The change reaches BACKWARDS over rows already on disk ---------------------------------
#
# The suppression was a RENDER-TIME filter — persistence never dropped a word — so deleting it
# gives the prose back in rows that were written while the rule was in force. A `schema_version`
# conjunct used to scope that rule, and the reason it existed was real: revision 0035 rewrote
# EVERY historical row's kind stamp to `build`, which made the drop newly true for every migrated
# Plan and Ask row, and prose that rendered yesterday would have stopped rendering across every
# migrated transcript at once with nothing going red. The predicate is gone, so its scope has
# nothing left to scope, and the gate went with it.
#
# The retroactive half was taken deliberately rather than tolerated: an older transcript now
# reads the way it would have read if the rule had never existed, which is how every transcript
# older than the rule already reads. So what is pinned below is the ABSENCE of a version
# predicate — when a row was written is not consulted, in either direction.


async def _pre_change_row(db_session, user, conversation) -> None:
    """One row in the exact shape a migrated Plan turn is left in: text beside a tool call,
    stamped `build` by the backfill, and carrying a schema version from before this change."""
    stored = await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    TextPart(content="Here is what your visitor log will do."),
                    ToolCallPart(
                        tool_name="read_file", args='{"path": "app/page.tsx"}', tool_call_id="r1"
                    ),
                ]
            ),
            ModelRequest(
                parts=[ToolReturnPart(tool_name="read_file", content="1\tx", tool_call_id="r1")]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.BUILD,
    )
    await db_session.execute(
        sa.update(Message).where(Message.id == stored.id).values(schema_version=SCHEMA_VERSION - 1)
    )


async def test_a_row_written_before_this_change_renders_its_prose_too(db_session) -> None:
    """★ THE RETROACTIVITY GUARD, and the whole of what is left of the version gate: nothing
    consults it. A row stamped `build` by the backfill, carrying a pre-change schema version,
    with text beside a tool call — the ordinary shape of a Plan turn that read a file and then
    explained itself. Its prose renders, exactly as a row written today does.

    Mutation-check: give the text arm of `_project_response_parts` back its old company —
    `row.schema_version >= SCHEMA_VERSION` beside a "does this response also call a tool" test —
    and this goes red on the missing paragraph while the step beside it still renders."""
    user, _project, conversation = await _thread(db_session)
    await _pre_change_row(db_session, user, conversation)
    items = project_rows(await _rows(db_session, user, conversation))

    # THE LIST, NOT A MEMBERSHIP CHECK: the paragraph renders, it renders BEFORE the step it
    # introduced, and the step is still there — so an empty projection cannot satisfy this.
    assert [item.type for item in items] == ["assistant_text", "step"]
    text_item = items[0]
    assert isinstance(text_item, AssistantTextItem)
    assert text_item.text == "Here is what your visitor log will do."


# --- redaction at the seam, asserted as a SHAPE --------------------------------------------


def test_no_browser_facing_shape_carries_tool_arguments_results_or_a_stack() -> None:
    """★ This is where the redaction guarantee actually lives.

    THE MECHANISM IS THE ABSENCE OF A FIELD, not a promise at a draw site. A client that does not
    render a field is not a guarantee — it is a client, and the next one is a different client.
    THIS GUARD PINS THE FIELD SET AND DELIBERATELY LEAVES THE LABEL TABLE OPEN: further tools are
    classified in the same construction, and pinning their labels would go red for a reason that
    has nothing to do with redaction. The catch-up snapshot's ordered `parts` are NOT in scope —
    they are the citizen's own prose and the steps it was written between."""
    assert set(StepItem.model_fields) == {"type", "seq", "tool", "label", "state", "hidden"}
    assert set(PlanOptionsItem.model_fields) == {"type", "seq", "tool_call_id", "state"}
    assert set(DiagnosticFrame.model_fields) == {
        "type",
        "seq",
        "source",
        "user_message",
        "user_action",
    }
    # And nothing named for the retired payloads survives anywhere in the projection's public
    # item union — the check that catches a re-add under a different item type.
    for item_type in get_args(DisplayItem):
        for field in item_type.model_fields:
            assert field not in {"detail", "args", "result", "cleaned_stack", "title", "stack"}, (
                f"{item_type.__name__}.{field}"
            )


async def test_a_tool_result_the_platform_wrote_to_itself_reaches_no_rendered_field(
    db_session,
) -> None:
    """The same claim asserted end to end rather than over a schema, because a shape guard
    cannot see a payload smuggled into a field that legitimately exists.

    The marker is put in the two places a leak would come from — the tool call's ARGUMENTS and
    its RETURN — and then looked for across the ENTIRE serialised projection, every item and
    every field, not just one."""
    secret = "PLATFORM-ONLY-a7f3c1"
    user, _, conversation = await _thread(db_session)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="add a field to the visitor form")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="write_file",
                        args={"path": "app/page.tsx", "content": f"const k = '{secret}'"},
                        tool_call_id="w1",
                    )
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="write_file",
                        content=f"wrote 1 file; server said {secret}",
                        tool_call_id="w1",
                    )
                ]
            ),
            ModelResponse(parts=[TextPart(content="Added the field.")]),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.BUILD,
    )

    items = project_rows(await _rows(db_session, user, conversation))

    # LIVENESS FIRST: the turn genuinely rendered, so the absence below means something rather
    # than meaning the projection returned nothing.
    assert [i.text for i in items if isinstance(i, AssistantTextItem)] == ["Added the field."]
    steps = [i for i in items if isinstance(i, StepItem)]
    assert len(steps) == 1 and steps[0].state == "ok"

    whole = json.dumps([i.model_dump(mode="json") for i in items], ensure_ascii=False)
    assert secret not in whole
    # …and the payload's OTHER half, the one a friendly label is allowed to be wrong about: a
    # write step names the area it touched, never the file it wrote.
    assert "app/page.tsx" not in whole


# --- the durable turn terminal, read back --------------------------------------------------


async def _terminal_row(
    db_session, user, conversation, *, status: str, reason: str | None
) -> None:
    """A turn-terminal row in exactly the shape `TurnEngine._write_turn_terminal` writes: hidden,
    payload-less, and carrying the whole fact in `meta`. Written through the real `append_batch`
    so a drift in the store's own rules breaks this too."""
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[],
        entry_kind=MessageEntryKind.SYSTEM_EVENT,
        kind=ChatKind.BUILD,
        visibility=MessageVisibility.HIDDEN,
        meta={
            "kind": TURN_TERMINAL_KIND,
            "turnId": "01a05879-5345-73b6-b795-47767884ea4c",
            "status": status,
            "reason": reason,
        },
    )


async def test_a_finished_turn_is_readable_as_finished_without_the_live_stream(
    db_session,
) -> None:
    """★ The turn terminal's happy path, and the reason the row exists at all.

    A transcript rebuilt from rows alone — a reload, a second tab, a process that restarted —
    has no `TurnEndedFrame` to read: that frame was delivered once, to whoever was subscribed.
    Without a stored terminal the last thing in the transcript is a reply, and a reply looks
    exactly the same whether the turn behind it finished or is still going."""
    user, _, conversation = await _thread(db_session)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="add a chart")]),
            ModelResponse(parts=[TextPart(content="Added it.")]),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.BUILD,
    )
    await _terminal_row(db_session, user, conversation, status="completed", reason=None)

    items = project_rows(await _rows(db_session, user, conversation))

    # LIVENESS: the turn itself still renders, so the terminal below is an addition rather than
    # a replacement.
    assert [i.text for i in items if isinstance(i, AssistantTextItem)] == ["Added it."]
    terminals = [i for i in items if isinstance(i, TurnTerminalItem)]
    assert len(terminals) == 1
    assert terminals[0].terminal == "completed"
    assert terminals[0].turn_id == "01a05879-5345-73b6-b795-47767884ea4c"
    # AFTER the reply, in seq order — a terminal that sorted before the turn it ends would be
    # read as ending the turn before it.
    assert items.index(terminals[0]) == len(items) - 1


async def test_a_plan_chats_turn_gets_the_same_terminal_as_a_builds(db_session) -> None:
    """The reload half, asserted SEPARATELY rather than parameterised, because the failure
    this guards against is one kind quietly getting the weaker path — and a parameterised test
    that someone later narrows to one kind reads as still covering both."""
    user, _, conversation = await _thread(db_session)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="what would this take?")]),
            ModelResponse(parts=[TextPart(content="Three steps.")]),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[],
        entry_kind=MessageEntryKind.SYSTEM_EVENT,
        kind=ChatKind.PLAN,
        visibility=MessageVisibility.HIDDEN,
        meta={
            "kind": TURN_TERMINAL_KIND,
            "turnId": "01a0587a-0000-7000-8000-000000000001",
            "status": "completed",
            "reason": None,
        },
    )

    items = project_rows(await _rows(db_session, user, conversation))
    assert [i.text for i in items if isinstance(i, AssistantTextItem)] == ["Three steps."]
    assert [i.terminal for i in items if isinstance(i, TurnTerminalItem)] == ["completed"]


async def test_the_terminal_reads_through_the_banners_own_vocabulary(db_session) -> None:
    """ONE mapping (`_banner_kind`) over the same stored meta, so a reload and a build banner
    cannot end up with two vocabularies for one fact.

    THE QUOTA CASE IS COARSER THAN THE LIVE FRAME, and that is stated here rather than papered
    over. A turn that runs out of budget ends `failed` with `reason="quota_exceeded"`, and
    `_banner_kind` reads status before reason, so it answers `failed`. The finer answer is not
    lost: it is in the row's `meta["reason"]`. Sharpening it would mean reordering the build
    banner's own mapping, and a second vocabulary for one fact is the worse trade."""
    for status, reason, expected in (
        ("stopped", "stopped_by_user", "stopped"),
        ("failed", "quota_exceeded", "failed"),
        ("failed", "self_heal_budget_exhausted", "failed"),
        ("completed", None, "completed"),
    ):
        user, _, conversation = await _thread(db_session)
        await _terminal_row(db_session, user, conversation, status=status, reason=reason)
        items = project_rows(await _rows(db_session, user, conversation))
        terminals = [i for i in items if isinstance(i, TurnTerminalItem)]
        assert [i.terminal for i in terminals] == [expected], (status, reason)
    # …and the discriminator that keeps the `stopped` row above from being a coincidence: a
    # `completed` turn and a `stopped` one do NOT read the same, which is the whole reason a
    # terminal is worth storing rather than inferring.
    assert expected == "completed"


async def test_a_stopped_turn_carries_the_reason_it_stored_and_not_only_the_terminal(
    db_session,
) -> None:
    """★ The row has always stored the reason; the item used to keep it.

    `_write_turn_terminal` writes `meta["reason"] = state.end_reason` on every arm, so the fact was
    durable the whole time — but `TurnTerminalItem` exposed `terminal` alone, handing a reloading
    client STRICTLY LESS than the live `TurnEndedFrame` gives a subscribed one: a client choosing a
    sentence from the reason could only print the generic line on reload, over an ending that had a
    name recorded beside it. ASSERTED AS A PAIR: `terminal` alone was already green before this
    change and would stay green if `reason` were dropped again tomorrow; only reading both off one
    item fails when the finer half goes missing."""
    user, _, conversation = await _thread(db_session)
    await _terminal_row(db_session, user, conversation, status="stopped", reason="stopped_by_user")

    items = project_rows(await _rows(db_session, user, conversation))
    terminals = [i for i in items if isinstance(i, TurnTerminalItem)]

    assert len(terminals) == 1
    assert (terminals[0].terminal, terminals[0].reason) == ("stopped", "stopped_by_user")


async def test_the_reason_survives_the_terminals_own_coarseness(db_session) -> None:
    """The case that proves the reason is worth carrying rather than deriving.

    `_banner_kind` reads status before reason, so every named graceful end that finishes `failed` —
    a spent daily limit, an exhausted self-heal budget, a workspace restored from its last save —
    arrives with the SAME terminal as a genuine crash, leaving a client that holds only `terminal`
    unable to tell "you used up your day" from "something broke" and no option but the generic
    failure sentence. Sharpening `_banner_kind` stays rejected: it is the build banner's mapping
    too (see `test_the_terminal_reads_through_the_banners_own_vocabulary`), and a second vocabulary
    for one fact is the worse trade. The finer answer was never lost, only withheld — so it rides.
    """
    for reason in ("quota_exceeded", "self_heal_budget_exhausted", "workspace_restored"):
        user, _, conversation = await _thread(db_session)
        await _terminal_row(db_session, user, conversation, status="failed", reason=reason)
        terminals = [
            i
            for i in project_rows(await _rows(db_session, user, conversation))
            if isinstance(i, TurnTerminalItem)
        ]
        # The coarse half is unchanged — this widens the item, it does not re-map it…
        assert [i.terminal for i in terminals] == ["failed"], reason
        # …and the fine half is what the client actually renders its sentence from.
        assert [i.reason for i in terminals] == [reason], reason


async def test_a_turn_that_ended_with_no_named_reason_reports_no_reason(db_session) -> None:
    """`None` is a real answer here, not a gap to paper over.

    A plain completion and an unexpected exception both end with `state.end_reason` unset, and a
    client reading `None` falls back to the neutral sentence for the terminal it was given. A
    placeholder — an empty string, the terminal's own word echoed into the field — would be a
    cause nobody recorded, and the client cannot tell an invented one from a stored one.

    THE THIRD ROW IS THE UNTYPED-META CASE. `meta` is JSON the projection does not validate, so
    a reason that is not a string is not a reason: it narrows to `None` rather than reaching a
    client as a number to look up in a copy table."""
    for status, stored, expected in (
        ("completed", None, None),
        ("failed", None, None),
        ("failed", 42, None),
    ):
        user, _, conversation = await _thread(db_session)
        await append_batch(
            db_session,
            user_id=user.id,
            conversation_id=conversation.id,
            messages=[],
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            kind=ChatKind.BUILD,
            visibility=MessageVisibility.HIDDEN,
            meta={
                "kind": TURN_TERMINAL_KIND,
                "turnId": "01a0587b-0000-7000-8000-000000000002",
                "status": status,
                "reason": stored,
            },
        )
        terminals = [
            i
            for i in project_rows(await _rows(db_session, user, conversation))
            if isinstance(i, TurnTerminalItem)
        ]
        assert [i.reason for i in terminals] == [expected], (status, stored)


async def test_a_row_written_before_the_reason_was_stored_still_projects(db_session) -> None:
    """A meta with no `reason` KEY AT ALL — the rows already in the database.

    Widening an item over stored history is where a required field bites: every turn-terminal
    row written before `_write_turn_terminal` carried a reason has no such key, and a projection
    that raised on them would take down the whole transcript rather than the one item. The
    default is what keeps those turns readable, and it reads as the honest "no reason recorded"
    rather than as an invented one."""
    user, _, conversation = await _thread(db_session)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[],
        entry_kind=MessageEntryKind.SYSTEM_EVENT,
        kind=ChatKind.BUILD,
        visibility=MessageVisibility.HIDDEN,
        meta={
            "kind": TURN_TERMINAL_KIND,
            "turnId": "01a0587c-0000-7000-8000-000000000003",
            "status": "stopped",
        },
    )

    terminals = [
        i
        for i in project_rows(await _rows(db_session, user, conversation))
        if isinstance(i, TurnTerminalItem)
    ]
    assert len(terminals) == 1
    assert terminals[0].reason is None


async def test_a_turn_killed_by_a_restart_leaves_no_terminal_and_reads_as_unfinished(
    db_session,
) -> None:
    """The frozen-group case, and the reason there is no `unknown` member on the item.

    A process killed mid-turn never reaches the write, so the row is simply absent — and its
    absence is a stronger signal than a value some future writer could forget to set. A
    consumer sees a turn's rows with no terminal among them and knows the turn did not finish;
    what it DRAWS for that is not this unit's business."""
    user, _, conversation = await _thread(db_session)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="add a chart")]),
            ModelResponse(parts=[TextPart(content="Working on it…")]),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.BUILD,
    )

    items = project_rows(await _rows(db_session, user, conversation))

    # LIVENESS: the interrupted turn still renders everything it managed to say.
    assert [i.text for i in items if isinstance(i, AssistantTextItem)] == ["Working on it…"]
    assert not [i for i in items if isinstance(i, TurnTerminalItem)]


async def test_the_terminal_row_is_invisible_to_the_model(db_session) -> None:
    """The half a projection test cannot see, and the one that would go wrong quietly.

    `load_history` flattens EVERY row's payload — hidden ones included, because a hidden row can
    carry the tool return that answers a deferred call. So hiddenness is not what keeps this row
    out of the model's context; an empty payload is. A one-part `ModelResponse` here, even an
    empty string, would put a blank assistant message into every later prompt of this
    conversation, for the rest of its life."""
    user, _, conversation = await _thread(db_session)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="add a chart")]),
            ModelResponse(parts=[TextPart(content="Added it.")]),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.BUILD,
    )
    await _terminal_row(db_session, user, conversation, status="completed", reason=None)

    async def _no_refs(_ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        return {}

    history = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    assert [type(m).__name__ for m in history] == ["ModelRequest", "ModelResponse"]


# --- The renderer's own ceilings are gone ----------------------------------------------------
#
# Two numbers used to be enforced twice: once in the tool body, which teaches the model where a
# bound is, and once HERE, which decided what a transcript was allowed to draw. The second copy
# is the one that hurt. A call the body had already refused rendered nothing at all, so the
# citizen read silence exactly where the agent had spoken — the update was rejected at the tool
# and deleted at the renderer, and neither half told anyone. Both copies went together, and that
# pairing was deliberate: removing only the body's would have taught the model it may write at
# length while the renderer went on quietly dropping it.
#
# What the two tests below pin is that a message far past either retired bound arrives whole.
# They are deliberately enormous rather than one character over, because there is no boundary
# left to test at: "one over is refused, exactly at it is allowed" is a claim about a number,
# and the claim now is that no number is consulted. Both go through `project_rows`, which is the
# reload half — the live emitter reads the same `update_from_args` / `proposal_from_args`, so a
# ceiling reintroduced in either helper fails here too.


async def test_a_voice_update_far_past_the_old_ceiling_renders_whole(db_session) -> None:
    """★ A spoken update the old character ceiling would have swallowed reaches the citizen
    byte for byte.

    Mutation-check: put any `len(text) > N` arm back into `update_from_args` and this goes red on
    an empty projection — which is the shape the defect actually took, a turn where the agent
    spoke and the transcript showed nothing."""
    user, _, conversation = await _thread(db_session)
    spoken = " ".join(
        f"Step {n}: the arrival board reads from the visitors table now, and I am checking the "
        "columns line up before I move on to the next one."
        for n in range(1, 41)
    )
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=TELL_THE_USER_TOOL,
                        args={"update": spoken},
                        tool_call_id="say-1",
                    )
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.BUILD,
    )

    items = project_rows(await _rows(db_session, user, conversation))

    # ONE ITEM, AND IT IS PROSE: the voice channel renders what was said at the position the
    # call occupies, and never a step announcing that the agent decided to say something.
    assert [item.type for item in items] == ["assistant_text"]
    said = items[0]
    assert isinstance(said, AssistantTextItem)
    assert said.text == spoken
    # Stated again as a length, because a clip that kept the opening sentence would otherwise
    # fail as a five-thousand-character diff nobody can read.
    assert len(said.text) == len(spoken)


async def test_a_first_slice_far_past_the_old_ceiling_renders_whole(db_session) -> None:
    """★ How many pieces belong in a first round is a judgement about the citizen's request,
    which is the thing the agent is for. A ceiling here refused proposals the agent had made well
    and drew nothing for a call the tool body had already refused.

    Six pieces, half again the retired bound, and every one of them is named — along with all
    nine the agent said it had picked up, because a proposal that narrows without listing
    everything back reads as a refusal. Mutation-check: put a `len(first) > N` arm back into
    `_slice_argument` and this goes red on an empty projection."""
    user, _, conversation = await _thread(db_session)
    found = [
        "a visitor sign-in form",
        "a badge printout",
        "the arrivals board",
        "an email to the host",
        "a daily summary",
        "a photo capture",
        "an evacuation list",
        "a returning-visitor lookup",
        "a monthly export",
    ]
    first = found[:6]
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=PROPOSE_SLICE_TOOL,
                        args={
                            "found": found,
                            "first": first,
                            "why": "These six are the reception desk's whole morning.",
                            "question": "Shall I start there?",
                        },
                        tool_call_id="slice-1",
                    )
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )

    items = project_rows(await _rows(db_session, user, conversation))

    assert [item.type for item in items] == ["assistant_text"]
    proposal = items[0]
    assert isinstance(proposal, AssistantTextItem)
    for piece in found:
        assert f"- {piece}" in proposal.text
    # THE LIST LENGTHS ARE THE CLAIM, not merely membership: nine picked up and six chosen means
    # fifteen bulleted lines, so a renderer that clipped either list to the retired bound of four
    # fails here even though every piece it kept would still be "in" the text.
    assert proposal.text.count("\n- ") == len(found) + len(first)
    # The agent's own reasoning and its one question survive too — a proposal that lost them
    # leaves the citizen a list with nothing to answer.
    assert "These six are the reception desk's whole morning." in proposal.text
    assert proposal.text.endswith("Shall I start there?")


# --- the attachment fence never reaches the bubble ------------------------------------


async def _user_turn(db_session, user, conversation, content) -> None:
    """One citizen turn persisted in the U7 wire shape — content is whatever the caller
    passes, so a test can store the LIST form (attachment items, typed prose last) that the
    composer really produces."""
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelRequest(parts=[UserPromptPart(content=content)])],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
        meta={},
    )


async def _user_items(db_session, user, conversation) -> list[UserTextItem]:
    rows = await _rows(db_session, user, conversation)
    return [i for i in project_rows(rows) if isinstance(i, UserTextItem)]


async def test_an_inlined_file_body_is_kept_out_of_the_user_bubble(db_session) -> None:
    """★ THE GUARD THIS FILE EXISTS TO PIN.

    `_is_attachment_fence` is the ONLY thing standing between a persisted file body and the
    citizen's own message bubble, and until now nothing tested it — a grep for `fence` across
    `backend/tests/` returned only build-prompt fixtures. This work deletes the inline-text lane
    that produces these blocks, and the fence check looks like part of that lane; it is not.
    Every conversation already on disk that carried a CSV or a spreadsheet has that content
    stored as a bare string inside a `user-prompt` content list, so deleting the check makes
    those turns render the whole file, row by row, as if the citizen had typed it.

    Driven through `append_batch` → `project_rows` rather than by calling the predicate,
    deliberately: `test_zip_safety.py` is the cautionary example in this repo of a suite that
    proves an algorithm works and never that anything calls it.

    Mutation receipt: drop the `if not _is_attachment_fence(item)` guard in
    `_user_text_and_refs` and this test goes red on the CSV rows appearing in `.text`.
    """
    user, _, conversation = await _thread(db_session)
    await _user_turn(
        db_session,
        user,
        conversation,
        [
            '<attachment name="roster.csv" type="text">\n'
            "badge,name,terminal\n"
            "1041,Asha Rao,T1\n"
            "1042,Vikram Nair,T2\n"
            "</attachment>",
            "How many people are on this?",
        ],
    )

    items = await _user_items(db_session, user, conversation)

    assert len(items) == 1
    # The prose survives whole — the bubble shows what the citizen typed.
    assert items[0].text == "How many people are on this?"
    # And nothing of the file does. Asserted on the row CONTENT, not on the fence tags: a
    # future fence shape that still leaked the body would pass a tag-only assertion.
    assert "badge,name,terminal" not in items[0].text
    assert "Asha Rao" not in items[0].text
    assert "1042" not in items[0].text


async def test_a_plain_message_is_not_mistaken_for_a_fence(db_session) -> None:
    """The companion case, so the guard cannot be satisfied by dropping everything. A citizen
    who types the word `<attachment` mid-sentence still gets their sentence back."""
    user, _, conversation = await _thread(db_session)
    await _user_turn(
        db_session, user, conversation, ["What does <attachment ...> mean in your logs?"]
    )

    items = await _user_items(db_session, user, conversation)

    assert items[0].text == "What does <attachment ...> mean in your logs?"


async def test_the_bare_string_shape_still_reaches_the_bubble(db_session) -> None:
    """A text-only turn is persisted as a bare string, not a list (`prompt_content`'s fast
    path). The filter must not touch that branch — pinned because this work rewrites the producer
    and the two shapes are easy to collapse into one."""
    user, _, conversation = await _thread(db_session)
    await _user_turn(db_session, user, conversation, "just a question, no files")

    items = await _user_items(db_session, user, conversation)

    assert items[0].text == "just a question, no files"


async def test_an_attachment_reference_becomes_a_chip_id_not_prose(db_session) -> None:
    """The other half of `_user_text_and_refs`: a `bial-attachment-ref` marker leaves the prose
    and arrives as an id the UI draws a chip from. The reload path builds on this — the projection
    already ships the ids and the reload path throws them away — so the producing side is
    pinned here before that work moves it."""
    user, _, conversation = await _thread(db_session)
    # A REAL `BinaryContent`, not a hand-written marker dict. `_externalize_binaries` mints the
    # `bial-attachment-ref` shape at persist time, so passing the marker directly would seed the
    # POST-serialization form into the pre-serialization slot — a shape production never writes,
    # and one pydantic-ai warns about because it matches no content type.
    await _user_turn(
        db_session,
        user,
        conversation,
        [
            BinaryContent(data=_PNG, media_type="image/png", identifier="att-7f3c"),
            "what is in this file?",
        ],
    )

    items = await _user_items(db_session, user, conversation)

    assert [a.attachment_id for a in items[0].attachments] == ["att-7f3c"]
    assert items[0].text == "what is in this file?"
    assert "att-7f3c" not in items[0].text
    # `project_rows` is pure, so it carries the id and nothing else; the name and media type
    # are filled by `project_conversation`, which has a database session. Pinned so the split
    # stays deliberate rather than looking like an unfinished item.
    assert items[0].attachments[0].name == ""
    assert items[0].attachments[0].media_type == ""


async def _code_lane_turn(db_session, user, conversation, text, ids) -> None:
    """One citizen turn carrying code-lane file markers, as the send route stamps them."""
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelRequest(parts=[UserPromptPart(content=text)])],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
        meta={},
        file_attachment_ids=ids,
    )


async def test_a_code_lane_chip_is_drawn_once_across_the_whole_transcript(db_session) -> None:
    """★ U7 — ONE CHIP PER FILE, ON THE TURN THAT CARRIED IT.

    The marker used to be stamped with the conversation's WHOLE code-lane set on every turn, so a
    citizen who attached one spreadsheet and then sent three more messages saw the same chip four
    times on reload — once under each bubble, including bubbles whose message never mentioned it.
    The send route narrows the stamp now, but rows written before that narrowing are already on
    disk and are indistinguishable from narrowed ones, so the projection dedupes as well.

    Seeded in the PRE-narrowing shape deliberately: that is the shape the dedupe exists for, and
    a test seeded in the post-narrowing shape would pass with the dedupe deleted.

    Mutation receipt: drop `seen_attachments` from `project_rows` and the second and third
    bubbles regrow `att_sheet`, and the third regrows `att_roster` as well.
    """
    user, _, conversation = await _thread(db_session)
    await _code_lane_turn(db_session, user, conversation, "what is in this sheet?", ["att_sheet"])
    # The old writer re-stamped everything the conversation had so far, every time.
    await _code_lane_turn(
        db_session, user, conversation, "and now the roster too", ["att_sheet", "att_roster"]
    )
    await _code_lane_turn(
        db_session, user, conversation, "no file on this one", ["att_sheet", "att_roster"]
    )

    items = await _user_items(db_session, user, conversation)

    assert [[a.attachment_id for a in i.attachments] for i in items] == [
        ["att_sheet"],
        ["att_roster"],
        [],
    ]
    # THE BUBBLE ITSELF SURVIVES its chips being taken away. A third turn whose every marker was
    # already spent still has prose, and dropping it would delete the citizen's own message.
    assert [i.text for i in items] == [
        "what is in this sheet?",
        "and now the roster too",
        "no file on this one",
    ]


async def test_a_turn_with_nothing_left_after_the_dedupe_is_not_drawn(db_session) -> None:
    """The other side of the guard above: a bubble with no prose AND no fresh chip is nothing to
    draw, so it must not become an empty one. The composer sends bare-attachment turns with a
    stand-in sentence, but the pre-narrowing rows on disk include genuinely empty re-stamps."""
    user, _, conversation = await _thread(db_session)
    await _code_lane_turn(db_session, user, conversation, "here it is", ["att_sheet"])
    await _code_lane_turn(db_session, user, conversation, "", ["att_sheet"])

    items = await _user_items(db_session, user, conversation)

    assert [[a.attachment_id for a in i.attachments] for i in items] == [["att_sheet"]]
    assert [i.text for i in items] == ["here it is"]


# --- the enrichment entry point -----------------------------------------------------------


async def _stored_attachment(db_session, user_id, attachment_id: str, name: str, media_type: str):
    row = Attachment(
        user_id=user_id,
        attachment_id=attachment_id,
        media_type=media_type,
        name=name,
        size=1,
        storage_key=f"att/{user_id}/{attachment_id}",
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def test_a_chip_is_filled_in_from_the_attachment_row(db_session) -> None:
    """`project_rows` is pure and carries only the id; the name and media type live in the
    attachments table. This is the seam that joins them, and it is what every route calls."""
    user, _, conversation = await _thread(db_session)
    await _stored_attachment(db_session, user.id, "att_sheet", "movements.xlsx", EXCEL_MEDIA_TYPE)
    await _code_lane_turn(db_session, user, conversation, "what is in this?", ["att_sheet"])

    rows = await _rows(db_session, user, conversation)
    items = [
        i
        for i in await project_conversation(db_session, user_id=user.id, rows=rows)
        if isinstance(i, UserTextItem)
    ]

    chip = items[0].attachments[0]
    assert (chip.name, chip.media_type) == ("movements.xlsx", EXCEL_MEDIA_TYPE)
    assert chip.kind == chip_kind_for(EXCEL_MEDIA_TYPE)


async def test_a_transcript_naming_another_citizens_attachment_learns_nothing_about_it(
    db_session,
) -> None:
    """★ THE PREDICATE THIS QUERY CANNOT LOSE.

    An attachment id is a client-supplied string that is stored verbatim into the payload, so a
    transcript can name any id at all. Without the `user_id` predicate the enrichment would hand
    back the OWNER's filename and media type — one citizen reading another's file names out of
    their own chat.

    Mutation receipt: drop `Attachment.user_id == user_id` from the select in
    `project_conversation` and this goes red on the stranger's filename appearing.
    """
    owner = await UserFactory.create(db_session)
    await _stored_attachment(db_session, owner.id, "att_theirs", "payroll.xlsx", EXCEL_MEDIA_TYPE)

    reader, _, conversation = await _thread(db_session)
    await _code_lane_turn(db_session, reader, conversation, "what is in this?", ["att_theirs"])

    rows = await _rows(db_session, reader, conversation)
    items = [
        i
        for i in await project_conversation(db_session, user_id=reader.id, rows=rows)
        if isinstance(i, UserTextItem)
    ]

    chip = items[0].attachments[0]
    assert chip.attachment_id == "att_theirs"  # the reference survives — it is in their payload
    assert chip.name == ""
    assert chip.media_type == ""


async def test_a_reference_whose_row_is_gone_reads_as_unavailable_rather_than_failing(
    db_session,
) -> None:
    """A reclaimed attachment leaves its reference in the payload forever. The chip has to render
    as unavailable, which the browser draws from exactly this empty-name state."""
    user, _, conversation = await _thread(db_session)
    await _code_lane_turn(db_session, user, conversation, "what is in this?", ["att_reclaimed"])

    rows = await _rows(db_session, user, conversation)
    items = [
        i
        for i in await project_conversation(db_session, user_id=user.id, rows=rows)
        if isinstance(i, UserTextItem)
    ]

    assert [a.attachment_id for a in items[0].attachments] == ["att_reclaimed"]
    assert items[0].attachments[0].name == ""


async def test_the_whole_transcript_costs_one_attachment_read(db_session) -> None:
    """★ THE ANTI-N+1 THE DOCSTRING CLAIMS, asserted rather than trusted.

    Ids are collected across every item before the read, so forty attachments cost one query.
    Counted by instrumenting the session, because the alternative — trusting the comment — is how
    a later edit moves the select inside the loop without anyone noticing.

    Mutation receipt: move the select into the per-item loop and the count goes above one.
    """
    user, _, conversation = await _thread(db_session)
    for n in range(4):
        await _stored_attachment(db_session, user.id, f"att_{n}", f"f{n}.xlsx", EXCEL_MEDIA_TYPE)
        await _code_lane_turn(db_session, user, conversation, f"file {n}", [f"att_{n}"])

    rows = await _rows(db_session, user, conversation)
    reads: list[str] = []

    @event.listens_for(db_session.sync_session, "do_orm_execute")
    def _count(state) -> None:
        if state.is_select and "attachments" in str(state.statement).lower():
            reads.append("read")

    try:
        await project_conversation(db_session, user_id=user.id, rows=rows)
    finally:
        event.remove(db_session.sync_session, "do_orm_execute", _count)

    assert len(reads) == 1, f"expected one attachment read for the transcript, got {len(reads)}"
