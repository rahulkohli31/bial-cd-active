"""U5/U6/U8 — present_plan_options mechanics: the call DEFERS (the click is the result), the
plan RIDES the argument (there is no other copy of it), resolutions are idempotent,
newest-only, and derived identically by the projection, and there is no `build_failed`
member left for a resolution to be.

U4's inertness guards live here too: the prose heuristic that used to infer a plan from the
model's TEXT — counting list items, scanning for a trailing `?` — is gone, and so is the
forced retry and the fabricated card it fed. Nothing anywhere issues a second model request,
or invents a card, as a consequence of what the model wrote.

The platform's own voice on this path lives here too: a turn that produced no words produces no
assistant message at all, and the one sentence the platform still authors when an offer is
refused rides a system row of its own rather than a `ModelResponse` in the model's name."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
from typing import get_args

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)

from src.api.v1.conversations._shared import MAX_MESSAGE_TEXT_CHARS
from src.api.v1.conversations.turns import ResolvePlanOptionsResponse
from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.message import MessageEntryKind
from src.services.agent import toolsets as toolsets_module
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.messages.projection import (
    PLATFORM_TEXT_KIND,
    AssistantTextItem,
    PlanOptionsItem,
    StepItem,
    project_rows,
)
from src.services.messages.store import load_history, load_rows
from src.services.sandbox.config import SandboxConfig
from src.services.turns import engine as engine_module
from src.services.turns import plan_options as plan_options_module
from src.services.turns.copy import PLAN_NOT_KEPT_TEXT
from src.services.turns.engine import TurnEngine, plan_from_call, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from src.services.turns.plan_options import (
    NoPendingOptionsError,
    PendingPlanOptions,
    PlanChoice,
    PlanOptionsExpiredError,
    find_pending,
    newest_card,
    record_build_started,
    resolve,
    resolve_pending_as_refine,
    stored_call,
)
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeSandboxClient
from tests.transcript import live_shape, reload_shape, rendered_text

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)

_PLAN_TEXT = "Here is the plan:\n1. Add a table\n2. Wire the form\n3. Ship it"

# --- U4: the exact shapes that used to fire the retired prose heuristic ------------------
#
# `_looks_plan_shaped` counted list items and scanned the reply's tail for a trailing `?`.
# Two-or-more list items with no `?` on the tail read as "a plan was written" and forced a
# retry; a retry that also produced no call got a card FABRICATED underneath it. All three of
# these texts are kept verbatim from the shapes that tripped it (one of them a real
# production incident, turn 019fc05f-d3df-729d-a688-d33a309bddfd) so the tests below are
# regression guards against the actual defect, not a generic "text alone proves nothing".

_LIST_SHAPED_NO_CALL_TEXT = (
    "Here is how I'd tackle it:\n1. Add a visitors table.\n2. Wire the intake form.\n3. Ship it.\n"
)

_CLARIFYING_QUESTION_TEXT = (
    "A couple of options exist.\nWhich fields should the visitors table hold?"
)

# The model asked which of three options to take as settled, listed them A/B/C, and closed on
# "I'm not going to start writing code until one of us has moved" — an explicit refusal to
# finalize. The retired heuristic read the last line, found no `?`, counted the three ANSWER
# CHOICES as three plan steps, forced the tool, got (rightly) no call, and synthesized a
# Build-it card underneath the question.
_CHOICE_QUESTION_TEXT = (
    "We keep circling the same three fields, so let me ask it straight — which of these "
    "do you want me to take as settled?\n"
    "\n"
    "- **A.** Name, badge number and visit purpose, and nothing else.\n"
    "- **B.** Everything on the paper form, including the host's signature line.\n"
    "- **C.** Start from A and add fields once the front desk has used it for a week.\n"
    "\n"
    "Any of those is workable. But I'm not going to start writing code until one of us "
    "has moved."
)


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every kind pins the project's LIVE container now (R18) — a Plan chat attaches a sandbox
    # exactly like Build does, so these engine-level tests need a configured deployment the
    # same way `test_write_turn.py` does, or every turn dies at the workspace pin before the
    # model — and the plan-options mechanics under test — ever run.
    monkeypatch.setattr(
        settings,
        "sandbox",
        SandboxConfig(
            subscription_id="s",
            resource_group="r",
            region="westeurope",
            managed_environment_name="aca-env",
            acr_server="acr.azurecr.io",
            acr_username="acr-user",
            acr_password=SecretStr("acr-pass"),
            image_ref="acr/img:latest",
        ),
    )


@pytest.fixture(autouse=True)
async def _sandbox_dependencies(fake_redis, fake_storage) -> None:
    """The R10 liveness lease (Redis) and the sandbox attach's storage reads both need a
    backing fake now that a Plan turn also attaches a live container. Pulled in as an
    autouse wrapper around the shared `fake_redis`/`fake_storage` fixtures
    (`tests/conftest.py`) rather than added to every test signature."""
    return None


@pytest.fixture(autouse=True)
def _fresh_engine():
    _mid_reply.clear()
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    yield engine
    set_turn_engine_for_tests(None)
    _mid_reply.clear()


@pytest.fixture
def session_factory(db_session):
    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    return lambda: _session()


async def _plan_conversation(db_session):
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    return user, conv


async def _noop_persist() -> None:
    return None


async def _run_turn(engine, db_session, session_factory, model, user, conv):
    await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="plan the visitors app",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=conv.project_id,
        model=model,
        session_factory=session_factory,
        persist_user_turn=_noop_persist,
        manager=SessionManager(),
        sandbox_client=FakeSandboxClient(),
    )
    state = engine.peek(conv.id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)
    return state


def _call_options(plan: str = _PLAN_TEXT, call_id: str = "opt-1") -> DeltaToolCalls:
    """A complete offer call, the plan riding the argument the way the real tool requires
    (U5) — every fixture in this file that means to make a HONOURABLE offer uses this, never
    the empty `json_args="{}"` the pre-U5 tool took."""
    return DeltaToolCalls(
        {
            0: DeltaToolCall(
                name="present_plan_options",
                json_args=json.dumps({"plan": plan}),
                tool_call_id=call_id,
            )
        }
    )


# --- U5: the plan rides the argument, and it is the only copy of it ----------------------


async def test_the_plan_argument_is_the_happy_path_live_and_on_reload(
    _fresh_engine, db_session, session_factory
) -> None:
    """Covers AE7. The offer tool takes the plan as its own argument now — no separate prose
    message, no forced retry, no second model request of any kind. The live stream pushes
    that argument as ordinary text, then the card; the reload projection derives the SAME
    text from the SAME stored call, so live and reload can never drift."""
    calls = {"count": 0}

    async def _stream(messages, info: AgentInfo):
        calls["count"] += 1
        yield _call_options()

    user, conv = await _plan_conversation(db_session)
    state = await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )
    assert state.status == "completed"
    assert calls["count"] == 1  # nothing anywhere re-issues this run

    # Live: the plan text precedes its card, in that order, and nothing else rides with it.
    text_frames = [f for f in state.ring if f.type == "text_delta"]
    assert "".join(f.text for f in text_frames) == _PLAN_TEXT
    cards = [f for f in state.ring if f.type == "plan_options"]
    assert len(cards) == 1 and cards[0].item.state == "pending"
    assert cards[0].item.tool_call_id == "opt-1"
    assert state.ring.index(cards[0]) > state.ring.index(text_frames[-1])

    pending = await find_pending(db_session, user_id=user.id, conversation_id=conv.id)
    assert pending is not None and pending.tool_call_id == "opt-1"
    assert pending.synthesized is False

    # U6 — the pending row carries the call's id and nothing else: no snapshot pin rides
    # along with it any more.
    #
    # ADDRESSED BY META KIND, NOT BY POSITION. `rows[-1]` used to be the turn's row; U20's
    # durable terminal is appended after it, so an index here would now be reading the wrong
    # row — and would keep silently reading the wrong row as later units add lifecycle records.
    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    pending_rows = [
        row
        for row in rows
        if isinstance(row.meta, dict) and row.meta.get("kind") == engine_module.PENDING_META_KIND
    ]
    assert len(pending_rows) == 1
    assert pending_rows[0].meta == {
        "kind": engine_module.PENDING_META_KIND,
        "toolCallId": "opt-1",
    }

    # Reload: the SAME text, from the SAME single copy (the stored call's own args).
    items = project_rows(list(rows))
    text_items = [i for i in items if isinstance(i, AssistantTextItem)]
    options = [i for i in items if isinstance(i, PlanOptionsItem)]
    assert [t.text for t in text_items] == [_PLAN_TEXT]
    assert len(options) == 1 and options[0].state == "pending"


async def test_the_writing_up_status_leaves_the_screen_when_the_plan_arrives(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ A WATCHING TAB AND A RELOADED ONE READ THE SAME THING, on the turn that has a status
    line to withdraw.

    "Writing up the plan…" is announced the moment the tool's block opens, because the plan
    rides the argument and thousands of tokens stream before the call resolves. It has no
    durable counterpart — a reloaded transcript shows the plan and the offer and never the
    moment before them — so the status has to LEAVE the feed when the call lands. Dropping it
    from the turn's own memory only settles what a LATE subscriber gets; a tab that was already
    connected received the started frame, and unless the withdrawal reaches the wire too it
    keeps a spinning row labelled "Writing up the plan…" above the finished plan for the rest
    of the session, and only a reload clears it.

    Compared as SHAPES rather than as two separate absence checks: an assertion that the live
    feed holds no visible step passes just as well if the whole turn produced nothing, and the
    plan text on both sides is the liveness half that says it did not.

    Mutation check: put `state.drop_step(...)` back in place of `_retract_step(...)` in the
    engine's offer arm and the live shape grows a `step:present_plan_options` the reloaded one
    does not have."""

    async def _stream(messages, info: AgentInfo):
        # The provider's own two-part shape: the tool NAME with an empty argument first, which
        # is what opens the status, then the plan.
        yield DeltaToolCalls(
            {0: DeltaToolCall(name="present_plan_options", json_args="", tool_call_id="opt-1")}
        )
        yield DeltaToolCalls(
            {0: DeltaToolCall(json_args=json.dumps({"plan": _PLAN_TEXT}), tool_call_id="opt-1")}
        )

    user, conv = await _plan_conversation(db_session)
    state = await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )
    assert state.status == "completed"

    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    assert live_shape(state) == [f"text:{_PLAN_TEXT}"] == reload_shape(project_rows(list(rows)))

    # The withdrawal is a FRAME, not just an absence from the snapshot: the connected tab is
    # told, on the same call id, and the item it is told with is hidden.
    withdrawals = [
        f
        for f in state.ring
        if f.type == "step" and f.tool_call_id == "opt-1" and f.phase == "finished"
    ]
    assert len(withdrawals) == 1 and withdrawals[0].item.hidden is True

    # And the card the status was making way for is still there — the retraction takes the
    # status off the screen and nothing else with it.
    assert [f.item.tool_call_id for f in state.ring if f.type == "plan_options"] == ["opt-1"]


