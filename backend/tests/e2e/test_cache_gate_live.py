"""THE RELEASE GATE: the cache is either being read on real turns or it is not.

Every offline check of the caching work can be green while nothing is cached. A prefix can be
byte-stable and still sit below the provider's minimum cacheable length; a breakpoint can be
placed and still fall outside the lookback window; two requests can serialize identically here
and differ on the wire. None of that is visible to a unit test, and all of it has happened.

So the release gates on THIS, run by hand against a real deployment, with its output pasted into
the release PR:

  1. the first turn WRITES a cache entry;
  2. every turn after it READS one, and the reads do not shrink as the conversation grows;
  3. each request of a turn EXTENDS the previous one rather than rewriting it, message for
     message, up to the previous request's own trailing message.

The third is the live counterpart of `tests/services/turns/test_prefix_stability.py` layer 1.
Offline it is asserted over a serialization built in-process; here it is asserted over the
messages the adapter is actually handed, on a real model, so a divergence introduced anywhere
between the store and the provider shows up.

WHAT THIS GATE DOES NOT ASSERT, and why it is a gap rather than an omission: that the response's
`input + cache_creation + cache_read` equals the whole rendered prompt. There is no independent
measure of "the whole rendered prompt" short of a `count_tokens` round trip per call, which
doubles the requests this gate makes and which nothing in this repository exercises. An
assertion nobody has ever seen pass is not a gate.

TO RUN (the lane's usual preamble, plus real Foundry credentials in the environment):

    docker build -t bial-sandbox:e2e -f Dockerfile.sandbox ../sandbox
    docker compose -f docker-compose.test.yml up -d
    docker run -d --name bial-redis -p 6379:6379 redis:7-alpine
    uv run pytest tests/e2e/test_cache_gate_live.py -m integration -s
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
import structlog
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.models.anthropic import AnthropicModel

from src.config import settings
from src.db.models.conversation import ChatKind
from src.services.agent.mode_prompts import PromptContext
from src.services.agent.model import build_foundry_model
from src.services.build_sessions.manager import SessionManager
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

pytestmark = pytest.mark.integration

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)

# Three ordinary follow-ups on one app. Deliberately small asks: the gate is about what the
# provider does with a growing prefix, and a turn that takes ten minutes to build something
# tells us nothing extra about that.
_TURNS = (
    "add a page that lists visitors",
    "give the list a search box",
    "sort the list by arrival time",
)

_CACHE_EVENT = "model_call_cache_tokens"


@pytest.fixture
def foundry_model() -> AnthropicModel:
    """The real model, or a clean skip. This lane runs by hand; it must never fail for the want
    of a credential rather than for the want of a cache."""
    if settings.foundry is None:
        pytest.skip("FOUNDRY__* is unset — the live cache gate needs a real deployment")
    return build_foundry_model(settings.foundry)


@pytest.fixture
def requests_sent(monkeypatch: pytest.MonkeyPatch) -> list[list[ModelMessage]]:
    """Every message list the Anthropic adapter is handed, in order, across every turn.

    Recorded at `_map_message` rather than at the engine: this is the last point before the
    request becomes wire bytes, so it sees the turn-scoped tail the engine never holds."""
    recorded: list[list[ModelMessage]] = []
    original = AnthropicModel._map_message  # noqa: SLF001 — pinned-version seam

    async def _recording(self, messages, *args, **kwargs):
        recorded.append(list(messages))
        return await original(self, messages, *args, **kwargs)

    monkeypatch.setattr(AnthropicModel, "_map_message", _recording)
    return recorded


@pytest.fixture
def session_factory(db_session):
    """The engine opens its own session per turn; here every turn shares the test's.

    Defined in this file rather than a conftest because that is where every other suite driving
    `start_turn` keeps it — and without it the lane errors at fixture resolution, which happens
    BEFORE `foundry_model` can skip, so a missing credential would read as a broken gate.
    """

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    return lambda: _session()


@pytest.fixture
def engine() -> Iterator[TurnEngine]:
    turn_engine = TurnEngine()
    set_turn_engine_for_tests(turn_engine)
    yield turn_engine
    set_turn_engine_for_tests(None)


def _count(event: dict[str, Any], key: str) -> int:
    """One raw counter off a logged event. The cast is the log's price: a structlog event is a
    bag of `Any`, and the three numbers this gate reads are integers the emitter put there."""
    return int(cast(int, event[key]))


def _fingerprints(messages: list[ModelMessage]) -> list[str]:
    """One hash per message, so "is a prefix of" is expressible.

    A hash of the whole list cannot answer that question — every shorter list differs in its
    final bytes — and the claim being made is positional."""
    return [
        hashlib.sha256(ModelMessagesTypeAdapter.dump_json([message])).hexdigest()
        for message in messages
    ]


async def _one_turn(
    engine: TurnEngine,
    db_session,
    session_factory,
    conversation,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    prompt: str,
    model: AnthropicModel,
    sandbox,
) -> list[dict[str, Any]]:
    """Drive one real turn and return the cache events it logged, in call order."""

    async def _noop() -> None:
        return None

    with structlog.testing.capture_logs() as captured:
        await engine.start_turn(
            conversation=conversation,
            user_id=user_id,
            prompt=prompt,
            history=[],
            prompt_context=_CTX,
            app_id=None,
            project_id=project_id,
            manager=SessionManager(),
            model=model,
            session_factory=session_factory,
            persist_user_turn=_noop,
            sandbox_client=sandbox,
        )
        state = engine.peek(conversation.id)
        assert state is not None and state.task is not None
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(state.task, timeout=900)
    return [dict(event) for event in captured if event.get("event") == _CACHE_EVENT]


async def test_a_real_conversation_writes_then_reads_its_cache(
    engine: TurnEngine,
    db_session,
    session_factory,
    foundry_model: AnthropicModel,
    requests_sent: list[list[ModelMessage]],
    live_redis,
    live_storage,
    sandbox,
) -> None:
    """★ THE GATE. Three consecutive turns on one conversation, against the real deployment."""
    user = await UserFactory.create(db_session, email="cache-gate@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(
        db_session, user.id, kind=ChatKind.BUILD, project_id=project.id
    )

    per_turn: list[list[dict[str, Any]]] = []
    for prompt in _TURNS:
        per_turn.append(
            await _one_turn(
                engine,
                db_session,
                session_factory,
                conversation,
                user.id,
                project.id,
                prompt,
                foundry_model,
                sandbox,
            )
        )

    for index, events in enumerate(per_turn):
        assert events, f"turn {index + 1} made no model call — nothing to gate on"

    written = sum(_count(event, "cache_creation_input_tokens") for event in per_turn[0])
    assert written > 0, (
        f"turn 1 wrote no cache entry at all: {per_turn[0]!r}. The prefix is below the "
        "provider's minimum cacheable length, or no breakpoint reached the request."
    )

    reads = [
        max(_count(event, "cache_read_input_tokens") for event in events) for events in per_turn
    ]
    for index, read in enumerate(reads[1:], start=2):
        assert read > 0, (
            f"turn {index} read nothing back (reads per turn: {reads}). Something ahead of the "
            "furthest breakpoint changed between turns."
        )
    assert reads[1:] == sorted(reads[1:]), (
        f"cache reads shrank as the conversation grew: {reads}. A prefix that stops growing is "
        "a prefix that stopped matching."
    )

    fingerprints = [_fingerprints(messages) for messages in requests_sent]
    for index in range(1, len(fingerprints)):
        earlier, later = fingerprints[index - 1], fingerprints[index]
        # Up to the PREVIOUS request's last message and no further: that message is the one
        # carrying this turn's prompt or the ephemeral tail, and neither is claimed to survive
        # into the next request unchanged. Everything before it is.
        shared = earlier[:-1]
        diverged = next(
            (i for i, (a, b) in enumerate(zip(shared, later, strict=False)) if a != b), None
        )
        assert later[: len(shared)] == shared, (
            f"request {index + 1} rewrote the prefix request {index} sent: they first differ "
            f"at message {diverged}"
        )
