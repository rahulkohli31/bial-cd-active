"""The prefix invariant, as a test that exists before the work it accepts.

The caching layer shipped two months ago and never worked. Every check we had was green: the
code was written, the markers were set, the tests passed. None of them looked at the bytes
leaving the process on two consecutive turns. This file is that look, in three layers:

1. **The byte-prefix invariant, from index 0.** What turn N+1 sends must reproduce what turn N
   sent, message for message, up to turn N's own trailing prompt. Red today: the workspace note
   is spliced in *ahead* of the citizen's message, and the citizen's message is persisted — so
   next turn the note's bytes are gone from the middle and everything after them shifts.
2. **The wire-level marker.** `anthropic_cache_instructions` claims to place a cache breakpoint.
   Red today: with a single callable-sourced instruction part the library resolves its target
   block to `None` and places nothing.
3. **The round trip through the store.** Layer 1 with plain text proves nothing about
   production, whose transcripts carry credential-shaped strings that the persistence seam
   rewrites on the way in. Red until redaction moves off the write path.

Every layer asserts on what was *built*, never on what a helper returned — an emission-side
assertion stays green while the wire carries something else, which is this repository's
documented way of being green for the wrong reason.

All three are necessary and none is sufficient: byte-stability can hold while caching is dead
(a below-floor prefix, an out-of-lookback breakpoint, cross-request non-determinism are all
invisible here). The release gates on the live cache read, not on this file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.message import MessageEntryKind
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.messages.store import append_batch, load_history
from src.services.orchestrator.constants import CACHE_TTL
from src.services.sandbox.config import SandboxConfig
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeSandboxClient

# A value shaped like the thing the persistence seam masks. Layer 3 is only a test of the seam
# while this stays credential-shaped; plain text would pass it for the wrong reason.
_CREDENTIAL = "DATABASE_URL=postgres://u:p@h/db"

_CITIZEN = "Ada"
_PROJECT = "Visitors"
"""The two per-conversation facts this file hunts for on the wrong side of the marker.

Named rather than read back off `_CTX`: `project_name` is optional on the context now — a
chat with no project has none — so the substring searches below would be asking whether a
possible absence appears in a string."""

_CTX = PromptContext(user_name=_CITIZEN, project_name=_PROJECT, project_description=None)


async def _no_refs(attachment_ids) -> dict[str, tuple[str, str]]:
    raise AssertionError(f"unexpected rehydration of {list(attachment_ids)!r}")


async def _noop_persist() -> None:
    return None


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every turn pins the project's live container, so without this a turn dies at the
    workspace pin and every assertion below would hold over an empty list."""
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


def _per_message_bytes(messages: list[ModelMessage]) -> list[bytes]:
    """One canonical serialization per message.

    A whole-list dump cannot express "is a prefix of": the JSON array's closing bracket makes
    every shorter list differ in its final bytes. Per-message dumps compare positionally, which
    is what the invariant actually claims.
    """
    return [ModelMessagesTypeAdapter.dump_json([message]) for message in messages]


async def _wire_bytes(messages: list[ModelMessage]) -> list[bytes]:
    """What the provider will actually put in the request body, one entry at a time.

    The invariant is about the bytes leaving the process, and a `ModelMessage` dump is not
    those bytes: it carries `instructions`, a request-level `timestamp`, `run_id` and `state`,
    none of which reach the Anthropic body. Comparing dumps would fail on bookkeeping that
    differs between a freshly-built request and one rebuilt from stored rows — a red that says
    nothing about caching — and would equally hide a difference the mapper introduces.

    The mapper is also where adjacent requests fold together, which is exactly where the
    spliced note does its damage: it lands *inside* the same entry as the citizen's persisted
    prompt rather than beside it.
    """
    model = AnthropicModel("claude-sonnet-4-5", provider=AnthropicProvider(api_key="offline"))
    params = model.customize_request_parameters(ModelRequestParameters())
    _, wire = await model._map_message(messages, params, {})  # noqa: SLF001 — pinned-version seam
    return [json.dumps(entry, sort_keys=True, default=str).encode() for entry in wire]


