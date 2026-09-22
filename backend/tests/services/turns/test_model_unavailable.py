"""A model service that failed mid-turn, named — and every unexplained failure, recorded.

WHY THIS EXISTS. On 2026-09-11 a Write turn ended through the broad `except Exception` with "The
assistant hit a problem" and `reason: null`. The only record of the exception was the
`turn_run_failed` log line, in an archive nobody on the team can read, and Foundry's metrics for
the minute showed no error of any kind — so the cause could not be recovered. Two things follow,
and both are pinned here:

1. A failure the model service owns — a status the SDK already retried, or a `ModelAPIError` (a
   connection that never answered, or a stream that ended in an error event) — ends with the named
   reason `MODEL_UNAVAILABLE_CODE` and a sentence that says what to do. Placed INSIDE the
   `ModelHTTPError` arm after the overflow match, and in its own `ModelAPIError` arm after that.
2. Every failed turn the platform does not otherwise explain writes the exception's CLASS CHAIN
   onto its terminal row as `meta.error` — never its message, which can carry bound parameters.
"""

from __future__ import annotations

import contextlib
import inspect
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.api.v1.conversations.schemas import TurnEndedFrame, TurnErrorFrame
from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.sandbox.config import SandboxConfig
from src.services.turns import engine as engine_module
from src.services.turns.copy import (
    CHAT_TOO_LONG_CODE,
    MODEL_UNAVAILABLE_CODE,
    MODEL_UNAVAILABLE_TEXT,
    MODEL_UNAVAILABLE_WITHOUT_A_WORKSPACE_TEXT,
)
from src.services.turns.engine import (
    TurnEngine,
    _is_transient_model_status,
    set_turn_engine_for_tests,
)
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeSandboxClient

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)

OVERFLOW_BODY = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "prompt is too long: 1963668 tokens > 1000000 maximum",
    },
}

# The shape the Anthropic stream carries when it ends in an error event after an HTTP 200.
OVERLOADED_BODY = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _refusing_model(exc: Exception) -> FunctionModel:
    async def _stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        raise exc
        yield ""  # pragma: no cover

    return FunctionModel(stream_function=_stream)


def _answering_model() -> FunctionModel:
    async def _stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        yield "Here is a plan."

    return FunctionModel(stream_function=_stream)


async def _start(engine: TurnEngine, db_session, session_factory, model, *, kind=ChatKind.PLAN):
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=kind)

    async def _noop_persist() -> None:
        return None

    turn_id = await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="add a chart to the dashboard",
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
    return conv.id, turn_id, state


async def _run_until_settled(
    engine: TurnEngine, db_session, session_factory, model, *, kind=ChatKind.PLAN
):
    conv_id, _turn_id, state = await _start(engine, db_session, session_factory, model, kind=kind)
    task = state.task
    assert task is not None
    with contextlib.suppress(BaseException):
        await task
    return conv_id, state


def _last_error(state) -> str | None:
    frames = [frame for frame in state.ring if isinstance(frame, TurnErrorFrame)]
    return frames[-1].message if frames else None


def _terminal(state) -> TurnEndedFrame:
    frames = [frame for frame in state.ring if isinstance(frame, TurnEndedFrame)]
    assert len(frames) == 1, f"expected exactly one terminal, got {len(frames)}"
    return frames[-1]


async def _terminal_meta(db_session, conv_id: uuid.UUID) -> dict:
    rows = (
        await db_session.scalars(
            sa.select(Message).where(
                Message.conversation_id == conv_id,
                Message.entry_kind == MessageEntryKind.SYSTEM_EVENT,
            )
        )
    ).all()
    terminals = [row.meta for row in rows if row.meta and row.meta.get("kind") == "turn_terminal"]
    assert len(terminals) == 1, f"expected one terminal row, got {len(terminals)}"
    return terminals[0]


# --- the named ending -----------------------------------------------------------------------


