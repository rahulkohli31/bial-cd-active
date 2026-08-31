"""U6 — the one history→display derivation (`services/messages/projection.py`).

Rows are written through the REAL producers/store (`append_batch`, `write_build_started`,
`write_build_outcome`) in the exact shapes U5 pinned
(`test_transcript_steps.py` / `test_producers.py`), so these tests break when the producer
contract drifts — which is the point. The golden build test doubles as U10's parity fixture:
the live stream must render THIS list for THIS transcript.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import get_args

import sqlalchemy as sa
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from src.api.v1.build_sessions.schemas import BuildSessionStatus, ErrorSource
from src.api.v1.conversations.schemas import DiagnosticFrame
from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.build_sessions.outcome import write_build_outcome, write_build_started
from src.services.messages.projection import (
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
    classify_command,
    classify_file_step,
    project_rows,
)
from src.services.messages.store import (
    SCHEMA_VERSION,
    append_batch,
    load_history,
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
        kind=ChatKind.BUILD,
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
        # F3/U3: friendly AREA, never the raw path; friendly command copy, never the argv.
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
    # is gone from the wire (U14; the field-set guard at the bottom of this file is where that
    # is pinned).
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


async def test_reads_are_hidden_steps_that_say_only_what_they_touched(db_session) -> None:
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
    # U16 — THIS ASSERTION USED TO PIN THE LEAK. It read `== "Read app/page.tsx"`, which made
    # the raw path the EXPECTED output of a helper whose two neighbours exist to guarantee the
    # opposite. Flipped, not deleted: the absence is asserted, and paired with the liveness half
    # (a real friendly area still renders) so a read arm that started returning "" would not
    # pass by rendering nothing at all.
    assert "app/page.tsx" not in steps[0].label
    assert steps[0].label == "Looking at your app's main page"
    assert steps[1].label == "Inspected the app's files"
    # AND NEITHER STEP CARRIES WHAT THE READ RETURNED. This used to assert the opposite —
    # `steps[1].detail.result == "app/db.ts:3: visitors"` — which is grep output over the
    # citizen's own data, shipped on a HIDDEN step, on every reload. `grep -rn` over an app is
    # exactly the call whose result is worth the least to a reader and the most to anyone else.
    assert "visitors" not in _rendered(steps[1])
    assert "app/db.ts" not in _rendered(steps[1])


async def test_hidden_rows_render_nothing_but_stay_auditable(db_session) -> None:
    # The mode-switch marker used to be the third hidden row here. It is gone with the switch
    # that wrote it (`tests/api/v1/conversations/test_mode_switch.py` is its inertness guard);
    # the `build_started` overlay carries the same property and is still written today.
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
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
    # The (closed) started row renders nothing; only the outcome banner shows.
    assert [item.type for item in items] == ["banner"]
    # …but the audit read still has it. Two rows, one of them hidden — three and two before
    # the mode-switch marker was retired along with the switch that wrote it.
    assert len(rows) == 2
    hidden = [row for row in rows if row.visibility is MessageVisibility.HIDDEN]
    assert len(hidden) == 1


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
    # FAILED, AND THAT IS ALL IT SAYS. The refusal text ("blocked …") used to ride the step's
    # detail block; a retry-prompt body is the harness talking to the model about why a call was
    # refused, which is neither the citizen's business nor safe to assume it is sanitised.
    assert steps[0].state == "failed"
    assert "blocked" not in _rendered(steps[0])


async def test_plan_options_states_have_no_third_member(db_session) -> None:
    """U8 — `build_failed` is retired without a caller, so a resolution recorded before its
    retirement must not read back as a still-actionable third state. `_plan_options_state`'s
    catch-all reads ANYTHING it does not recognise — including a stray `build_failed:<reason>`
    overlay from before the retired recorder — as `refine`: the build behind it never
    happened, and the card is spent, so offering a button with nothing behind it would be
    worse than reading it as resolved.

    The fixture is deliberately the PRE-MIGRATION shape: a prose message ahead of an
    empty-argument tool call, which is what every card presented before U5 looks like on
    disk (`test_the_plan_renders_from_the_offers_own_stored_call_args` below covers the new
    shape, where the plan rides the argument itself).

    Mutation-check: revert `_plan_options_state`'s catch-all to return `"build_failed"` and
    this goes red on opt-2's state without any other test in this file moving."""
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
    """U5 — the offer's stored `args` is the single authoritative copy of the plan. The
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
    """Every card presented before U5's migration is exactly this shape: the tool call
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


