"""The OpenAPI error-response machinery: the two envelope body models, the
`DailyTokenLimitBody`, the `error_responses` builder, and the v1-router-level
500 + suspension-403 defaults propagating onto every v1 route."""

from __future__ import annotations

import pytest

from src.main import create_app
from src.schemas import DailyTokenLimitBody, DetailBody, ErrorEnvelope, error_responses
from src.schemas.responses import DailyTokenLimitDetail, ErrorDetail


def test_error_envelope_shape_without_code() -> None:
    body = ErrorEnvelope(error=ErrorDetail(message="boom"))
    assert body.model_dump(exclude_none=True) == {"error": {"message": "boom"}}


def test_error_envelope_shape_with_code() -> None:
    body = ErrorEnvelope(error=ErrorDetail(message="boom", code="FILE_QUOTA_EXCEEDED"))
    assert body.model_dump(exclude_none=True) == {
        "error": {"message": "boom", "code": "FILE_QUOTA_EXCEEDED"}
    }


def test_error_envelope_can_carry_a_structured_detail() -> None:
    """`detail` is for the refusals a client must RENDER rather than merely branch on —
    the publish gate's waiting-for-review 409 carries the pending state, the submitted
    version and the rejection note, so neither citizen surface needs a second call.

    The unset case is pinned by the two tests above with `exclude_none=True`, which is
    what the HANDLER does in effect: `app_api_error_handler` builds the body key by key
    and only adds `detail` when the raiser attached one, so no response ever carries a
    literal `"detail": null`."""
    body = ErrorEnvelope(
        error=ErrorDetail(
            message="Already waiting for review.",
            code="waiting_for_review",
            detail={"status": "pending", "submittedSha": "ab" * 20, "rejectionNote": None},
        )
    )
    dumped = body.model_dump()
    assert dumped["error"]["detail"]["status"] == "pending"
    assert dumped["error"]["code"] == "waiting_for_review"


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


def test_error_responses_rejects_duplicate_status() -> None:
    # Two specs for the same status code is a programming error (the second would
    # silently clobber the first) — the builder must refuse it loudly.
    with pytest.raises(ValueError, match="duplicate status 404"):
        error_responses(
            (404, ErrorEnvelope, "App not found"),
            (404, ErrorEnvelope, "Record not found"),
        )


def test_openapi_headerout_description_has_no_dead_flag_reference() -> None:
    # HeaderOut is documented-only; its docstring must NOT claim a
    # `response_model_exclude_none` flag drives the omit-when-unset shape (that flag
    # is not involved — `_header_dict` produces the shape). Negative substring guard.
    schema = create_app().openapi()
    description = schema["components"]["schemas"]["HeaderOut"].get("description", "")
    assert "response_model_exclude_none" not in description


def test_v1_router_500_default_propagates_to_every_route() -> None:
    # The router-level 500 default merges onto each included route's OpenAPI, so a
    # route that declares nothing (health only declares 503) still documents 500.
    schema = create_app().openapi()
    health = schema["paths"]["/v1/health"]["get"]["responses"]
    assert "500" in health  # inherited v1-router default
    assert "503" in health  # health's own declaration, not clobbered


def test_v1_router_suspension_403_default_propagates() -> None:
    # `current_user` 403s a suspended account on every authenticated route (deps.py,
    # R11); the router-level AUTH_403_SUSPENDED default documents it. A route with
    # its own 403 (admin's superadmin gate) keeps its declaration.
    schema = create_app().openapi()
    projects = schema["paths"]["/v1/projects"]["get"]["responses"]
    assert projects["403"]["description"] == "Account suspended"
    admin_users = schema["paths"]["/v1/admin/users"]["get"]["responses"]
    assert admin_users["403"]["description"] == "Super-admin privileges required"
