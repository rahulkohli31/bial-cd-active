"""THE PER-TURN RESTATEMENT IS GONE — this file is its inertness guard.

A cadence used to restate "which mode you are in" (full reminder every 8th turn, nudge every
4th) anchored on mode-switch markers. It went because a chat's kind is now fixed at creation —
there is no mode to be in, and the ABILITIES available already carry "which chat this is". The
delivery was also the wrong tier: a `user`-role message on a per-turn cadence, a named
cache-breaking action.

This file stays as the removal trace's last link: a long conversation runs through the real
engine and no restatement rides it. The workspace note that once used the same envelope is gone
too — the agent pulls that fact with `check_the_app` — so the guard is now a COUNT of what
reached the model rather than a search for wording, which is what stops "nothing was injected at
all" from satisfying it for the wrong reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import (
    CachePoint,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.profiles import ModelProfile

from src.config import settings
from src.db.models.conversation import ChatKind
from src.services.agent import mode_prompts
from src.services.agent import toolsets as toolsets_module
from src.services.agent.capabilities import PULL_THE_APP_STATE
from src.services.agent.conversation_tools import _SHOWN
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.messages.store import load_history
from src.services.orchestrator.constants import CACHE_TTL
from src.services.orchestrator.selfheal import AppState
from src.services.sandbox.config import SandboxConfig
from src.services.turns import engine as engine_module
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeSandboxClient

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every kind pins the project's LIVE container now — a Plan turn attaches a sandbox
    exactly like a Build turn — so a turn dies at the workspace pin before the model ever runs
    unless a deployment is configured. Same wiring `test_engine.py` and `test_write_turn.py`
    carry, for the same reason.

    THIS IS WHAT MAKES THE GUARD BELOW HONEST rather than vacuously green: without it every
    assertion about "what reached the model" would be asserting over an empty list, because
    nothing reached the model at all."""
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
    """The liveness lease (Redis) and the attach's storage reads both need a backing fake
    now that every turn attaches a live container. One deployment fact, two fixtures."""
    return None


# What the retired cadence's anchors were. Kept as literals rather than imported — importing
# them is what this file exists to prove is impossible — so "run a long conversation" means the
# same lengths it always did, including the two that used to be guaranteed to speak.
_RETIRED_NUDGE_EVERY = 4
_RETIRED_FULL_EVERY = 8


def _turn(n: int) -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content=f"question {n}")]),
        ModelResponse(parts=[TextPart(content=f"answer {n}")]),
    ]


def _turns(count: int) -> list[ModelMessage]:
    return [message for n in range(count) for message in _turn(n)]


# --- the symbols themselves ------------------------------------------------------------


def test_no_restatement_machinery_survives_anywhere() -> None:
    """The named symbols are gone from both modules that carried them.

    Named one by one rather than as a `dir()` sweep: each of these was a separate decision to
    delete, and an implementer working from a "six reminders" description would leave the two
    HOLDING variants behind, unreferenced, where nothing would ever notice them."""
    for retired in (
        "mode_reminder",
        "_ASK_SEGMENT",
        "_ASK_REMINDER_FULL",
        "_ASK_REMINDER_NUDGE",
        "_PLAN_REMINDER_FULL",
        "_PLAN_REMINDER_NUDGE",
        "_PLAN_REMINDER_FULL_HOLDING",
        "_PLAN_REMINDER_NUDGE_HOLDING",
        "_WRITE_REMINDER_FULL",
        "_WRITE_REMINDER_NUDGE",
        # The workspace note went the same way and for a related reason: it was a `user`-role
        # message composed per turn and spliced ahead of the citizen's prompt. The agent pulls
        # the fact with `check_the_app` instead.
        "_PRIVATE",
        "workspace_note",
        "_WORKSPACE_NOTE_HEAD",
        "_WORKSPACE_NOTE_TAIL",
        "_WORKSPACE_UNKNOWN",
        "_WORKSPACE_NOT_SERVING",
        "_WORKSPACE_STILL_TEMPLATE",
        "_WORKSPACE_LIVE",
    ):
        assert not hasattr(mode_prompts, retired), retired
    for retired in (
        "_reminder_text",
        "REMINDER_FULL_EVERY",
        "REMINDER_NUDGE_EVERY",
        "_turns_since_mode_anchor",
        "_MODE_MARKER_PREFIX",
    ):
        assert not hasattr(engine_module, retired), retired


# --- the engine seam -------------------------------------------------------------------


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


