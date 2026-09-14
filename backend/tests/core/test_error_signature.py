"""The name that stands in for an exception wherever its message may not go."""

from __future__ import annotations

import re

import pytest
from pydantic_ai.exceptions import ModelAPIError

from src.core.error_signature import error_signature

_OVERLOADED_BODY = {
    "type": "error",
    "error": {"type": "overloaded_error", "message": "Overloaded"},
}


def test_the_signature_is_the_class_chain_the_status_and_the_provider_type_never_the_text() -> (
    None
):
    """A stream that ended in an error event, as pydantic-ai surfaces it: a `ModelAPIError` caused
    by the SDK's status error on an HTTP 200 carrying the provider's error body."""

    class APIStatusError(Exception):
        status_code = 200
        body = _OVERLOADED_BODY

    try:
        try:
            raise APIStatusError("Overloaded: server message with detail 8812")
        except APIStatusError as inner:
            raise ModelAPIError(model_name="opus", message="Overloaded detail 8812") from inner
    except ModelAPIError as exc:
        signature = error_signature(exc)

    assert re.fullmatch(
        r"ModelAPIError <- APIStatusError status=200 type=overloaded_error"
        r" at=tests\.core\.test_error_signature:\w+:\d+",
        signature,
    ), signature
    assert "8812" not in signature
    assert "Overloaded" not in signature


def test_the_signature_survives_a_cycle_and_is_bounded() -> None:
    first, second = RuntimeError("a"), ValueError("b")
    first.__context__, second.__context__ = second, first
    assert error_signature(first) == "RuntimeError <- ValueError"

    deep: BaseException = KeyError("root")
    for _ in range(10):
        wrapper = RuntimeError("wrap")
        wrapper.__cause__ = deep
        deep = wrapper
    assert error_signature(deep).count(" <- ") == 4


@pytest.mark.parametrize(
    "token",
    ["Overloaded", "overloaded error", "x" * 65, "überladen", "overloaded_error\nforged=1"],
)
def test_the_provider_type_is_kept_only_as_a_short_lowercase_token(token: str) -> None:
    """The one string in the signature that a remote party writes. Mutation check: drop the token
    pattern and every case goes red with the provider's text on the row."""

    class APIStatusError(Exception):
        status_code = 529
        body = {"type": "error", "error": {"type": token, "message": "Overloaded"}}

    assert error_signature(APIStatusError("detail")) == "APIStatusError status=529"