def _first_divergence(earlier: list[bytes], later: list[bytes]) -> int | None:
    for index, (before, after) in enumerate(zip(earlier, later, strict=False)):
        if before != after:
            return index
    return None


# --- layer 1: the byte-prefix invariant, checked from index 0 -------------------------------


async def _one_turn(
    engine: TurnEngine,
    db_session,
    session_factory,
    conversation,
    user_id: uuid.UUID,
    prompt: str,
) -> list[ModelMessage]:
    """Drive one real turn against the stored conversation and return what the model was handed.

    Everything about the durability path is production's, because the store is where the
    rewriting this file hunts actually happens:

    - history is LOADED, never carried across in memory;
    - the prompt row is written by `persist_user_turn`, which the engine calls before the model
      runs, exactly as the route does;
    - the reply is written by the engine itself, off `_persistable_messages`.

    That last point is the trap. A test that appends [prompt, reply] itself after the turn writes
    the reply a second time, and the doubled row shifts every later turn's history — a divergence
    the fixture manufactured, which no implementation can remove.
    """
    history = await load_history(
        db_session, user_id=user_id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    seen: list[list[ModelMessage]] = []

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        seen.append(list(messages))
        yield "noted."

    async def _persist_prompt() -> None:
        await append_batch(
            db_session,
            user_id=user_id,
            conversation_id=conversation.id,
            messages=[ModelRequest(parts=[UserPromptPart(content=prompt)])],
            entry_kind=MessageEntryKind.TURN,
            kind=ChatKind.PLAN,
        )

    await engine.start_turn(
        conversation=conversation,
        user_id=user_id,
        prompt=prompt,
        history=history,
        prompt_context=_CTX,
        app_id=None,
        project_id=conversation.project_id,
        manager=SessionManager(),
        model=FunctionModel(stream_function=_stream),
        session_factory=session_factory,
        persist_user_turn=_persist_prompt,
        sandbox_client=FakeSandboxClient(),
    )
    state = engine.peek(conversation.id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)
    assert seen, "the model was never called — the turn died before its first request"
    return seen[0]


async def test_turn_two_reproduces_turn_one_byte_for_byte_up_to_the_new_prompt(
    _fresh_engine, db_session, session_factory
) -> None:
    """The invariant the whole state-tool track is accepted against.

    Turn N+1's history is loaded from the store, exactly as production loads it — not carried
    across in memory, because the store is where the rewriting happens. Plain text throughout,
    so the redaction seam is a no-op here and a failure means what this layer says it means;
    layer 3 is where credential-shaped text belongs.

    The failure message names the diverging index, because "a list differs" on a
    fifty-message history is not a debuggable signal.
    """
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)

    # The seed ends with an assistant reply, and that is load-bearing rather than decorative: the
    # provider's mapper folds ADJACENT requests into one wire entry, so a history ending on a user
    # prompt would merge into turn one's prompt while turn two's — separated by turn one's reply —
    # would not. The two turns have to meet the mapper in the same shape or the comparison is
    # measuring the fixture.
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="add a visitors chart")]),
            ModelResponse(parts=[TextPart(content="the chart is in.")]),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    turn_one = await _one_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "and a date filter"
    )
    turn_two = await _one_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "now group by month"
    )

    before = await _wire_bytes(turn_one)
    after = await _wire_bytes(turn_two)
    assert len(after) >= len(before), (
        f"turn two sent {len(after)} messages, fewer than turn one's {len(before)} — "
        "a shorter list cannot extend a prefix"
    )
    diverged = _first_divergence(before, after)
    assert diverged is None, (
        f"the prefix broke at message {diverged} of {len(before)}.\n"
        f"turn one sent: {before[diverged]!r}\n"
        f"turn two sent: {after[diverged]!r}"
    )