# --- F3/U3: the friendly classifier (one source of truth, live == reload) -------


def test_classify_command_maps_the_pinned_commands() -> None:
    """The pinned command copy: install / data-setup / data-ready / checks — friendly, never
    the raw argv. This is the SAME translator the live emitter (`tools.py`) calls."""
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


def test_classify_command_hides_reads_and_housekeeping() -> None:
    """Read-only inspections AND housekeeping plumbing are hidden — they never clutter the feed."""
    for read_only in (["ls", "-la"], ["grep", "-rn", "x", "app/"], ["cat", "app/page.tsx"]):
        _, hidden = classify_command(read_only)
        assert hidden is True
    for housekeeping in (["mkdir", "-p", "app/lib"], ["mv", "a", "b"], ["touch", "x.ts"]):
        _, hidden = classify_command(housekeeping)
        assert hidden is True


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
    """The friendly-area file map: an app AREA, never the filename. Config is hidden noise."""
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
    # Config / settings are hidden noise.
    assert _friendly_area("package.json")[1] is True
    assert _friendly_area("drizzle.config.ts")[1] is True
    assert _friendly_area("tsconfig.json")[1] is True
    # Anything unrecognized → the generic area, NEVER the raw path.
    area, hidden = _friendly_area("lib/weird/thing.ts")
    assert area == "a part of your app"
    assert hidden is False
    assert "lib/weird" not in area


def test_classify_file_step_carries_the_verb_and_area() -> None:
    """write_file reads as *Building*, the edits as *Updating* — friendly area, never a path."""
    assert classify_file_step("write_file", "app/page.tsx") == (
        "Building your app's main page",
        False,
    )
    assert classify_file_step("edit_file", "app/api/x/route.ts") == (
        "Updating how your app saves and loads information",
        False,
    )
    # A config write is hidden regardless of the verb.
    assert classify_file_step("write_file", "package.json")[1] is True


# --- AE13: the complete set of messages a citizen sees -----------------------------------


# THE SHARED VOCABULARY GUARD, written once (U16) — U15 and U18 assert against this same list.
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

    Exported by name so U15 and U18 can assert their own surfaces against the SAME list rather
    than each growing a private near-copy that drifts."""
    lowered = text.lower()
    hits = [word for word in _DEVELOPER_VOCABULARY if word in lowered]
    assert not hits, f"{where} leaks developer vocabulary {hits}: {text!r}"


async def test_ae13_nothing_a_citizen_reads_across_a_whole_build_is_addressed_to_a_developer(
    db_session,
) -> None:
    """AE13 — asserted over the COMPLETE rendered set, not only the agent's narration.

    The narration was never the whole story: the platform's own error rendering was the most
    developer-looking thing on the screen, and it sat below a feed whose read steps printed raw
    paths. So this walks a full build — a read, a write, an install, a failed typecheck, the
    agent's closing line, the outcome banner — and then walks the platform's error copy for
    EVERY error class, and holds one rule over all of it.

    The user's OWN words are excluded, and that is not a loophole: a citizen may perfectly well
    type "make it look like the React docs", and echoing their prompt back is not the platform
    speaking developer.

    LIVENESS IS ASSERTED FIRST. A projection that returned nothing at all would satisfy every
    absence below, so the rendered set is required to be non-trivial before it is scanned."""
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
    assert len(labels) == 6  # every tool call produced a row (hidden ones included)
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
    """The committed fallback is spelled in TWO codebases — `BuildProgress.tsx` renders it when
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


# --- U15/R20: narration between tools never reaches the citizen ----------------
#
# The live browser run of 2026-08-24 is the fixture these three pin. A build that hit a
# compile error made the model narrate its own debugging into the citizen's chat — ~1900
# words naming Drizzle, HMR, `globalThis`, React Server Components, and the platform's own
# word "harness". `NARRATION_VOICE` asks the model not to; a failing build is exactly when
# it stops complying, so the guarantee is enforced here instead of only in the prompt.