def test_transient_statuses_are_exactly_the_ones_the_sdk_retries() -> None:
    """The engine names a failure the model service's only where the SDK already tried again:
    408, 409, 429 and every 5xx (`_should_retry` in `anthropic._base_client`)."""
    for code in (408, 409, 429, 500, 502, 503, 529):
        assert _is_transient_model_status(code), code
    for code in (400, 401, 403, 404, 413, 422):
        assert not _is_transient_model_status(code), code


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(
            ModelHTTPError(status_code=429, model_name="opus", body="rate limit exceeded"),
            id="rate-limited-past-every-retry",
        ),
        pytest.param(
            ModelHTTPError(status_code=529, model_name="opus", body=OVERLOADED_BODY),
            id="provider-overloaded",
        ),
        pytest.param(
            ModelAPIError(model_name="opus", message="connection reset by peer"),
            id="socket-never-answered-or-stream-error-event",
        ),
    ],
)
async def test_a_model_service_failure_ends_with_a_named_reason_and_a_way_forward(
    _fresh_engine, db_session, session_factory, exc: Exception
) -> None:
    """★ Not "the assistant hit a problem". Mutation check: delete either new arm and its cases
    go red on the generic sentence."""
    conv_id, state = await _run_until_settled(
        _fresh_engine, db_session, session_factory, _refusing_model(exc)
    )

    assert state.status == "failed"
    assert _terminal(state).reason == MODEL_UNAVAILABLE_CODE
    assert _last_error(state) == MODEL_UNAVAILABLE_WITHOUT_A_WORKSPACE_TEXT
    assert _last_error(state) != engine_module._TURN_FAILED_MESSAGE
    assert conv_id not in _mid_reply


async def test_the_sentence_names_no_status_no_provider_and_no_jargon(
    _fresh_engine, db_session, session_factory
) -> None:
    _conv_id, state = await _run_until_settled(
        _fresh_engine,
        db_session,
        session_factory,
        _refusing_model(ModelHTTPError(status_code=429, model_name="opus", body="rate limit")),
    )
    message = _last_error(state)
    assert message is not None
    for leak in ("429", "opus", "rate limit", "token", "request", "api", "http", "retry"):
        assert leak not in message.lower(), f"the citizen was shown {leak!r}"


async def test_the_overflow_still_wins_over_the_named_ending(
    _fresh_engine, db_session, session_factory
) -> None:
    """A 400 is never transient, so the order cannot matter today — pinned anyway, because the
    day someone widens the transient table to 4xx is the day the overflow sentence disappears."""
    _conv_id, state = await _run_until_settled(
        _fresh_engine,
        db_session,
        session_factory,
        _refusing_model(ModelHTTPError(status_code=400, model_name="opus", body=OVERFLOW_BODY)),
    )
    assert _terminal(state).reason == CHAT_TOO_LONG_CODE


def test_the_arms_are_ordered_http_then_api_then_everything_else() -> None:
    """`ModelHTTPError` IS a `ModelAPIError`, which is an `Exception`: Python accepts the three
    handlers in any order and says nothing when one is unreachable."""
    source = inspect.getsource(TurnEngine._run_turn)
    http, api, broad = (
        source.index("except ModelHTTPError"),
        source.index("except ModelAPIError"),
        source.rindex("except Exception as exc:"),
    )
    assert http < api < broad


# --- the record of what ended a failed turn --------------------------------------------------


