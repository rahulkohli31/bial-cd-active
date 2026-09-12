"""The one context overflow no pre-flight can catch, said in the platform's words.

WHY THIS EXISTS. This is not a leak fix. `engine._run_turn` has always ended a broken turn with a
broad `except Exception` that says "the assistant hit a problem", so the citizen never saw the
provider's own sentence — the work was scoped as though they did. What was wrong is that the
sentence was USELESS here: the assistant is fine, the chat is full, and the thing to do is
start a new one. So this is a SPECIFIC match placed ahead of that broad handler, and the two
risks it carries are both about placement rather than about wording.

THE FIRST RISK IS SWALLOWING. The new arm catches an exception CLASS — every 4xx/5xx the
provider returns — of which only one member is its business. An arm that answered a bad media
type or a rate limit with "this chat is full, start a new chat" would send the citizen to a new
chat that fails identically — a loop they cannot leave by following the advice they were given.

THE SECOND IS ORDER. `ModelHTTPError` is an `Exception`, so both arms match every payload this
file sends: with the arms the other way round the specific one is unreachable dead code, and
Python says nothing about it. That is why the ordering test below sends a payload BOTH would
catch and asserts which sentence won — the only way the mistake is visible.

The shape being matched was MEASURED against the live Foundry deployment rather than assumed
from an OpenAI-flavoured string: `status_code=400` with a parsed body of
`{'type': 'error', 'error': {'type': 'invalid_request_error',
'message': 'prompt is too long: 1963668 tokens > 1000000 maximum'}, 'request_id': ...}`, raised
as pydantic-ai's `ModelHTTPError` (its `_map_api_errors` wraps the SDK's `BadRequestError`).
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import pathlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.api.v1.conversations.schemas import TurnEndedFrame, TurnErrorFrame
from src.config import settings
from src.db.models.conversation import ChatKind
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.sandbox.config import SandboxConfig
from src.services.turns import engine as engine_module
from src.services.turns.copy import CHAT_TOO_LONG_CODE, CHAT_TOO_LONG_TEXT
from src.services.turns.engine import (
    DOCUMENT_TOO_LONG_CODE,
    DOCUMENT_TOO_LONG_TEXT,
    TurnEngine,
    _is_context_overflow,
    _is_document_too_long,
    set_turn_engine_for_tests,
)
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeSandboxClient

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)

# The provider's real refusal, copied from the probe run rather than paraphrased. The numbers are
# incidental — nothing reads them, which is the point of this being error handling rather than a
# second estimate — but they are kept so the fixture is recognisably the thing that was measured.
OVERFLOW_BODY: dict[str, Any] = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "prompt is too long: 1963668 tokens > 1000000 maximum",
    },
    "request_id": "req_011CepLWJmgUvgxV5mq4rij5",
}

# THE REAL REFUSAL, COPIED FROM A LIVE CALL rather than composed here — a 601-page PDF sent
# through this exact stack (pydantic-ai `AnthropicModel` over `AsyncAnthropicFoundry`) against
# the deployment in use. A hand-written approximation of a provider's wording is the one thing
# a marker match must not be tested against.
PDF_PAGES_BODY: dict[str, Any] = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": (
            "messages.0.content.0.pdf.source.base64.data: "
            "A maximum of 600 PDF pages may be provided."
        ),
    },
    "request_id": "req_011Ceyz4nptDfF5aTYkZmURj",
}

# A 400 from the same provider, the same error type, about something else entirely. This is the
# payload that separates "the prompt did not fit" from "the request was malformed" — the two are
# indistinguishable by status, and answering the second with the first's remedy is the defect.
UNSUPPORTED_MEDIA_BODY: dict[str, Any] = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "messages.0.content.1.source.media_type: image/tiff is not supported",
    },
}


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every kind pins the project's live container, so a turn dies at the workspace pin long
    before the model runs unless a deployment is configured (same reason `test_engine.py` does
    this)."""
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
    """The liveness lease (Redis) and the attach's storage reads, as autouse wrappers."""
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
    """A model that fails the way the provider fails: at the request, having said nothing."""

    async def _stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        raise exc
        yield ""  # pragma: no cover — unreachable, and what makes this an async generator

    return FunctionModel(stream_function=_stream)