async def test_an_offer_survives_unrelated_turns_and_still_names_its_own_plan(
    _fresh_engine, db_session, session_factory
) -> None:
    """Covers AE7. An offer made on turn three must still name turn three's plan after two
    more exchanges that have nothing to do with it — the stored call is the one and only
    copy, so nothing later in the conversation can dilute or relabel it."""
    engine = _fresh_engine
    user, conv = await _plan_conversation(db_session)

    def _plain(text: str) -> FunctionModel:
        async def _stream(messages, info: AgentInfo):
            yield text

        return FunctionModel(stream_function=_stream)

    async def _offer(messages, info: AgentInfo):
        yield _call_options(plan=_PLAN_TEXT, call_id="opt-turn-3")

    await _run_turn(
        engine, db_session, session_factory, _plain("Tell me about your visitors."), user, conv
    )
    await _run_turn(
        engine, db_session, session_factory, _plain("A few more questions first."), user, conv
    )
    await _run_turn(
        engine, db_session, session_factory, FunctionModel(stream_function=_offer), user, conv
    )
    await _run_turn(
        engine, db_session, session_factory, _plain("One more unrelated aside."), user, conv
    )
    await _run_turn(engine, db_session, session_factory, _plain("And another."), user, conv)

    pending = await find_pending(db_session, user_id=user.id, conversation_id=conv.id)
    assert pending is not None and pending.tool_call_id == "opt-turn-3"
    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    call = stored_call(list(rows), "opt-turn-3")
    assert call is not None and plan_from_call(call) == _PLAN_TEXT