async def test_the_prefix_holds_across_a_turn_that_called_tools(
    _fresh_engine, db_session, session_factory
) -> None:
    """A turn whose history carries tool calls and their returns, followed by one that does not.

    Tool-return requests are persisted by construction, so they are the half of history that
    should already be stable. This separates "the note broke it" from "tool results broke it" —
    without it, a single red test cannot say which.
    """
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="read the page")]),
            ModelResponse(
                parts=[
                    ToolCallPart(tool_name="read_file", args={"path": "p.tsx"}, tool_call_id="a")
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="read_file", content="export default …", tool_call_id="a"
                    )
                ]
            ),
            ModelResponse(parts=[TextPart(content="read it.")]),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    turn_one = await _one_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "now add a filter"
    )
    turn_two = await _one_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "and sort it"
    )

    diverged = _first_divergence(await _wire_bytes(turn_one), await _wire_bytes(turn_two))
    assert diverged is None, f"the prefix broke at message {diverged} on a tool-carrying history"


# --- layer 2: the marker on the built request, not on the setting ---------------------------


async def _system_blocks_our_agent_builds(
    engine: TurnEngine, db_session, session_factory
) -> list[dict[str, object]]:
    """Run a real turn, take the instruction parts OUR agent produced, and map them the way the
    provider will.

    The parts have to come off `AgentInfo.model_request_parameters` rather than be handed in.
    A parts list assembled by the test is a test of the library's marker arithmetic — which
    works — and says nothing about whether our agent gives it anything to mark. That is the
    difference between this failing today and passing today for the wrong reason.
    """
    captured: list[ModelRequestParameters] = []

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        captured.append(info.model_request_parameters)
        yield "noted."

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="add a visitors chart",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=conv.project_id,
        manager=SessionManager(),
        model=FunctionModel(stream_function=_stream),
        session_factory=session_factory,
        persist_user_turn=_noop_persist,
        sandbox_client=FakeSandboxClient(),
    )
    state = engine.peek(conv.id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)
    assert captured, "the model was never called — no request parameters to map"

    model = AnthropicModel("claude-sonnet-4-5", provider=AnthropicProvider(api_key="offline"))
    params = model.customize_request_parameters(
        ModelRequestParameters(instruction_parts=captured[0].instruction_parts)
    )
    system, _ = await model._map_message(  # noqa: SLF001 — pinned-version seam
        [ModelRequest(parts=[UserPromptPart(content="add a visitors chart")])],
        params,
        {"anthropic_cache_instructions": CACHE_TTL},
    )
    if not isinstance(system, list):
        return []
    return [dict(block) for block in system]


async def test_our_agents_instructions_carry_a_cache_marker_on_the_wire(
    _fresh_engine, db_session, session_factory
) -> None:
    """The marker `anthropic_cache_instructions` claims to place, asserted on the request our
    own agent builds.

    It lands on the LAST STATIC block, which is where the library puts it — not on block 0.
    Pinning block 0 would pin a position the library never uses.

    Red today, and the reason is ours rather than the library's: `chat_agent` is built with no
    static system prompt and a single callable-sourced instruction, so every part comes through
    `dynamic=True`, the library resolves its target block to `None`, and nothing is marked.
    """
    blocks = await _system_blocks_our_agent_builds(_fresh_engine, db_session, session_factory)
    marked = [index for index, block in enumerate(blocks) if "cache_control" in block]
    assert marked, (
        "no cache_control reached the request — every instruction part our agent contributes "
        "is dynamic, so the setting placed nothing"
    )
    assert blocks[marked[0]]["cache_control"] == {"type": "ephemeral", "ttl": CACHE_TTL}