async def test_build_text_beside_a_tool_call_is_dropped(db_session) -> None:
    """★ The observed leak: prose and a tool call in ONE response. The step label already
    tells the citizen what happened, so the prose is the model talking to itself."""
    user, _project, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    await _step(
        db_session,
        user,
        conversation,
        session_id,
        [
            ModelResponse(
                parts=[
                    TextPart(
                        content=(
                            "The SQL looks fine. Let me check if the migration was applied — "
                            "maybe the pooled connection is caching a stale schema? `next dev` "
                            "uses HMR and the DB pool is cached on globalThis."
                        )
                    ),
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

    assert not [i for i in items if isinstance(i, AssistantTextItem)]
    # LIVENESS — the step must still be there, or this passes by rendering nothing at all.
    steps = [i for i in items if isinstance(i, StepItem)]
    assert [s.label for s in steps] == ["Building your app's main page"]


async def test_build_text_with_no_tool_call_survives(db_session) -> None:
    """★ The zero-mutation ending. A Build turn the citizen typed a QUESTION into touches no
    file and never calls `declare_done` — this prose IS the answer, and dropping it would
    leave them staring at nothing. Keyed on 'no tool call beside it', which is why keying on
    `meta.kind == "write_completion"` would have been wrong."""
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


async def test_a_plan_chat_keeps_its_prose_beside_a_tool_call(db_session) -> None:
    """★ A Plan chat's prose IS the deliverable — the same drop there would delete the feature,
    so the gate must be kind-scoped rather than universal.

    `entry_kind=TURN`, not STEP, because that is the only shape the product can actually
    produce: every production writer of a STEP row stamps the Build kind (the BRAIN build loop
    owns that kind), and a Plan turn persists as TURN. Mirrors
    `test_plan_options_states_have_no_third_member` above."""
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

    texts = [i for i in items if isinstance(i, AssistantTextItem)]
    assert [t.text for t in texts] == ["Here is what your visitor log will do."]


# --- R-10: the drop must not become RETROACTIVE over migrated transcripts -------------
#
# Revision 0035 rewrote EVERY historical row's kind stamp to `build`, because "was this a Plan
# chat?" is not a question the old rows can answer — a conversation stamped `plan` was one mode
# switch away from writing files. That rewrite makes the left conjunct of the narration
# predicate newly TRUE for every migrated Plan and Ask row, and a turn that read files and then
# wrote prose in one response is the ORDINARY shape of those, not an edge case. Without a way to
# tell a rewritten row from a natively-written one, prose that renders today would silently stop
# rendering on reload — across every migrated transcript at once, with nothing going red.
#
# The fix is `schema_version`, and the reason it is the right one rather than a convenient one:
# it is a fact about WHEN a row was written, not about what kind of chat it was. It adds no
# per-row exemption keyed on the migrated stamp — which is the shape the two-emitter narration
# guarantee explicitly forbids — and it costs the "behaviour lives in the toolset" claim nothing,
# because it is not about behaviour at all.


async def _pre_migration_row(db_session, user, conversation) -> None:
    """One row in the exact shape a migrated Plan turn is left in: text beside a tool call,
    stamped `build` by the backfill, still carrying the pre-migration schema version."""
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


async def test_a_migrated_row_still_renders_its_prose(db_session) -> None:
    """★ THE R-10 GUARD. A pre-migration row, stamped `build` by the backfill, with text beside
    a tool call — the ordinary shape of a Plan turn that read a file and then explained itself.
    Its prose must still render, exactly as it did before the migration ran.

    Mutation-check: drop the `schema_version` conjunct from `_project_response_parts` and this
    goes red while every other narration test stays green — which is precisely how the defect
    would have shipped."""
    user, _project, conversation = await _thread(db_session)
    await _pre_migration_row(db_session, user, conversation)
    items = project_rows(await _rows(db_session, user, conversation))

    texts = [i for i in items if isinstance(i, AssistantTextItem)]
    assert [t.text for t in texts] == ["Here is what your visitor log will do."]


async def test_the_mirror_a_row_written_after_the_migration_still_drops(db_session) -> None:
    """★ THE OTHER HALF, in the same file and against the same predicate. A row written NOW, in
    a Build chat, with text beside a tool call, still drops its narration.

    Both halves are needed: the guard above alone is satisfied by removing the drop entirely,
    which would put ~1900 words of the model debugging itself back into a citizen's chat."""
    user, _project, conversation = await _thread(db_session)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    TextPart(content="Let me check whether the pool is caching a stale schema."),
                    ToolCallPart(
                        tool_name="read_file", args='{"path": "app/db.ts"}', tool_call_id="n1"
                    ),
                ]
            ),
            ModelRequest(
                parts=[ToolReturnPart(tool_name="read_file", content="1\tx", tool_call_id="n1")]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.BUILD,
    )
    items = project_rows(await _rows(db_session, user, conversation))

    assert not [i for i in items if isinstance(i, AssistantTextItem)]
    # LIVENESS — the step is still rendered, so this cannot pass by rendering nothing at all.
    assert [i for i in items if isinstance(i, StepItem)]


async def test_one_fixture_two_rows_one_predicate(db_session) -> None:
    """Both rows in ONE conversation, because that is the state a migrated transcript is
    actually in the moment its owner sends their next message: old rows below, new rows above,
    and one predicate that has to get both right."""
    user, _project, conversation = await _thread(db_session)
    await _pre_migration_row(db_session, user, conversation)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    TextPart(content="Now let me look at the database file."),
                    ToolCallPart(
                        tool_name="read_file", args='{"path": "app/db.ts"}', tool_call_id="r2"
                    ),
                ]
            ),
            ModelRequest(
                parts=[ToolReturnPart(tool_name="read_file", content="1\tx", tool_call_id="r2")]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.BUILD,
    )
    items = project_rows(await _rows(db_session, user, conversation))

    texts = [i.text for i in items if isinstance(i, AssistantTextItem)]
    assert texts == ["Here is what your visitor log will do."]