async def test_a_revised_plan_leaves_exactly_one_live_offer_naming_the_newer_plan(
    _fresh_engine, db_session, session_factory
) -> None:
    """Covers AE8. The agent revises and calls again: exactly one offer is live and it names
    the newer plan. The older one reads as spent — a newer presentation supersedes it by
    construction, whether or not anyone ever clicked it — and its OWN plan is untouched:
    superseded, not overwritten."""
    engine = _fresh_engine
    user, conv = await _plan_conversation(db_session)
    plan_v1 = _PLAN_TEXT
    plan_v2 = _PLAN_TEXT + "\n4. Add a CSV export."

    async def _offer_v1(messages, info: AgentInfo):
        yield _call_options(plan=plan_v1, call_id="opt-v1")

    async def _offer_v2(messages, info: AgentInfo):
        yield _call_options(plan=plan_v2, call_id="opt-v2")

    await _run_turn(
        engine, db_session, session_factory, FunctionModel(stream_function=_offer_v1), user, conv
    )
    await _run_turn(
        engine, db_session, session_factory, FunctionModel(stream_function=_offer_v2), user, conv
    )

    pending = await find_pending(db_session, user_id=user.id, conversation_id=conv.id)
    assert pending is not None and pending.tool_call_id == "opt-v2"
    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    newer = stored_call(list(rows), "opt-v2")
    assert newer is not None and plan_from_call(newer) == plan_v2

    # The older offer cannot be acted on any more, even though nothing ever resolved it.
    with pytest.raises(PlanOptionsExpiredError):
        await resolve(
            db_session,
            user_id=user.id,
            conversation_id=conv.id,
            tool_call_id="opt-v1",
            choice="refine",
        )
    # …and its own plan is exactly what it always was.
    older = stored_call(list(rows), "opt-v1")
    assert older is not None and plan_from_call(older) == plan_v1