async def _noop_persist() -> None:
    return None


async def _settle(engine: TurnEngine, conversation_id: uuid.UUID) -> None:
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


async def _run_with_history(
    engine: TurnEngine,
    db_session,
    session_factory,
    history: list[ModelMessage],
) -> tuple[list[list[ModelMessage]], uuid.UUID]:
    """One Plan turn through the real engine with a capturing model: returns every
    request-message list the model saw, and the conversation id.

    Run through the real engine rather than by calling the injection helper directly, because
    the thing being proved is what reaches the model — not what a helper returns."""
    seen: list[list[ModelMessage]] = []

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        seen.append(list(messages))
        yield "noted."

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="and one more thing",
        history=history,
        prompt_context=_CTX,
        app_id=None,
        project_id=conv.project_id,
        manager=SessionManager(),
        model=FunctionModel(stream_function=_stream),
        session_factory=session_factory,
        persist_user_turn=_noop_persist,
        sandbox_client=FakeSandboxClient(),
    )
    await _settle(engine, conv.id)
    return seen, conv.id


def _injected_prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]


@pytest.mark.parametrize("length", [0, 1, 3, _RETIRED_NUDGE_EVERY, _RETIRED_FULL_EVERY, 40])
async def test_no_turn_at_any_length_carries_a_restatement(
    _fresh_engine, db_session, session_factory, length: int
) -> None:
    """Including at both retired anchors, and including a forty-turn conversation.

    The two middle lengths are the ones that used to be GUARANTEED to speak — a nudge at four,
    a full restatement at eight — so a cadence left half-deleted would still fire here. Forty
    covers the "long conversation" the cadence existed for in the first place."""
    seen, _ = await _run_with_history(_fresh_engine, db_session, session_factory, _turns(length))
    dumped = ModelMessagesTypeAdapter.dump_json(seen[0]).decode()
    assert "mode is active" not in dumped
    assert "Plan mode" not in dumped
    assert "Write mode" not in dumped
    assert "Ask mode" not in dumped