# --- U14/R36: redaction at the seam, asserted as a SHAPE ------------------------------------


def test_no_browser_facing_shape_carries_tool_arguments_results_or_a_stack() -> None:
    """★ AE34, and this is where U14's guarantee actually lives.

    THE MECHANISM IS THE ABSENCE OF A FIELD, not a promise at a draw site. A step used to carry
    `detail.args` and `detail.result` — the raw arguments and the raw return of every tool call,
    redacted, clipped to four thousand characters, shipped on every frame and every reload item
    INCLUDING hidden steps — and the browser parsed them and rendered them nowhere. A
    `DiagnosticFrame` used to carry `title` and `cleaned_stack` beside the citizen's sentence,
    under a docstring that called them "safe to transmit; NOT a product surface"; the sentence
    "safe to render verbatim" that preceded it is what put a stack trace under a file-path title
    in somebody's chat. A client that does not render a field is not a guarantee — it is a
    client, and the next one is a different client.

    THIS GUARD PINS THE FIELD SET AND DELIBERATELY LEAVES THE LABEL TABLE OPEN. Plan C registers
    further tools that are classified in this same construction, and a guard that pinned the
    labels too would go red in C's branch for a reason that has nothing to do with redaction —
    at which point the fix under time pressure is to loosen it. So: the fields are closed, the
    vocabulary is not.

    `text_so_far` on a catch-up snapshot is NOT in scope and must not be: it is the citizen's own
    assistant prose, and it is what lets a tab reconnecting mid-turn see what it missed.
    """
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
    every field, not in the one place it used to live."""
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


# --- U20: the durable turn terminal, read back --------------------------------------------


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
    """★ U20's happy path, and the reason the row exists at all.

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
    """R72's reload half, asserted SEPARATELY rather than parameterised, because the failure
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
    `_banner_kind` reads status before reason — so it answers `failed`, where the live
    `TurnEndedFrame` carries both halves and a client can tell the two apart. The finer answer
    is not lost: it is in the row's `meta["reason"]`, which is where the mapping read it from.
    Sharpening this would mean reordering `_banner_kind`, which is also the build banner's
    mapping and is pinned by that surface's own tests — a second vocabulary for one fact is the
    thing this reuse exists to avoid, and it is the worse trade."""
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