async def test_a_run_cut_off_mid_argument_records_no_offer_and_no_partial_plan(
    _fresh_engine, db_session, session_factory
) -> None:
    """Covers AE32, and the closely related edge case: a run stopped after the started status
    but before the argument completes leaves the status out of the reload too. `content_block_
    start` puts the tool's NAME on the wire before any argument arrives (U5), so a stop that
    lands in that window must not leave a half-written plan anywhere — live, or durable.

    ★ AND THE STATUS ITSELF CANNOT OUTLIVE THE TURN, which is the half the absence checks below
    cannot see. "Writing up the plan…" is withdrawn by the OFFER arm when the argument lands,
    and a turn that ends in this window never reaches that arm — so a tab that was already
    connected had the started frame and nothing after it, and kept a spinning row under a turn
    that was over for the rest of the session. Only a reload cleared it. The terminal withdraws
    it instead, which is what the shape comparison and the hidden frame at the end assert.

    Mutation check: drop the `plan_status_tool_call_id` retraction from `_finish` and the live
    shape grows a `step:present_plan_options` the reloaded transcript does not have."""
    engine = _fresh_engine
    gate = asyncio.Event()

    async def _stream(messages, info: AgentInfo):
        # The block opens (the name is on the wire) but the argument never finishes.
        yield DeltaToolCalls(
            {0: DeltaToolCall(name="present_plan_options", json_args="", tool_call_id="opt-cut")}
        )
        await gate.wait()
        yield DeltaToolCalls(  # pragma: no cover - never reached; the turn is stopped first
            {0: DeltaToolCall(json_args=json.dumps({"plan": _PLAN_TEXT}), tool_call_id="opt-cut")}
        )

    user, conv = await _plan_conversation(db_session)
    turn_id = await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="plan the visitors app",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=conv.project_id,
        model=FunctionModel(stream_function=_stream),
        session_factory=session_factory,
        persist_user_turn=_noop_persist,
        manager=SessionManager(),
        sandbox_client=FakeSandboxClient(),
    )
    state = engine.peek(conv.id)
    assert state is not None
    while not state.steps:  # the started status has landed; the argument is still open
        await asyncio.sleep(0.01)

    assert await engine.stop_turn(conv.id, turn_id) is True
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)

    assert state.status == "stopped"
    assert state.text_blocks() == []  # no plan, partial or otherwise, reached the live feed
    assert not [f for f in state.ring if f.type == "plan_options"]

    # WRITE-BEFORE-DONE: a run that never completed persists no TURN row — no offer, no
    # partial plan. The reloaded transcript therefore carries neither a status nor an offer.
    #
    # The one row that IS here is U20's durable terminal, and it belongs: this turn genuinely
    # ended (stopped), and a reload that could not tell that would show a cut-off run as still
    # in flight forever. It is hidden, carries no payload, and says only how the turn ended.
    assert await find_pending(db_session, user_id=user.id, conversation_id=conv.id) is None
    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    assert [row.entry_kind for row in rows] == [MessageEntryKind.SYSTEM_EVENT]
    assert rows[0].payload == []
    assert rows[0].meta is not None and rows[0].meta["status"] == "stopped"

    # AND THE WATCHING TAB IS TOLD, which the assertions above cannot see: they read the
    # snapshot and the durable rows, and the started frame went out on the wire before either
    # existed. Reduced as shapes, so what is compared is what each surface still SHOWS.
    assert live_shape(state) == [] == reload_shape(project_rows(list(rows)))

    # The withdrawal is a FRAME on the status's own call id, and the item it carries is hidden —
    # the only way a tab that was already connected can learn the status is over. Exactly one, so
    # the terminal cannot double up with the offer arm on a turn that reached both.
    withdrawals = [
        f
        for f in state.ring
        if f.type == "step" and f.tool_call_id == "opt-cut" and f.phase == "finished"
    ]
    assert len(withdrawals) == 1 and withdrawals[0].item.hidden is True