async def test_an_unexplained_failure_records_its_class_on_the_terminal_row_and_no_text(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ The row a later query reads. Mutation check: drop the `error` key from the terminal
    meta and this goes red — which is the state that left 2026-09-11 unexplained."""
    conv_id, state = await _run_until_settled(
        _fresh_engine,
        db_session,
        session_factory,
        _refusing_model(RuntimeError("password=hunter2 row 4242")),
    )

    assert _last_error(state) == engine_module._TURN_FAILED_MESSAGE
    meta = await _terminal_meta(db_session, conv_id)
    assert meta["status"] == "failed"
    assert meta["reason"] is None
    assert str(meta["error"]).startswith("RuntimeError")
    # WHERE, in the platform's own code rather than the test's model stub it was raised from.
    # Mutation check: take the innermost frame regardless of module and this goes red on `tests.`.
    assert " at=src." in str(meta["error"]), meta["error"]
    assert "hunter2" not in str(meta["error"])
    assert "4242" not in str(meta["error"])


async def test_a_named_model_failure_records_its_status_on_the_terminal_row(
    _fresh_engine, db_session, session_factory
) -> None:
    conv_id, _state = await _run_until_settled(
        _fresh_engine,
        db_session,
        session_factory,
        _refusing_model(ModelHTTPError(status_code=529, model_name="opus", body=OVERLOADED_BODY)),
    )
    meta = await _terminal_meta(db_session, conv_id)
    assert meta["reason"] == MODEL_UNAVAILABLE_CODE
    assert str(meta["error"]).startswith("ModelHTTPError")
    assert "status=529" in str(meta["error"])
    assert "type=overloaded_error" in str(meta["error"])


async def test_a_completed_turn_writes_no_error_key(
    _fresh_engine, db_session, session_factory
) -> None:
    """A completed turn's row stays exactly what it always was."""
    conv_id, state = await _run_until_settled(
        _fresh_engine, db_session, session_factory, _answering_model()
    )
    assert state.status == "completed"
    meta = await _terminal_meta(db_session, conv_id)
    assert "error" not in meta


# --- a Build turn ------------------------------------------------------------------------------


async def test_a_build_turn_is_told_in_its_own_words_rather_than_the_generic_one(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ The kind the incident was: the assistant is fine, the workspace is exactly as the last
    write left it, and the thing to do is send again — none of which the generic ending says.
    Also the `ModelAPIError` case on the persisted row.
    Mutation check: fall through to the generic failure sentence and this goes red."""
    conv_id, state = await _run_until_settled(
        _fresh_engine,
        db_session,
        session_factory,
        _refusing_model(ModelAPIError(model_name="opus", message="stream ended: detail 5521")),
        kind=ChatKind.BUILD,
    )

    assert state.status == "failed"
    assert _terminal(state).reason == MODEL_UNAVAILABLE_CODE
    assert _last_error(state) == MODEL_UNAVAILABLE_TEXT
    meta = await _terminal_meta(db_session, conv_id)
    assert meta["reason"] == MODEL_UNAVAILABLE_CODE
    assert str(meta["error"]).startswith("ModelAPIError")
    assert "5521" not in str(meta["error"])


# --- one arm per kind ------------------------------------------------------------------------

_SENTENCE_BY_KIND = {
    ChatKind.PLAN: MODEL_UNAVAILABLE_WITHOUT_A_WORKSPACE_TEXT,
    ChatKind.GENERIC: MODEL_UNAVAILABLE_WITHOUT_A_WORKSPACE_TEXT,
    ChatKind.BUILD: MODEL_UNAVAILABLE_TEXT,
}


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_every_kind_is_told_the_sentence_its_own_arm_names(
    _fresh_engine, db_session, session_factory, kind: ChatKind
) -> None:
    """★ Walked over the enum rather than checked as a pair. Two kinds share the no-workspace
    sentence because neither holds a workspace, not because one of them is the fallback — and a
    kind added to `ChatKind` fails here on the lookup rather than inheriting a promise about a
    workspace it does not have.

    Mutation check: group `ChatKind.GENERIC` with `ChatKind.BUILD` in `_end_model_unavailable`
    and BIAL Chat goes red on "it will carry on from here"."""
    _conv_id, state = await _run_until_settled(
        _fresh_engine,
        db_session,
        session_factory,
        _refusing_model(ModelHTTPError(status_code=503, model_name="opus", body="unavailable")),
        kind=kind,
    )

    assert state.status == "failed"
    assert _terminal(state).reason == MODEL_UNAVAILABLE_CODE
    assert _last_error(state) == _SENTENCE_BY_KIND[kind]
