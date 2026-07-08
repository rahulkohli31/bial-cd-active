"""The OpenAPI error-response machinery: the two envelope body models, the
`DailyTokenLimitBody`, the `error_responses` builder, and the v1-router-level 500
default propagating onto every v1 route."""

from __future__ import annotations

from src.main import create_app
from src.schemas import DailyTokenLimitBody, DetailBody, ErrorEnvelope, error_responses
from src.schemas.responses import DailyTokenLimitDetail, ErrorDetail


def test_error_envelope_shape_without_code() -> None:
    body = ErrorEnvelope(error=ErrorDetail(message="boom"))
    assert body.model_dump(exclude_none=True) == {"error": {"message": "boom"}}


def test_error_envelope_shape_with_code() -> None:
    body = ErrorEnvelope(error=ErrorDetail(message="boom", code="FILE_QUOTA_EXCEEDED"))
    assert body.model_dump() == {"error": {"message": "boom", "code": "FILE_QUOTA_EXCEEDED"}}


def test_detail_body_shape() -> None:
    assert DetailBody(detail="Not authenticated").model_dump() == {"detail": "Not authenticated"}


def test_daily_token_limit_body_carries_all_five_keys() -> None:
    body = DailyTokenLimitBody(
        error=DailyTokenLimitDetail(
            message="You've reached your daily limit of 1,000 tokens.",
            code="daily_token_limit_exceeded",
            limit=1000,
            used=1000,
            remaining=0,
        )
    )
    assert set(body.model_dump()["error"]) == {"message", "code", "limit", "used", "remaining"}


def test_error_responses_builds_fastapi_mapping() -> None:
    mapping = error_responses(
        (401, DetailBody, "Not authenticated"),
        (404, ErrorEnvelope, "App not found"),
    )
    # int keys, each value carries the model it documents — and one call mixes
    # detail- and envelope-shaped codes.
    assert set(mapping) == {401, 404}
    assert mapping[401] == {"model": DetailBody, "description": "Not authenticated"}
    assert mapping[404]["model"] is ErrorEnvelope


def test_error_responses_empty() -> None:
    assert error_responses() == {}


def test_v1_router_500_default_propagates_to_every_route() -> None:
    # The router-level 500 default merges onto each included route's OpenAPI, so a
    # route that declares nothing (health only declares 503) still documents 500.
    schema = create_app().openapi()
    health = schema["paths"]["/v1/health"]["get"]["responses"]
    assert "500" in health  # inherited v1-router default
    assert "503" in health  # health's own declaration, not clobbered