async def test_an_empty_plan_argument_records_no_offer_and_says_so_once(
    _fresh_engine, db_session, session_factory
) -> None:
    """Error path (U5/R28a). A whitespace-only argument is nothing to build from — the exact
    defect the retired heuristic used to manufacture, a Build-it button under a plan nobody
    wrote. The call comes off what is persisted; one platform-authored line explains why.

    THE LINE IS THE PLATFORM'S AND THE RECORD NOW SAYS SO. It reaches the citizen exactly as it
    always did, and the second half of this test is about where it comes FROM — a system row of
    the platform's own rather than a response written in the model's name."""

    async def _stream(messages, info: AgentInfo):
        yield _call_options(plan="   ")

    user, conv = await _plan_conversation(db_session)
    state = await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )

    assert state.status == "completed"
    assert await find_pending(db_session, user_id=user.id, conversation_id=conv.id) is None
    # SAID ONCE, AND SAID ALONE. Compared as the whole block list rather than searched for: a
    # substring check passes just as happily with the sentence repeated, or buried under prose
    # the platform was never supposed to add beside it.
    assert state.text_blocks() == [PLAN_NOT_KEPT_TEXT]

    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    items = project_rows(list(rows))
    assert [i.text for i in items if isinstance(i, AssistantTextItem)] == [PLAN_NOT_KEPT_TEXT]
    assert not [i for i in items if isinstance(i, PlanOptionsItem)]

    # THE PROVENANCE IS THE CHANGE, so it is pinned as hard as the words are. The sentence
    # explains a refusal the PLATFORM made, and it used to be appended to the run's own batch as
    # a `ModelResponse` — stored, replayed and read back as something the model had written. It
    # now rides one visible `SYSTEM_EVENT` row of its own, stamped with the platform-text kind
    # and the turn it belongs to, carrying the words in `meta` and NO payload at all, and it
    # reaches the citizen through the projection's arm for that kind. What they read is
    # unchanged; the record has stopped attributing it to the model.
    platform_rows = [
        row
        for row in rows
        if isinstance(row.meta, dict) and row.meta.get("kind") == PLATFORM_TEXT_KIND
    ]
    assert len(platform_rows) == 1
    assert platform_rows[0].entry_kind is MessageEntryKind.SYSTEM_EVENT
    assert platform_rows[0].meta == {
        "kind": PLATFORM_TEXT_KIND,
        "turnId": str(state.turn_id),
        "text": PLAN_NOT_KEPT_TEXT,
    }
    # ★ AND THE MODEL IS NEVER TOLD IT SAID THIS. `load_history` flattens every row's payload —
    # hidden ones included, and with no filter by kind or entry kind — so a sentence stored in a
    # payload is a sentence the NEXT turn hands the model as a paragraph it wrote itself, to
    # build on or repeat. The empty payload is what keeps it out, the same way the turn-terminal
    # row stays out, and it is asserted DIRECTLY rather than only through the reader: the reader
    # returns nothing at all for this turn (the refused call was stripped, which left the run
    # with no persistable message of its own), so an absence read off it alone would hold just as
    # well against a row that still carried the sentence in a payload nobody happened to load.
    assert platform_rows[0].payload == []
    assert not any(PLAN_NOT_KEPT_TEXT in str(row.payload) for row in rows)

    async def _noop_rehydrate(_attachment_ids):
        raise AssertionError("no attachments in this history")

    history = await load_history(
        db_session, user_id=user.id, conversation_id=conv.id, rehydrate=_noop_rehydrate
    )
    assert not any(PLAN_NOT_KEPT_TEXT in str(message) for message in history)
    for row in rows:
        for message in row.payload:
            assert "present_plan_options" not in str(message.get("parts", []))


async def test_an_over_ceiling_plan_argument_records_no_offer_and_is_not_truncated(
    _fresh_engine, db_session, session_factory
) -> None:
    """Error path (U5/R44). An argument past the stored-message ceiling is REFUSED, never
    trimmed: a plan cut mid-sentence is one the citizen would agree to sight-unseen. The
    refusal is explained by the same platform-authored line, on the same platform-owned row."""
    huge_plan = "x" * (MAX_MESSAGE_TEXT_CHARS + 1)

    async def _stream(messages, info: AgentInfo):
        yield _call_options(plan=huge_plan)

    user, conv = await _plan_conversation(db_session)
    state = await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )

    assert state.status == "completed"
    assert await find_pending(db_session, user_id=user.id, conversation_id=conv.id) is None
    assert state.text_blocks() == [PLAN_NOT_KEPT_TEXT]  # refused outright, not trimmed to fit

    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    for row in rows:
        assert huge_plan[:200] not in str(row.payload)  # no fragment of the refused plan survives
    items = project_rows(list(rows))
    assert [i.text for i in items if isinstance(i, AssistantTextItem)] == [PLAN_NOT_KEPT_TEXT]
    # The same platform row as the empty-argument refusal, restated here rather than left to the
    # sibling test: these two are the pair a reader compares, and a provenance assertion on only
    # one of them reads as an accident of whichever test was written second.
    assert [
        row.entry_kind
        for row in rows
        if isinstance(row.meta, dict) and row.meta.get("kind") == PLATFORM_TEXT_KIND
    ] == [MessageEntryKind.SYSTEM_EVENT]


# --- U4: the prose heuristic, the forced retry and the fabricated card are all gone -------


async def test_a_list_shaped_reply_with_no_tool_call_produces_no_offer(
    _fresh_engine, db_session, session_factory
) -> None:
    """Edge case (U4). Two-or-more list items and no trailing `?` is precisely the shape
    `_looks_plan_shaped` used to count as a plan. There is no reader left that counts list
    items, so the model simply not calling the tool ends the turn — no retry, no card."""
    calls = {"count": 0}

    async def _stream(messages, info: AgentInfo):
        calls["count"] += 1
        yield _LIST_SHAPED_NO_CALL_TEXT

    user, conv = await _plan_conversation(db_session)
    state = await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )

    assert state.status == "completed"
    assert calls["count"] == 1  # nothing re-issues this run
    assert await find_pending(db_session, user_id=user.id, conversation_id=conv.id) is None
    assert not [f for f in state.ring if f.type == "plan_options"]


async def test_a_clarifying_question_turn_produces_no_offer(
    _fresh_engine, db_session, session_factory
) -> None:
    """Edge case (U4). A clarifying question is a legitimate planning turn on its own — never
    a trigger for a forced retry, with or without a `?` at the very end."""
    calls = {"count": 0}

    async def _stream(messages, info: AgentInfo):
        calls["count"] += 1
        yield _CLARIFYING_QUESTION_TEXT

    user, conv = await _plan_conversation(db_session)
    state = await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )

    assert state.status == "completed"
    assert calls["count"] == 1
    assert await find_pending(db_session, user_id=user.id, conversation_id=conv.id) is None


