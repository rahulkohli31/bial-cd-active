"""The dynamic-sessions client: one call per operation, and every failure a named error."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable

import httpx
import pytest
import structlog.testing

from src.services.analysis import (
    AnalysisConfig,
    AnalysisTimedOutError,
    AnalysisUnavailableError,
    session_identifier,
)
from src.services.analysis.sessions import API_VERSION, DynamicSessionsRuntime

_POOL = "https://centralindia.dynamicsessions.io/subscriptions/s/resourceGroups/rg/sessionPools/p"
_CHAT = uuid.UUID("01890a5d-ac96-7c4a-8f4e-9d2b3a1c0e5f")
_SESSION = "01890a5dac967c4a8f4e9d2b3a1c0e5f"


async def _token() -> str:
    return "bearer-for-the-backend"


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    token: Callable[[], Awaitable[str]] | None = None,
) -> tuple[DynamicSessionsRuntime, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    runtime = DynamicSessionsRuntime(
        AnalysisConfig(pool_endpoint=_POOL),
        transport=httpx.MockTransport(_record),
        token=token or _token,
    )
    return runtime, seen


def test_a_conversation_is_named_by_its_id_in_hex() -> None:
    assert session_identifier(_CHAT) == _SESSION


async def test_listing_sends_the_identifier_and_reads_name_size_and_time() -> None:
    body = {
        "value": [
            {
                "name": "q3.xlsx",
                "directory": ".",
                "type": "file",
                "sizeInBytes": 1234,
                "lastModifiedAt": "t1",
            },
            {"name": "read_attachment.py", "directory": ".", "type": "file", "sizeInBytes": 99},
            {"name": "out", "directory": ".", "type": "directory", "sizeInBytes": 0},
        ]
    }
    runtime, seen = _client(lambda _r: httpx.Response(200, json=body))

    listed = await runtime.list_files(_SESSION)

    assert [(f.name, f.size, f.modified) for f in listed] == [
        ("q3.xlsx", 1234, "t1"),
        ("read_attachment.py", 99, None),
    ]
    request = seen[0]
    assert (request.method, request.url.path) == (
        "GET",
        "/subscriptions/s/resourceGroups/rg/sessionPools/p/files",
    )
    assert request.url.params["identifier"] == _SESSION
    assert request.url.params["api-version"] == API_VERSION
    assert request.headers["authorization"] == "Bearer bearer-for-the-backend"


async def test_an_upload_carries_the_file_as_multipart() -> None:
    runtime, seen = _client(lambda _r: httpx.Response(200, json={}))

    await runtime.upload_file(_SESSION, "q3.xlsx", b"workbook bytes")

    request = seen[0]
    assert (request.method, request.url.path.rsplit("/", 1)[-1]) == ("POST", "files")
    assert b'filename="q3.xlsx"' in request.content
    assert b"workbook bytes" in request.content


async def test_a_file_name_is_escaped_into_its_delete_path() -> None:
    runtime, seen = _client(lambda _r: httpx.Response(204))

    await runtime.delete_file(_SESSION, "Q3 report?.xlsx")

    assert seen[0].method == "DELETE"
    assert seen[0].url.raw_path.split(b"?")[0].endswith(b"/files/Q3%20report%3F.xlsx")


async def test_a_run_sends_the_code_and_reads_what_it_printed() -> None:
    answer = {"status": "Succeeded", "result": {"stdout": "42\n", "stderr": ""}}
    runtime, seen = _client(lambda _r: httpx.Response(200, json=answer))

    result = await runtime.run(_SESSION, "print(6 * 7)", timeout_s=120, output_limit=123_456)

    assert (result.succeeded, result.stdout, result.stderr) == (True, "42\n", "")
    assert seen[0].url.path.endswith("/executions")
    sent = json.loads(seen[0].content)
    assert sent == {
        "codeInputType": "Inline",
        "executionType": "Synchronous",
        "code": "print(6 * 7)",
        "timeoutInSeconds": 120,
        "outputStreamsMaxLength": 123_456,
    }


async def test_code_that_raised_is_a_run_that_did_not_succeed() -> None:
    answer = {"status": "Failed", "result": {"stdout": "", "stderr": "ZeroDivisionError"}}
    runtime, _ = _client(lambda _r: httpx.Response(200, json=answer))

    result = await runtime.run(_SESSION, "1/0", timeout_s=5, output_limit=20_000)

    assert (result.succeeded, result.stderr, result.out_of_memory) == (
        False,
        "ZeroDivisionError",
        False,
    )


async def test_deleting_a_session_names_it() -> None:
    runtime, seen = _client(lambda _r: httpx.Response(204))

    await runtime.delete_session(_SESSION)

    assert seen[0].method == "DELETE"
    assert seen[0].url.params["identifier"] == _SESSION


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
async def test_every_refusal_is_unavailable(status: int) -> None:
    runtime, _ = _client(lambda _r: httpx.Response(status, json={"error": "no"}))

    with pytest.raises(AnalysisUnavailableError):
        await runtime.list_files(_SESSION)


async def test_a_transport_failure_is_unavailable() -> None:
    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    runtime, _ = _client(_refuse)

    with pytest.raises(AnalysisUnavailableError):
        await runtime.run(_SESSION, "print(1)", timeout_s=5, output_limit=20_000)


async def test_a_call_past_its_deadline_is_timed_out() -> None:
    def _slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    runtime, _ = _client(_slow)

    with pytest.raises(AnalysisTimedOutError):
        await runtime.run(_SESSION, "while True: pass", timeout_s=5, output_limit=20_000)


async def test_an_answer_that_is_not_the_expected_shape_is_unavailable() -> None:
    runtime, _ = _client(lambda _r: httpx.Response(200, json={"surprise": True}))

    with pytest.raises(AnalysisUnavailableError):
        await runtime.list_files(_SESSION)


async def test_a_failed_token_is_unavailable_and_logs_only_the_class() -> None:
    async def _no_token() -> str:
        raise RuntimeError("secret-bearing message")

    runtime, seen = _client(lambda _r: httpx.Response(200, json={}), token=_no_token)

    with structlog.testing.capture_logs() as logs, pytest.raises(AnalysisUnavailableError):
        await runtime.list_files(_SESSION)

    assert seen == []
    assert "secret-bearing" not in repr(logs)
    assert [log["error"] for log in logs if log["event"] == "analysis_token_failed"] == [
        "RuntimeError"
    ]


async def test_what_code_printed_never_reaches_a_log() -> None:
    printed = "q3.xlsx row 7: SALARY 1,234,567"
    answer = {"status": "Failed", "result": {"stdout": printed, "stderr": printed}}
    runtime, _ = _client(lambda _r: httpx.Response(200, json=answer))

    with structlog.testing.capture_logs() as logs:
        await runtime.run(
            _SESSION, f"open('q3.xlsx')  # {printed}", timeout_s=5, output_limit=20_000
        )

    assert logs
    assert "SALARY" not in repr(logs)
    assert "q3.xlsx" not in repr(logs)
    assert logs[-1]["conversation_id"] == str(_CHAT)


async def test_closing_closes_the_pool() -> None:
    runtime, _ = _client(lambda _r: httpx.Response(200, json={}))

    await runtime.aclose()

    with pytest.raises(AnalysisUnavailableError):
        await runtime.list_files(_SESSION)


def _failed(stderr: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(
        200, json={"status": "Failed", "result": {"stdout": "", "stderr": stderr}}
    )


async def test_the_services_own_deadline_is_timed_out() -> None:
    runtime, _ = _client(
        _failed("Request timed out waiting for code execution to complete after 120 seconds")
    )

    with pytest.raises(AnalysisTimedOutError):
        await runtime.run(
            _SESSION, "import time; time.sleep(999)", timeout_s=120, output_limit=20_000
        )


async def test_a_run_aborted_behind_a_timeout_is_unavailable() -> None:
    runtime, _ = _client(_failed("Execution aborted"))

    with pytest.raises(AnalysisUnavailableError):
        await runtime.run(_SESSION, "print(1)", timeout_s=5, output_limit=20_000)


@pytest.mark.parametrize(
    "stderr", ["Kernel restarted", "Traceback ... MemoryError: cannot allocate"]
)
async def test_running_out_of_memory_is_named(stderr: str) -> None:
    runtime, _ = _client(_failed(stderr))

    result = await runtime.run(_SESSION, "x = bytearray(2**33)", timeout_s=5, output_limit=20_000)

    assert (result.succeeded, result.out_of_memory) == (False, True)


async def test_deleting_a_file_that_is_already_gone_is_done() -> None:
    runtime, _ = _client(
        lambda _r: httpx.Response(404, json={"error": {"code": "DeleteFileError"}})
    )

    await runtime.delete_file(_SESSION, "q3.xlsx")


async def test_a_list_that_cannot_be_read_is_unavailable() -> None:
    runtime, _ = _client(
        lambda _r: httpx.Response(200, json={"value": [{"name": "x", "type": "file"}]})
    )

    with pytest.raises(AnalysisUnavailableError):
        await runtime.list_files(_SESSION)