async def test_nothing_at_all_is_spliced_between_the_history_and_the_citizens_prompt(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ THE PROPERTY THE WHOLE CACHING TRACK RESTS ON, at what used to be a cadence anchor: the
    request the model is handed is the stored history and this turn's prompt, and nothing else.

    A COUNT, NOT A SUBSTRING SEARCH. "No note" is satisfied by a note whose wording changed;
    "the history's own turns plus exactly one more, and that one is the prompt" is not. Anything
    appended at the tail of `message_history` sits AHEAD of the citizen's prompt — and the
    citizen's prompt is persisted while the appendage is not, so next turn the bytes vanish from
    the middle of the history and every message after them shifts.

    LIVENESS IS THE SAME ASSERTION. The count is `_RETIRED_FULL_EVERY + 1`, so a turn that
    reached the model with nothing at all fails it exactly as a turn that reached it with one
    thing too many does."""
    seen, _ = await _run_with_history(
        _fresh_engine, db_session, session_factory, _turns(_RETIRED_FULL_EVERY)
    )
    prompts = _injected_prompts(seen[0])
    assert prompts[-1] == "and one more thing"
    assert len(prompts) == _RETIRED_FULL_EVERY + 1, (
        f"the history's {_RETIRED_FULL_EVERY} turns plus this one's prompt is "
        f"{_RETIRED_FULL_EVERY + 1} user messages; the model was handed {prompts!r}"
    )


def test_a_system_prompt_bearing_request_is_never_persisted() -> None:
    """★ THE DOOR THE USER-PROMPT RULE DOES NOT CLOSE, guarded before anything walks through it.

    A turn-scoped system message is ephemeral by intent, but unlike injected history it would
    arrive on the run's OWN `new_messages()` — so the user-prompt exclusion never sees it and it
    lands in a row. Replayed next turn, pydantic-ai hoists the opening system parts of the first
    request into the top-level `system` field: the message changes tier AND position on rebuild,
    which is the prefix divergence this whole track exists to remove.

    The tool-return request beside it is the liveness half — a predicate that dropped everything
    would satisfy the first assertion and quietly stop persisting real results.

    Mutation check: drop `SystemPromptPart` from the predicate's exclusion and the first
    assertion goes red."""
    turn_scoped = ModelRequest(parts=[SystemPromptPart(content="the app is serving right now")])
    a_result = ModelRequest(
        parts=[ToolReturnPart(tool_name="read_file", content="export default", tool_call_id="a")]
    )
    kept = engine_module._persistable_messages([turn_scoped, a_result])
    assert turn_scoped not in kept, "a turn-scoped system message fossilized into the transcript"
    assert kept == [a_result]


# --- the turn-scoped system message ----------------------------------------------------
#
# The retired cadence above and this one are opposites, which is why they share a file: that
# was a `user`-role restatement on a fixed count, this is one operator sentence at the tail of
# a request, sent only where the turn has demonstrably stopped looking at the app. What follows
# proves the difference is real — no turn gets one for merely existing, and the turn that has
# stopped looking gets exactly one.

_SAYS_SOMETHING = [("tell_the_user", '{"update": "starting on it."}')]
_LOOKS = [("check_the_app", "{}")]


def _capturing(
    script: list[list[tuple[str, str]] | str], *, inline: bool
) -> tuple[FunctionModel, list[list[ModelMessage]]]:
    """A model that replays `script` one entry per request, capturing what it was handed.

    `inline` is whether this model can serve a mid-conversation `{'role': 'system'}` entry. It
    has to be a knob rather than a constant: production's Foundry deployment can, every other
    model in this suite cannot, and the emitter is required to stay silent on the ones that
    cannot — where the library folds the sentence into the citizen's own message instead."""
    seen: list[list[ModelMessage]] = []
    step = iter(script)

    async def _stream(messages: list[ModelMessage], _info: AgentInfo):
        seen.append(list(messages))
        entry = next(step, "done.")
        if isinstance(entry, str):
            yield entry
            return
        yield DeltaToolCalls(
            {
                index: DeltaToolCall(
                    name=name, json_args=args, tool_call_id=f"c{len(seen)}-{index}"
                )
                for index, (name, args) in enumerate(entry)
            }
        )

    model = FunctionModel(
        stream_function=_stream,
        profile=ModelProfile(supports_inline_system_prompts=True) if inline else None,
    )
    return model, seen


async def _no_rehydration(attachment_ids) -> dict[str, tuple[str, str]]:
    raise AssertionError(f"unexpected rehydration of {list(attachment_ids)!r}")


async def _run_scripted(
    engine: TurnEngine, db_session, session_factory, model: FunctionModel
) -> tuple[uuid.UUID, uuid.UUID]:
    """One real Plan turn against a fresh conversation, driven by a scripted model."""
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="is the date filter working now?",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=conv.project_id,
        manager=SessionManager(),
        model=model,
        session_factory=session_factory,
        persist_user_turn=_noop_persist,
        sandbox_client=FakeSandboxClient(),
    )
    await _settle(engine, conv.id)
    return conv.id, user.id


def _system_parts(messages: list[ModelMessage]) -> list[str]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, SystemPromptPart)
    ]


@pytest.fixture
def scripted_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """What `check_the_app` finds, with no container to find it in.

    Patched at the probe rather than at the tool, so the tool's own plumbing runs — including
    the callback that hands the reading to the turn, which IS the trigger's only input."""

    async def _read(*_args, **_kwargs) -> AppState:
        return AppState.LIVE

    monkeypatch.setattr(toolsets_module, "read_the_app_state", _read)