async def test_the_marker_sits_after_the_last_static_instruction(
    _fresh_engine, db_session, session_factory
) -> None:
    """Position, not just presence.

    A marker on a block that still carries per-conversation text caches nothing across two
    citizens, so "a marker landed" is not the property — "it landed where everything before it
    is identical next turn" is.
    """
    captured: list[ModelRequestParameters] = []

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        captured.append(info.model_request_parameters)
        yield "noted."

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    await _fresh_engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="add a visitors chart",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=conv.project_id,
        manager=SessionManager(),
        model=FunctionModel(stream_function=_stream),
        session_factory=session_factory,
        persist_user_turn=_noop_persist,
        sandbox_client=FakeSandboxClient(),
    )
    state = _fresh_engine.peek(conv.id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)

    parts = captured[0].instruction_parts or []
    static_count = sum(1 for part in parts if not part.dynamic)
    assert static_count, (
        "our agent contributes no static instruction part at all, so there is no stable block "
        "for a marker to sit after"
    )
    blocks = await _system_blocks_our_agent_builds(_fresh_engine, db_session, session_factory)
    marked = [index for index, block in enumerate(blocks) if "cache_control" in block]
    assert marked == [static_count - 1], (
        f"the marker sits on block(s) {marked}; the last static block is {static_count - 1}"
    )


async def test_no_block_up_to_the_marker_names_this_citizen_or_their_project(
    _fresh_engine, db_session, session_factory
) -> None:
    """The property the index arithmetic only stands in for.

    `test_the_marker_sits_after_the_last_static_instruction` compares the marker's index to a
    COUNT of static parts, which agrees with position only while the library orders static
    parts first — an assumption about someone else's library, checked nowhere. Asked directly
    here instead: the citizen's name and their project's name are what differ between two
    people on the same kind, so finding either at or before the marker means the cached prefix
    is theirs alone and no second citizen can ever hit it.

    Mutation check: order the dynamic tail ahead of the contract and this goes red on block 0
    while the count-based test above can still agree with itself."""
    blocks = await _system_blocks_our_agent_builds(_fresh_engine, db_session, session_factory)
    marked = [index for index, block in enumerate(blocks) if "cache_control" in block]
    assert marked, "no marker, so there is no prefix to make claims about"

    cached = [str(block.get("text", "")) for block in blocks[: marked[0] + 1]]
    for index, text in enumerate(cached):
        assert _CITIZEN not in text, f"block {index} names the citizen: {text[:120]!r}"
        assert _PROJECT not in text, f"block {index} names the project: {text[:120]!r}"

    tail = "\n".join(str(block.get("text", "")) for block in blocks[marked[0] + 1 :])
    assert _CITIZEN in tail and _PROJECT in tail, (
        "this conversation's own facts are not behind the marker at all — either they went "
        "missing from the prompt, or they were folded into the cached prefix"
    )


# --- layer 3: the round trip through the store, not two in-memory histories -----------------


async def test_a_credential_shaped_tool_result_replays_exactly_as_it_was_sent(
    db_session,
) -> None:
    """The layer that stops layer 1 being green on fixtures production does not resemble.

    A build turn that read a `.env` or echoed a `*_KEY=` line sends raw bytes on the wire and
    replays masked ones out of the store, so the prefix breaks at the persistence seam and no
    plain-text fixture will ever show it.

    Red until masking moves off the write path and onto the display path. Green after, with one
    stated exception this fixture deliberately avoids: the relocation and dedupe that dangling-
    call repair performs on load is bounded and accepted, so the history here is one that does
    not trigger it.
    """
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    sent: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="read the env file")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="read_file", args={"path": ".env"}, tool_call_id="a")]
        ),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="read_file", content=_CREDENTIAL, tool_call_id="a")]
        ),
        ModelResponse(parts=[TextPart(content="found it.")]),
    ]
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        messages=sent,
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    replayed = await load_history(
        db_session, user_id=user.id, conversation_id=conv.id, rehydrate=_no_refs
    )
    assert _per_message_bytes(replayed) == _per_message_bytes(sent), (
        "the store did not return what it was given — the replayed prefix is not the prefix"
    )
