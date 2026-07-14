"""U1 — the C2 client's `/_sup/*` supervisor HTTP layer + accessor.

An `httpx.MockTransport` stands in for the C1 supervisor: no live container, every
wire shape asserted against `sandbox/supervisor/app.py`.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from src.services.sandbox import client as client_module
from src.services.sandbox.base import (
    ExecResult,
    FileStrReplace,
    SandboxError,
    SandboxHandle,
    SandboxNotReadyError,
)
from src.services.sandbox.client import (
    AcaSandboxClient,
    SandboxNotConfiguredError,
    create_sandbox,
)
from src.services.sandbox.config import SandboxConfig

Handler = Callable[[httpx.Request], httpx.Response]


def _config() -> SandboxConfig:
    return SandboxConfig(
        subscription_id="sub",
        resource_group="rg",
        region="westeurope",
        image_ref="bialgenaicr01.azurecr.io/citizen-dev-sandbox:latest",
        app_data_base_url="https://platform.example/v1",
    )


def _handle(*, token: str = "tok-secret", ready: bool = False) -> SandboxHandle:
    fqdn = "app-xyz.westeurope.azurecontainerapps.io"
    return SandboxHandle(
        fqdn=fqdn,
        token=token,
        app_name="sbx-abc",
        preview_url=f"https://{fqdn}/",
        ready=ready,
    )


def _client(handler: Handler) -> AcaSandboxClient:
    return AcaSandboxClient(_config(), transport=httpx.MockTransport(handler))


async def test_exec_returns_result_and_nonzero_exit_is_not_an_error() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        body = json.loads(request.content)
        code = 0 if body["cmd"] == ["npm", "run", "build"] else 1
        return httpx.Response(200, json={"stdout": "out", "stderr": "err", "exit": code})

    client = _client(handler)
    handle = _handle()
    ok = await client.exec(handle, ["npm", "run", "build"])
    assert ok == ExecResult(stdout="out", stderr="err", exit=0)
    assert seen["auth"] == "Bearer tok-secret"  # every /_sup/* call is bearer-authed
    assert seen["path"] == "/_sup/exec"
    # A non-zero exit (e.g. a tsc failure) is a NORMAL return, never an exception (C1).
    bad = await client.exec(handle, ["tsc"])
    assert bad.exit == 1
    await client.aclose()


async def test_files_str_replace_serializes_to_flat_c1_body() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["path"] = request.url.path
        return httpx.Response(200, json={"ok": True, "replacements": 1})

    client = _client(handler)
    res = await client.files(
        _handle(), FileStrReplace(path="app/page.tsx", old_str="a", new_str="b")
    )
    assert res.ok is True
    assert res.detail == {"replacements": 1}
    assert captured["path"] == "/_sup/files"
    assert captured["body"] == {
        "action": "str_replace",
        "path": "app/page.tsx",
        "old_str": "a",
        "new_str": "b",
    }
    await client.aclose()


async def test_dev_start_returns_pid_and_is_idempotent_on_409() -> None:
    state = {"started": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_sup/dev/start":
            if state["started"]:
                return httpx.Response(409, json={"detail": "dev server already running"})
            state["started"] = True
            return httpx.Response(200, json={"pid": 4321})
        if request.url.path == "/_sup/dev/status":
            return httpx.Response(200, json={"running": True, "ready": True, "port": 3000})
        return httpx.Response(404)

    client = _client(handler)
    handle = _handle()
    assert await client.dev_start(handle) == 4321
    # A second start -> C1 409 -> idempotent success (the already-running sentinel), no raise.
    assert await client.dev_start(handle) == 0
    await client.aclose()


async def test_wait_ready_polls_until_ready_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(client_module, "_asleep", fake_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        ready = calls["n"] >= 3
        return httpx.Response(200, json={"running": True, "ready": ready, "port": 3000})

    client = _client(handler)
    result = await client.wait_ready(_handle(), timeout_s=120.0)
    assert result.ready is True
    assert calls["n"] == 3
    # First poll ~0.5 s, exponential backoff (x2), capped at 5 s.
    assert sleeps == [0.5, 1.0]
    await client.aclose()


async def test_wait_ready_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "_READY_POLL_START_SECONDS", 0.001)
    monkeypatch.setattr(client_module, "_READY_POLL_MAX_SECONDS", 0.002)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"running": True, "ready": False, "port": 3000})

    client = _client(handler)
    with pytest.raises(SandboxNotReadyError):
        await client.wait_ready(_handle(), timeout_s=0.02)
    await client.aclose()


async def test_dev_logs_maps_next_to_next_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("since") == "5"
        assert request.headers.get("authorization") == "Bearer tok-secret"
        return httpx.Response(200, json={"lines": ["a", "b"], "next": 7})

    client = _client(handler)
    logs = await client.dev_logs(_handle(), since=5)
    assert logs.lines == ["a", "b"]
    assert logs.next_cursor == 7
    await client.aclose()


async def test_exec_504_maps_to_plain_sandbox_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, json={"detail": "exec timed out"})

    client = _client(handler)
    with pytest.raises(SandboxError) as excinfo:
        await client.exec(_handle(), ["sleep", "999"])
    assert excinfo.type is SandboxError  # not the retryable NotReady/terminal Gone subclass
    await client.aclose()


async def test_connection_error_maps_to_sandbox_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    client = _client(handler)
    with pytest.raises(SandboxError):
        await client.dev_status(_handle())
    await client.aclose()


async def test_files_422_maps_to_sandbox_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "No match found for old_str"})

    client = _client(handler)
    with pytest.raises(SandboxError):
        await client.files(_handle(), FileStrReplace(path="p", old_str="x", new_str="y"))
    await client.aclose()


async def test_preview_url_never_carries_the_token() -> None:
    handle = _handle(token="super-secret")
    assert "super-secret" not in handle.preview_url


async def test_get_sandbox_unconfigured_raises() -> None:
    client_module.reset_sandbox_for_tests()
    # `.env.test` sets no SANDBOX__* block, so settings.sandbox is None.
    with pytest.raises(SandboxNotConfiguredError):
        client_module.get_sandbox()


async def test_aclose_sandbox_is_a_noop_when_never_opened() -> None:
    client_module.reset_sandbox_for_tests()
    await client_module.aclose_sandbox_singleton()  # no raise


async def test_reset_sandbox_for_tests_drops_the_singleton() -> None:
    c = create_sandbox(_config())
    client_module.set_sandbox_for_tests(c)
    assert client_module._sandbox_singleton is c
    client_module.reset_sandbox_for_tests()
    assert client_module._sandbox_singleton is None
    await c.aclose()