async def _run_until_settled(engine: TurnEngine, db_session, session_factory, model):
    """Start one Plan turn on a fresh conversation and wait for its detached task to finish."""
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)

    async def _noop_persist() -> None:
        return None

    await engine.start_turn(
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
    with contextlib.suppress(BaseException):
        await state.task
    return conv.id, state


def _last_error(state) -> str | None:
    frames = [frame for frame in state.ring if isinstance(frame, TurnErrorFrame)]
    return frames[-1].message if frames else None


def _terminal(state) -> TurnEndedFrame:
    frames = [frame for frame in state.ring if isinstance(frame, TurnEndedFrame)]
    assert len(frames) == 1, f"expected exactly one terminal, got {len(frames)}"
    return frames[-1]


# --- the refusal a citizen can act on -------------------------------------------------------


async def test_a_provider_context_refusal_reaches_the_citizen_as_the_chat_being_full(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ The overflow that gets past the admission check is answered with the guardrail's own
    sentence, not with "the assistant hit a problem".

    THE SAME SENTENCE AS THE 413, DELIBERATELY. `turns.start_turn` refuses an already-over-full
    conversation with `CHAT_TOO_LONG_TEXT`; this is the identical condition arriving a moment
    later, from the provider instead of from the platform's own count. Two wordings for one
    fact is two things to keep true, and the citizen cannot tell the two moments apart anyway.

    Mutation check: delete the specific arm and this goes red on the generic sentence."""
    conv_id, state = await _run_until_settled(
        _fresh_engine,
        db_session,
        session_factory,
        _refusing_model(ModelHTTPError(status_code=400, model_name="opus", body=OVERFLOW_BODY)),
    )

    assert state.status == "failed"
    assert _last_error(state) == CHAT_TOO_LONG_TEXT
    # The route forward, in the machine-readable half: the same code the 413 carries, so a
    # client that already knows how to offer "start a new chat" needs to learn nothing new.
    assert _terminal(state).reason == CHAT_TOO_LONG_CODE
    # The ending is still an ENDING: one terminal, and the conversation is not left wedged.
    assert conv_id not in _mid_reply


async def test_the_sentence_names_no_number_and_no_provider(
    _fresh_engine, db_session, session_factory
) -> None:
    """This is error handling, not estimation. The provider's message carries two token counts
    and a request id, and not one character of it may reach the citizen — this is the assertion
    that catches an "improvement" that interpolates the real figures for helpfulness."""
    _conv_id, state = await _run_until_settled(
        _fresh_engine,
        db_session,
        session_factory,
        _refusing_model(ModelHTTPError(status_code=400, model_name="opus", body=OVERFLOW_BODY)),
    )

    message = _last_error(state)
    assert message is not None
    for leak in ("1963668", "1000000", "token", "req_011", "opus", "400"):
        assert leak not in message.lower(), f"the citizen was shown {leak!r}"


# --- the arm must not swallow anything else -------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        pytest.param(400, UNSUPPORTED_MEDIA_BODY, id="a-400-about-something-else"),
        pytest.param(
            403, {"error": {"message": "forbidden"}}, id="a-refusal-that-is-not-transient"
        ),
        pytest.param(400, None, id="a-400-with-no-body-to-read"),
    ],
)
async def test_other_provider_errors_still_reach_the_generic_handler_unchanged(
    _fresh_engine, db_session, session_factory, status_code: int, body: object
) -> None:
    """★ The assertion that stops the new arm swallowing unrelated failures.

    Each of these is a `ModelHTTPError` — so each one enters the SPECIFIC arm — and none of them
    means the chat is full. Telling this citizen to start a new chat would send them somewhere
    the identical message fails the identical way, with nothing left to try.

    A 429 and a 5xx used to sit in this list. They left it on 2026-09-11, when they gained a named
    ending of their own (`test_model_unavailable.py`): the point of this test is that a refusal
    which is NEITHER an overflow NOR transient still ends generically, so the statuses here are
    the ones no specific arm claims.

    IT ALSO PINS THE ENDING, not just the sentence. The non-matching branch cannot `raise`
    onward (a sibling `except` never catches what another one raises), so the way this could
    fail is not "wrong words" but "no terminal at all" — a turn left running until the
    subscriber's stall timeout. Hence the terminal and the released guard below.

    Mutation check: widen the match to `exc.status_code == 400` and the first case goes red."""
    conv_id, state = await _run_until_settled(
        _fresh_engine,
        db_session,
        session_factory,
        _refusing_model(ModelHTTPError(status_code=status_code, model_name="opus", body=body)),
    )

    assert state.status == "failed"
    assert _last_error(state) == engine_module._TURN_FAILED_MESSAGE
    assert CHAT_TOO_LONG_TEXT not in (_last_error(state) or "")
    # The generic ending names no reason — that is what makes the chat-full code meaningful.
    assert _terminal(state).reason is None
    assert conv_id not in _mid_reply


async def test_a_failure_that_is_not_a_provider_error_at_all_is_untouched(
    _fresh_engine, db_session, session_factory
) -> None:
    """The arm is keyed on the exception CLASS as well as the message, so an ordinary bug —
    which reaches the broad handler without passing through the new arm — must end exactly as
    it did before. Its message contains the marker phrase on purpose: a match written against
    `str(exc)` alone, with no class test, would translate a platform bug into "your chat is
    full" and hide it."""
    _conv_id, state = await _run_until_settled(
        _fresh_engine,
        db_session,
        session_factory,
        _refusing_model(RuntimeError("prompt is too long for the buffer we allocated")),
    )

    assert state.status == "failed"
    assert _last_error(state) == engine_module._TURN_FAILED_MESSAGE


# --- ordering: the specific arm must be reachable -------------------------------------------


async def test_the_specific_match_runs_before_the_broad_one(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ THE ORDERING, ASSERTED BY A PAYLOAD BOTH ARMS CATCH.

    `ModelHTTPError` is an `Exception`. Python accepts the two handlers in either order and
    says nothing when the second is unreachable, so swapping them is a silent regression that
    no type checker, linter or import test can see — the only witness is a payload that both
    would catch, and which sentence comes out.

    Mutation check: move `except Exception` above `except ModelHTTPError` and this goes red
    with the generic sentence, while every other test in this file stays green."""
    overflow = ModelHTTPError(status_code=400, model_name="opus", body=OVERFLOW_BODY)
    # Both arms would catch it: this is the premise the assertion rests on, stated rather than
    # assumed, so a future change to the exception hierarchy cannot quietly make the test vacuous.
    assert isinstance(overflow, Exception)

    _conv_id, state = await _run_until_settled(
        _fresh_engine, db_session, session_factory, _refusing_model(overflow)
    )

    assert _last_error(state) == CHAT_TOO_LONG_TEXT
    assert _last_error(state) != engine_module._TURN_FAILED_MESSAGE


def test_a_build_turns_refusal_reaches_the_same_arm() -> None:
    """★ THE OTHER KIND, and the reason it is asserted structurally rather than driven.

    A Build turn's model call happens inside `_run_write` / `_run_write_once`, several frames
    below the handler chain — so the translation reaches Build turns only for as long as
    nothing in between catches the refusal first. Today nothing does: those two helpers catch
    exactly one narrow class each. An `except Exception` added to either would end a Build
    turn's overflow with the generic sentence while the Plan tests above stayed green, which is
    a defect visible from nowhere else.

    This is a claim about the model call's path, not a ban on error handling: a handler for a
    class the provider cannot raise is welcome, and this test names the ones that would eat the
    refusal."""
    source = pathlib.Path(inspect.getfile(engine_module)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    swallows_the_refusal = {"Exception", "BaseException", "ModelAPIError", "ModelHTTPError"}

    for name in ("_run_write", "_run_write_once"):
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        )
        for handler in (n for n in ast.walk(function) if isinstance(n, ast.ExceptHandler)):
            caught = handler.type
            names = (
                {e.id for e in caught.elts if isinstance(e, ast.Name)}
                if isinstance(caught, ast.Tuple)
                else {caught.id}
                if isinstance(caught, ast.Name)
                else {"BareExcept"}
            )
            assert not names & swallows_the_refusal, (
                f"{name} line {handler.lineno} catches {names & swallows_the_refusal} — a "
                "Build turn's context refusal would never reach the arm that translates it"
            )


def test_the_handler_order_is_what_the_source_says_too() -> None:
    """The same fact structurally, because the behavioural test above is one `await` away from
    a fixture problem masking it. Read straight off `_run_turn`'s handler list: the specific
    arm has to appear before the bare `except Exception`, and both have to still be there."""
    source = pathlib.Path(inspect.getfile(engine_module)).read_text(encoding="utf-8")
    run_turn = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_turn"
    )
    outermost = next(node for node in run_turn.body if isinstance(node, ast.Try))
    caught = [
        handler.type.id if isinstance(handler.type, ast.Name) else None
        for handler in outermost.handlers
    ]
    assert "ModelHTTPError" in caught, "the specific arm is gone"
    assert "Exception" in caught, "the broad handler is gone"
    assert caught.index("ModelHTTPError") < caught.index("Exception")


# --- the predicate, on its own ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        pytest.param(400, OVERFLOW_BODY, True, id="the-measured-refusal"),
        pytest.param(
            400,
            {
                "error": {
                    "message": "input length and `max_tokens` exceed context limit: 1 + 2 > 3"
                }
            },
            True,
            id="the-prompt-fits-but-the-reply-would-not",
        ),
        pytest.param(
            400, "prompt is too long: 5 tokens > 4 maximum", True, id="a-bare-string-body"
        ),
        pytest.param(400, UNSUPPORTED_MEDIA_BODY, False, id="a-different-400"),
        pytest.param(429, OVERFLOW_BODY, False, id="the-right-words-at-the-wrong-status"),
        pytest.param(400, None, False, id="no-body"),
        pytest.param(
            400, ["prompt is too long"], False, id="a-body-shaped-like-nothing-documented"
        ),
        pytest.param(400, {"error": "prompt is too long"}, False, id="error-is-not-an-object"),
    ],
)
def test_the_predicate_is_narrow_on_content_and_defensive_on_shape(
    status_code: int, body: object, expected: bool
) -> None:
    """WHAT IT MAY AND MAY NOT READ. The status alone is not evidence — every malformed request
    is a 400 — so the provider's own sentence decides. An unreadable body is not evidence
    either: it yields no message and therefore no match, which fails towards the generic ending
    rather than towards a remedy that would not work."""
    assert (
        _is_context_overflow(ModelHTTPError(status_code=status_code, model_name="o", body=body))
        is expected
    )


def test_the_markers_are_phrases_not_words() -> None:
    """A one-word marker (`"long"`, `"context"`, `"token"`) would match half the 400s a provider
    can produce, and nothing downstream would notice — the citizen would simply be told to
    start a new chat for faults a new chat cannot fix. Each marker has to be a phrase specific
    enough that no other refusal contains it."""
    for marker in engine_module._CONTEXT_OVERFLOW_MARKERS:
        assert " " in marker, f"{marker!r} is too small to identify a refusal"
        assert marker == marker.lower(), f"{marker!r} must be lowercase — the haystack is"


def test_a_conversation_id_is_never_needed_to_tell_the_citizen_this() -> None:
    """The translation is a pure function of the provider's answer. It does not read the
    conversation, the user's limit or the message — which is what keeps it honest as ERROR
    HANDLING rather than a second, quieter admission check with a number in it."""
    signature = inspect.signature(_is_context_overflow)
    assert list(signature.parameters) == ["exc"]
    assert uuid.UUID not in {p.annotation for p in signature.parameters.values()}


# --- a document the provider will not read ---------------------------------------------------


async def test_a_pdf_refused_on_page_count_names_the_document_not_the_chat(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ THE OTHER 400 A DOCUMENT CAN EARN, and it must not borrow the overflow's sentence.

    The provider refuses any PDF over 600 pages outright. Measured against the live deployment:
    a 0.84 MB PDF of 601 text pages is refused while a 10 MB scan of forty is not — so this is
    not a size refusal and no byte cap at the upload door can see it coming. The door
    retired the page cap that could, deliberately, which is what leaves this arm as the only
    place the citizen can be told what happened.

    IT IS NOT "THIS CHAT IS FULL", WHICH NAMES ONLY HALF THE REMEDY. The user turn is persisted
    before the model is called and its bytes are rehydrated into every later turn, so the document
    is a permanent resident of this chat and every message here refuses identically. A new chat
    alone does not help — they would attach the same file. A shorter document alone does not help
    either — this chat still carries the old one. The sentence has to name both.

    Mutation check: delete the `_is_document_too_long` arm and this goes red on the generic
    sentence; point it at `CHAT_TOO_LONG_TEXT` and it goes red on the wrong remedy; drop either
    half of the remedy and the last two assertions go red.
    """
    conv_id, state = await _run_until_settled(
        _fresh_engine,
        db_session,
        session_factory,
        _refusing_model(ModelHTTPError(status_code=400, model_name="opus", body=PDF_PAGES_BODY)),
    )

    assert state.status == "failed"
    assert _last_error(state) == DOCUMENT_TOO_LONG_TEXT
    assert _terminal(state).reason == DOCUMENT_TOO_LONG_CODE
    # Emphatically NOT the chat-full sentence, whose remedy stops at the new chat.
    assert CHAT_TOO_LONG_TEXT not in (_last_error(state) or "")
    assert _terminal(state).reason != CHAT_TOO_LONG_CODE
    said = _last_error(state) or ""
    assert "new chat" in said, "this chat cannot recover — the remedy has to leave it"
    assert "shorter" in said, "a new chat with the same file fails identically"
    # The ending is still an ENDING: one terminal, and the conversation is not left wedged.
    assert conv_id not in _mid_reply


async def test_the_page_refusal_leaks_no_provider_internals(
    _fresh_engine, db_session, session_factory
) -> None:
    """The provider's message names a request id and a JSON path into the wire format
    (`messages.0.content.0.pdf.source.base64.data`). None of it may reach the citizen, and the
    sentence quotes no page number either: the limit is the provider's, not the platform's, and
    a number stated in copy is one more thing to keep true across a deployment change."""
    _, state = await _run_until_settled(
        _fresh_engine,
        db_session,
        session_factory,
        _refusing_model(ModelHTTPError(status_code=400, model_name="opus", body=PDF_PAGES_BODY)),
    )

    said = _last_error(state) or ""
    for leak in ("messages.0", "base64", "req_011", "600"):
        assert leak not in said, f"{leak!r} reached the citizen"


def test_only_the_providers_own_page_sentence_matches() -> None:
    """THE STATUS IS NOT THE MATCH. Every malformed request is a 400 — an unsupported media
    type, a bad tool schema — and answering any of them with "that PDF has too many pages" is
    the same untrue-sentence defect read from the other end.

    Mutation check: widen `_is_document_too_long` to `exc.status_code == 400` and the last three
    cases go red."""
    assert _is_document_too_long(
        ModelHTTPError(status_code=400, model_name="o", body=PDF_PAGES_BODY)
    )
    # A bare-string body (a gateway that answered with something other than provider JSON).
    assert _is_document_too_long(
        ModelHTTPError(
            status_code=400, model_name="o", body="A maximum of 600 PDF pages may be provided."
        )
    )
    # Another 400 entirely.
    assert not _is_document_too_long(
        ModelHTTPError(status_code=400, model_name="o", body=OVERFLOW_BODY)
    )
    # An unreadable body is not evidence of anything.
    assert not _is_document_too_long(ModelHTTPError(status_code=400, model_name="o", body=None))
    # The right sentence on the wrong status is not this failure.
    assert not _is_document_too_long(
        ModelHTTPError(status_code=500, model_name="o", body=PDF_PAGES_BODY)
    )


def test_the_two_document_refusals_never_both_match() -> None:
    """They take different arms and give different remedies, so an overlap would make the
    citizen's sentence depend on the order the arms happen to be written in."""
    for body in (OVERFLOW_BODY, PDF_PAGES_BODY):
        error = ModelHTTPError(status_code=400, model_name="o", body=body)
        assert not (_is_context_overflow(error) and _is_document_too_long(error))