async def test_the_opening_request_of_a_turn_never_carries_the_sentence(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ WHAT SEPARATES A DETECTED LAPSE FROM A CADENCE.

    Every turn starts with no reading on record, so a trigger that only asked "has this turn
    looked?" answers no on every opening request and fires on every turn — the blind every-N
    reminder this work exists to not build, with N of one. The turn has to have been given the
    chance to look before not having looked means anything."""
    model, seen = _capturing([_SAYS_SOMETHING, "there you go."], inline=True)
    await _run_scripted(_fresh_engine, db_session, session_factory, model)
    assert _system_parts(seen[0]) == []


async def test_a_turn_that_stopped_looking_is_handed_exactly_one(
    _fresh_engine, db_session, session_factory
) -> None:
    """The lapse: the turn ran a tool, none of it read the app, and the next request says so.

    POSITION IS THE PROPERTY, not presence. The sentence is the last part of the last request —
    after the tool results, never ahead of the citizen's persisted words — and the part
    immediately before it is the cache pin, which is what stops the next request of the same
    turn from losing the prefix at the byte where this one ends."""
    model, seen = _capturing([_SAYS_SOMETHING, "there you go."], inline=True)
    await _run_scripted(_fresh_engine, db_session, session_factory, model)

    assert len(seen) == 2, f"the script makes two requests; the model saw {len(seen)}"
    assert _system_parts(seen[1]) == [PULL_THE_APP_STATE]
    tail = seen[1][-1]
    assert isinstance(tail, ModelRequest)
    assert isinstance(tail.parts[-1], SystemPromptPart)
    pin = tail.parts[-2]
    assert isinstance(pin, UserPromptPart)
    assert pin.content == [CachePoint(ttl=CACHE_TTL)]


async def test_a_turn_that_called_check_the_app_is_told_nothing(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """A reading exists this turn, so there is no lapse to report.

    Driven through the REGISTERED TOOL rather than by setting the turn's state: the fact under
    test is that the trigger reads what the model was actually handed, and a test that assigns
    the reading itself proves only that a dataclass holds values."""
    model, seen = _capturing([_LOOKS, "it is serving."], inline=True)
    await _run_scripted(_fresh_engine, db_session, session_factory, model)

    assert len(seen) == 2
    assert _system_parts(seen[1]) == []


async def test_one_sentence_covers_a_turn_however_many_requests_it_makes(
    _fresh_engine, db_session, session_factory
) -> None:
    """The cooldown, on the shape that would otherwise pay for it at every step.

    A sentence on every request moves the bytes the NEXT request of the same turn has to
    reproduce, so a long turn would re-break its own prefix step after step. Counted over every
    request rather than checked on the last: a second copy on request four is exactly as
    damaging as one on request two, and no easier to notice."""
    model, seen = _capturing(
        [_SAYS_SOMETHING, _SAYS_SOMETHING, _SAYS_SOMETHING, "all done."], inline=True
    )
    await _run_scripted(_fresh_engine, db_session, session_factory, model)

    assert len(seen) == 4
    carried = [index for index, request in enumerate(seen) if _system_parts(request)]
    assert carried == [1], f"the sentence rode requests {carried}; only request 1 was expected"


async def test_a_model_that_cannot_serve_a_system_entry_is_handed_nothing(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ FAIL CLOSED. On a model without the inline-system capability the library does not drop
    a mid-conversation system part — it rewrites it as `<system>`-tagged text inside the user
    message ahead of it, which is ephemeral content spliced into the citizen's own persisted
    words. Both halves are asserted, because the absence of a system part alone passes on
    exactly the arrangement this guard exists to prevent."""
    model, seen = _capturing([_SAYS_SOMETHING, "there you go."], inline=False)
    await _run_scripted(_fresh_engine, db_session, session_factory, model)

    assert len(seen) == 2
    assert _system_parts(seen[1]) == []
    assert "<system>" not in ModelMessagesTypeAdapter.dump_json(seen[1]).decode()


async def test_the_sentence_never_reaches_a_stored_row(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ THE EMITTER'S OWN HALF of the never-persisted property.

    The predicate test above pins the door a future emitter would walk through by splicing into
    the run's own new messages. This is the other question, about the emitter that exists: it
    writes into the list built for the wire, after the framework has already copied the run's
    history back, so no row can carry the sentence.

    THE TOOL RESULT IS THE LIVENESS HALF, AND IT IS THE MUTATION TOO. Moved to the earlier
    hook, the sentence lands on the request that carries the tool results — and the predicate
    then refuses that whole request, so the results never reach a row and the calls they answer
    are left dangling.

    WHICH IS WHY THE ASSERTION IS ON THE RESULT'S TEXT. Neither the tool's NAME nor the mere
    presence of a return distinguishes the two worlds: the name is in the model's call as well,
    and a dropped result leaves a dangling call that the loader REPAIRS by synthesizing a
    stand-in return under the same name. Only the answer the tool actually gave is proof the
    real row survived."""
    model, seen = _capturing([_SAYS_SOMETHING, "there you go."], inline=True)
    conversation_id, user_id = await _run_scripted(
        _fresh_engine, db_session, session_factory, model
    )
    assert _system_parts(seen[1]) == [PULL_THE_APP_STATE]

    stored = await load_history(
        db_session, user_id=user_id, conversation_id=conversation_id, rehydrate=_no_rehydration
    )
    assert PULL_THE_APP_STATE not in ModelMessagesTypeAdapter.dump_json(stored).decode()
    returns = [
        (part.tool_name, part.content)
        for message in stored
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert returns == [("tell_the_user", _SHOWN)], (
        f"the turn's tool results came back as {returns!r} — the request carrying them never "
        "reached a row, and what is here is the loader's repair of the call it orphaned"
    )