async def test_the_turn_019fc05f_regression_shape_still_produces_no_offer(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ THE FABRICATED CARD, for real (turn 019fc05f-d3df-729d-a688-d33a309bddfd). The model
    asked which of three options to take as settled, listed them A/B/C, and closed on an
    explicit refusal to finalize. The retired heuristic read the last line, found no `?`,
    counted the three ANSWER CHOICES as three plan steps, forced the tool, got (rightly) no
    call, and synthesized a Build-it card underneath the question nobody had answered."""
    calls = {"count": 0}

    async def _stream(messages, info: AgentInfo):
        calls["count"] += 1
        yield _CHOICE_QUESTION_TEXT

    user, conv = await _plan_conversation(db_session)
    state = await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )

    assert state.status == "completed"
    assert calls["count"] == 1  # A/B/C is a question with its answers written out
    assert await find_pending(db_session, user_id=user.id, conversation_id=conv.id) is None
    assert not [f for f in state.ring if f.type == "plan_options"]


def test_the_prose_heuristic_the_forced_retry_and_the_synthesizer_no_longer_exist() -> None:
    """★ INERTNESS GUARD (U4). Named individually rather than as a set: an implementer working
    from a plan SUMMARY rather than the plan itself tends to delete three symbols and leave
    the fourth behind (or vice versa), and a coarser check (e.g. "the module still imports")
    would not catch that partial cleanup.

    Mutation-check: reintroduce any ONE of these names as a no-op stub (e.g. `def
    _looks_plan_shaped(...): return False`) and this goes red on that exact assertion, with
    every other test in this file still green."""
    for name in (
        "_looks_plan_shaped",
        "_list_item_body",
        "_is_labelled_alternative",
        "_CLARIFYING_TAIL_LINES",
        "_FORCE_OPTIONS_NUDGE",
    ):
        assert not hasattr(engine_module, name), f"engine.{name} should have been deleted"
    assert not hasattr(TurnEngine, "_synthesize_options")
    assert not hasattr(toolsets_module, "plan_options_only_toolset")


# --- U6: the stale-plan pin is gone, at both ends of the wire -----------------------------


def test_no_snapshot_head_rides_on_the_turn_state_or_the_pending_record() -> None:
    """★ INERTNESS GUARD (U6). The pin's only writer sat inside the mode branch that no
    longer exists; this guards that nothing reintroduces the field under any name, on either
    the in-memory turn state or the durable pending record.

    Mutation-check: add `head_sha: str | None = None` back to either dataclass and this goes
    red on that exact field name."""
    state_fields = {f.name for f in dataclasses.fields(engine_module._TurnState)}
    assert "head_sha" not in state_fields
    pending_fields = {f.name for f in dataclasses.fields(PendingPlanOptions)}
    assert "head_sha" not in pending_fields
    assert not hasattr(plan_options_module, "approved_plan_text")


# --- U8: there is no third resolution value anywhere --------------------------------------


def test_no_resolution_value_exists_that_a_user_cannot_produce() -> None:
    """★ INERTNESS GUARD (U8). `build_failed` had no production caller and no way for a user
    to reach it. This pins every place that value could still hide: the wire type the engine
    writes, the projection the client reads, and the endpoint response the client parses.

    Mutation-check: add "build_failed" back to any ONE of the three `Literal`s (or restore
    `record_build_failure`) and this goes red on that exact assertion."""
    assert get_args(PlanChoice) == ("refine", "build")
    assert get_args(PlanOptionsItem.model_fields["state"].annotation) == (
        "pending",
        "refine",
        "build",
    )
    assert get_args(ResolvePlanOptionsResponse.model_fields["state"].annotation) == (
        "refine",
        "build",
    )
    assert not hasattr(plan_options_module, "record_build_failure")


# --- resolution mechanics untouched by U4/U5/U6/U8 (kept from the prior suite) ------------


async def test_refine_resolution_is_idempotent_and_feeds_the_next_run(
    _fresh_engine, db_session, session_factory
) -> None:
    async def _stream(messages, info: AgentInfo):
        yield _call_options()

    user, conv = await _plan_conversation(db_session)
    await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )

    first = await resolve(
        db_session, user_id=user.id, conversation_id=conv.id, tool_call_id="opt-1", choice="refine"
    )
    assert first.already_resolved is False
    second = await resolve(
        db_session, user_id=user.id, conversation_id=conv.id, tool_call_id="opt-1", choice="refine"
    )
    assert second.already_resolved is True and second.choice == "refine"

    # The projection reads the SAME record the model will see.
    rows = await load_rows(db_session, user_id=user.id, conversation_id=conv.id)
    options = [i for i in project_rows(list(rows)) if isinstance(i, PlanOptionsItem)]
    assert options[0].state == "refine"

    async def _noop_rehydrate(_attachment_ids):
        raise AssertionError("no attachments in this history")

    history = await load_history(
        db_session, user_id=user.id, conversation_id=conv.id, rehydrate=_noop_rehydrate
    )
    flat = [
        part
        for message in history
        for part in getattr(message, "parts", [])
        if getattr(part, "part_kind", "") == "tool-return"
    ]
    assert any(p.tool_call_id == "opt-1" and p.content == "refine" for p in flat)


async def test_only_the_newest_pending_is_actionable(
    _fresh_engine, db_session, session_factory
) -> None:
    round_ = {"n": 0}

    async def _stream(messages, info: AgentInfo):
        round_["n"] += 1
        yield _call_options(call_id=f"opt-{round_['n']}")

    user, conv = await _plan_conversation(db_session)
    model = FunctionModel(stream_function=_stream)
    await _run_turn(_fresh_engine, db_session, session_factory, model, user, conv)
    # The user keeps typing → implicit refine resolves card 1; a second plan presents card 2.
    implicit = await resolve_pending_as_refine(
        db_session, user_id=user.id, conversation_id=conv.id
    )
    assert implicit is not None and implicit.choice == "refine"
    engine2 = TurnEngine()
    set_turn_engine_for_tests(engine2)
    await _run_turn(engine2, db_session, session_factory, model, user, conv)

    pending = await find_pending(db_session, user_id=user.id, conversation_id=conv.id)
    assert pending is not None and pending.tool_call_id == "opt-2"
    with pytest.raises(NoPendingOptionsError):
        await resolve(
            db_session,
            user_id=user.id,
            conversation_id=conv.id,
            tool_call_id="opt-missing",
            choice="refine",
        )
    # Resolving the SUPERSEDED card is refused; its stored resolution (refine) answers
    # idempotently instead.
    superseded = await resolve(
        db_session, user_id=user.id, conversation_id=conv.id, tool_call_id="opt-1", choice="refine"
    )
    assert superseded.already_resolved is True


async def test_build_started_after_a_raced_refine_writes_no_second_wire_return(
    _fresh_engine, db_session, session_factory
) -> None:
    # The Build-it vs turn-start race (#2): a concurrent turn-start resolves the card as
    # "refine" (a real ToolReturnPart) while the build is starting. record_build_started must
    # then record the build as a system overlay — NOT a second ToolReturnPart — so the loaded
    # history carries exactly one return for the card and the conversation never wedges.
    async def _stream(messages, info: AgentInfo):
        yield _call_options()

    user, conv = await _plan_conversation(db_session)
    await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )
    # The racing writer's implicit refine (a real wire return for the card).
    await resolve(
        db_session, user_id=user.id, conversation_id=conv.id, tool_call_id="opt-1", choice="refine"
    )
    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    card = newest_card(list(rows))
    assert card is not None
    # Build-it re-checks and finds the card already answered → overlay, not a 2nd return.
    await record_build_started(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        pending=card,
        answered_already=True,
    )

    async def _noop_rehydrate(_attachment_ids):
        raise AssertionError("no attachments in this history")

    history = await load_history(
        db_session, user_id=user.id, conversation_id=conv.id, rehydrate=_noop_rehydrate
    )
    returns = [
        part
        for message in history
        for part in getattr(message, "parts", [])
        if getattr(part, "part_kind", "") == "tool-return" and part.tool_call_id == "opt-1"
    ]
    assert len(returns) == 1  # exactly one wire return — no duplicate tool_result to reject
    # NEWEST WINS BY ROW SEQ. The build overlay was recorded AFTER the raced refine return, so
    # the card projects "build" — which is the truth the user can see (a build is running).
    fresh_rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    projected = [i for i in project_rows(list(fresh_rows)) if isinstance(i, PlanOptionsItem)]
    assert projected[0].state == "build"


# --- nothing is written in the model's name -------------------------------------------------
#
# WHAT USED TO BE HERE, AND WHY IT IS NOT. While prose written beside a tool call was dropped at
# render time, a turn that explained itself between two calls could reach its end with a screen
# of finished steps and not one word — so the platform wrote a closing sentence of its own into
# the transcript, as a `ModelResponse`, in the model's name, to cover it. The drop is gone. A
# wordless turn now means the agent chose not to speak, which is legitimate, and the sentence
# that existed to paper over a case that no longer arises went with it.
#
# The one platform sentence still reachable on this path is the refused-offer line, and the two
# refusal tests above pin where it now lives: its own visible `SYSTEM_EVENT` row, stamped with
# the platform-text kind, never a response inside the run's own batch.


async def test_a_turn_that_only_used_a_tool_leaves_no_words_behind(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ The turn that reads a file and then has nothing to say now says nothing.

    THE SHAPE IS NARROWER THAN "EVERY RESPONSE CALLED A TOOL", and that is worth stating rather
    than quietly writing a fixture that happens to pass. The literal shape cannot end a
    pydantic-ai run at all: the loop stops only on a response with no tool call, and a response
    with no usable output either is retried and then fails the turn — a different terminal with
    its own message. What reaches this arm is a final response whose text is present but empty of
    content, so that is the case under test.

    LIVE AND ON RELOAD, because a sentence that appears on one path and not the other is the
    failure this guards. The read step is asserted on both sides for the same reason: an absence
    assertion that passes because the turn rendered nothing whatsoever would prove the opposite
    of what it claims.

    Mutation check: restore the arm that pushed a platform-authored sentence whenever a turn
    produced no prose, and this goes red twice over — on the live blocks and on the projected
    items — while every other test in this file stays green."""
    engine, (user, conv) = _fresh_engine, await _plan_conversation(db_session)

    requests = 0

    async def _reads_then_says_nothing(messages: list[ModelMessage], info: AgentInfo):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {
                0: DeltaToolCall(
                    name="read_file", json_args='{"path": "app/page.tsx"}', tool_call_id="r1"
                )
            }
            return
        yield "   "

    state = await _run_turn(
        engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_reads_then_says_nothing),
        user,
        conv,
    )

    assert state.status == "completed"
    # NOT ONE READABLE WORD. The model's closing response was whitespace, which the live path
    # carries through as an empty block and the reload projection drops outright — so what the
    # citizen is given is nothing, and nothing is what the platform adds to it.
    assert rendered_text(state).strip() == ""

    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    # No row anywhere claims to be the platform speaking on this turn's behalf.
    assert not [
        row
        for row in rows
        if isinstance(row.meta, dict) and row.meta.get("kind") == PLATFORM_TEXT_KIND
    ]
    items = project_rows(list(rows))
    assert not [i for i in items if isinstance(i, AssistantTextItem)]
    # LIVENESS, on both paths. The turn is not empty — it did the work and shows the work — so
    # the two absences above are about what was said, not about a transcript that never rendered.
    assert [i.tool for i in items if isinstance(i, StepItem)] == ["read_file"]
    assert "r1" in state.steps


