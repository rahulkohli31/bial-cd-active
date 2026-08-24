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

from src.api.v1.build_sessions.schemas import BuildSessionStatus, ErrorSource
from src.api.v1.conversations.schemas import DiagnosticFrame
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
    _friendly_area,
    classify_command,
    classify_file_step,
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
    # U16 — THIS ASSERTION USED TO PIN THE LEAK. It read `== "Read app/page.tsx"`, which made
    # the raw path the EXPECTED output of a helper whose two neighbours exist to guarantee the
    # opposite. Flipped, not deleted: the absence is asserted, and paired with the liveness half
    # (a real friendly area still renders) so a read arm that started returning "" would not
    # pass by rendering nothing at all.
    assert "app/page.tsx" not in steps[0].label
    assert steps[0].label == "Looking at your app's main page"
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
        frame = DiagnosticFrame(
            seq=1,
            source=source,
            title="app/page.tsx(12,5): error TS2307: Cannot find module '@/components/X'",
            cleaned_stack="app/page.tsx(12,5): error TS2307\n  at Object.<anonymous>",
        )
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


async def test_write_text_beside_a_tool_call_is_dropped(db_session) -> None:
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


async def test_write_text_with_no_tool_call_survives(db_session) -> None:
    """★ The zero-mutation ending. A Write turn the citizen typed a QUESTION into touches no
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


async def test_plan_mode_keeps_its_prose_beside_a_tool_call(db_session) -> None:
    """★ Plan mode's prose IS the deliverable — the same drop there would delete the feature,
    so the gate must be mode-scoped rather than universal.

    `entry_kind=TURN`, not STEP, because that is the only shape the product can actually
    produce: every production writer of a STEP row hardcodes WRITE mode (the BRAIN build
    loop owns that kind), and Plan/Ask turns persist as TURN. Mirrors
    `test_plan_options_three_states` above."""
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
        mode=ConversationMode.PLAN,
    )
    items = project_rows(await _rows(db_session, user, conversation))

    texts = [i for i in items if isinstance(i, AssistantTextItem)]
    assert [t.text for t in texts] == ["Here is what your visitor log will do."]
