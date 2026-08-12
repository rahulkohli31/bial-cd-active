"""Boundary exception handlers: no input echo on 422, no internal detail on 500."""

from __future__ import annotations

import json

from fastapi import Request
from fastapi.exceptions import RequestValidationError

from src.core.errors import unhandled_exception_handler, validation_exception_handler


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def test_validation_handler_drops_submitted_input() -> None:
    # A validation error whose raw form carries the submitted value (a plaintext
    # password) must NOT reflect that value back to the client.
    exc = RequestValidationError(
        [
            {
                "type": "missing",
                "loc": ("body", "password"),
                "msg": "Field required",
                "input": "hunter2-secret",
            }
        ]
    )
    response = validation_exception_handler(_request(), exc)
    assert response.status_code == 422
    body = json.loads(bytes(response.body))
    assert body == {
        "detail": [{"type": "missing", "loc": ["body", "password"], "msg": "Field required"}]
    }
    assert b"hunter2-secret" not in bytes(response.body)


def test_unhandled_handler_returns_generic_500() -> None:
    # An unhandled exception must return a generic message — never the internal
    # detail, which is logged server-side instead.
    response = unhandled_exception_handler(_request(), RuntimeError("secret stack detail"))
    assert response.status_code == 500
    body = json.loads(bytes(response.body))
    assert body == {"detail": "Internal server error"}
    assert b"secret stack detail" not in bytes(response.body)