async def test_a_plain_answer_turn_shows_the_answer_and_no_activity_at_all(
    _fresh_engine, db_session, session_factory
) -> None:
    """The other half, and the one that keeps the platform quiet on an ordinary turn.

    A response that calls no tool IS the answer. Nothing is appended underneath it — a closing
    line telling the citizen there was nothing to show, sitting beneath the thing they were just
    shown, is the noise this unit removes — and nothing is manufactured beside it either: a turn
    that needed no tools has no activity to display, so `steps` stays empty."""
    engine, (user, conv) = _fresh_engine, await _plan_conversation(db_session)

    async def _just_answers(messages: list[ModelMessage], info: AgentInfo):
        yield "Your visitor list already records arrival times."

    state = await _run_turn(
        engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_just_answers),
        user,
        conv,
    )

    assert state.text_blocks() == ["Your visitor list already records arrival times."]
    assert state.steps == {}


async def test_the_words_spoken_mid_work_are_the_whole_of_what_the_turn_says(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ The turn that spoke through the voice channel and then closed on nothing.

    The citizen has something to read, so the platform must not follow it with a second sentence
    of its own — least of all one saying there was nothing to show, underneath a line the agent
    had just written.

    FILTERED ON `strip()` BECAUSE THE CLOSING RESPONSE IS WHITESPACE: an empty block live, and
    dropped entirely by the reload projection. What is pinned here is that no READABLE block
    joins the spoken one, which is the claim that would break if the platform started writing."""
    engine, (user, conv) = _fresh_engine, await _plan_conversation(db_session)
    spoken = "Looking at how your visitor list works today."

    requests = 0

    async def _speaks_then_says_nothing(messages: list[ModelMessage], info: AgentInfo):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {
                0: DeltaToolCall(
                    name="tell_the_user",
                    json_args=json.dumps({"update": spoken}),
                    tool_call_id="s1",
                )
            }
            return
        yield "   "

    state = await _run_turn(
        engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_speaks_then_says_nothing),
        user,
        conv,
    )

    assert [block for block in state.text_blocks() if block.strip()] == [spoken]


async def test_prose_written_after_a_tool_call_is_the_whole_of_the_answer(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ The turn the drop used to break, and the one the platform must not talk over.

    The agent reads a file and then answers. The answer reaches the citizen — prose written in
    the same response as a tool call is no longer withheld and deleted — and it is the WHOLE of
    what the turn says: no second block from the platform closing a turn that already had an
    answer in it.

    Mutation check: append any platform-authored sentence at the end of a chat run and this goes
    red on the block list, where a substring check would still find the answer inside it."""
    engine, (user, conv) = _fresh_engine, await _plan_conversation(db_session)

    async def _reads_then_answers(messages: list[ModelMessage], info: AgentInfo):
        if len(messages) == 1:
            yield {
                0: DeltaToolCall(
                    name="read_file", json_args='{"path": "app/page.tsx"}', tool_call_id="r1"
                )
            }
            return
        yield "Your visitor list shows who arrived and when."

    state = await _run_turn(
        engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_reads_then_answers),
        user,
        conv,
    )

    assert state.text_blocks() == ["Your visitor list shows who arrived and when."]
    assert "r1" in state.steps  # liveness: the read happened, and the answer came after it
