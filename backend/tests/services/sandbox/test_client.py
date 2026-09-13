"""The C2 client's `/_sup/*` supervisor HTTP layer + accessor.

An `httpx.MockTransport` stands in for the C1 supervisor: no live container, every
wire shape asserted against `sandbox/supervisor/app.py`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import cast

import httpx
import pytest
from pydantic import SecretStr

from src.services.sandbox import client as client_module
from src.services.sandbox.base import (
    CompileState,
    ExecResult,
    FileStrReplace,
    SandboxClient,
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
        managed_environment_name="aca-env",
        acr_server="bialgenaicr01.azurecr.io",
        acr_username="acr-user",
        acr_password=SecretStr("acr-pass"),
        image_ref="bialgenaicr01.azurecr.io/citizen-dev-sandbox:latest",
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


async def test_files_str_replace_serializes_to_a_flat_body() -> None:
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


@pytest.mark.parametrize(
    ("body", "shape"),
    [
        ({"ok": True}, "no pid at all"),
        ({"pid": "not-a-number"}, "a pid that is not an int"),
        (["pid", 4321], "not even an object"),
    ],
)
async def test_a_malformed_dev_start_body_raises_sandbox_error_not_a_vendor_exception(
    body: object, shape: str
) -> None:
    """★ A 200 whose body is not `{"pid": N}` must raise `SandboxError`, never a raw
    `KeyError`/`TypeError`/`ValueError` — two callers (Write turn's boot-at-attach, relaunch's
    attach arm) guard only `except SandboxError`, so a vendor exception here skips both and
    kills a turn whose workspace had already been reported ready. Same lesson as
    `test_a_malformed_supervisor_reply_does_not_fail_the_provision_either`, one seam over.

    Mutation check: delete the `except (KeyError, TypeError, ValueError)` arm in `dev_start` and
    each case raises its own vendor exception instead of `SandboxError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(SandboxError):
        await _client(handler).dev_start(_handle())


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


# --- the root_status wire seam ----------------------------------------------------------------
#
# These four lines in `dev_status` are the ONLY place the supervisor's `root_status` enters the
# control plane. Every consumer of `DevStatus.shows_a_page` — the frame decision, the status the
# citizen is shown, the relaunch arm — sits downstream of them, so a mapping bug here is invisible
# at every one of those sites and surfaces only as a blank white pane on somebody's screen. The
# field also has to survive the fleet it is being rolled out to: containers running an image that
# predates it answer a body with no `root_status` key at all, forever.


def _status_handler(body: object) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/_sup/dev/status"
        assert request.headers.get("authorization") == "Bearer tok-secret"
        return httpx.Response(200, json=body)

    return handler


async def test_a_root_status_on_the_wire_reaches_the_frame_decision() -> None:
    """★ THE MEASURED DEFECT, end to end at the wire. This is the shape the supervisor sent on
    2026-09-10: the dev server was up and answering (`ready` is fail-open and counts the 404), the
    agent had not written `app/page.tsx` yet, and the platform framed it — a blank white pane
    under a live-preview label.

    Asserting `shows_a_page` here and not only the field is the point of the test: the field is
    worth carrying only because the predicate reads it, and nothing else in the suite walks a
    supervisor body all the way to that verdict.

    Mutation check: drop `root_status=...` from the `DevStatus(...)` call in `dev_status` and this
    goes red on the verdict, while every other test in this file stays green."""
    client = _client(
        _status_handler({"running": True, "ready": True, "port": 3000, "root_status": 404})
    )
    status = await client.dev_status(_handle())
    assert status.root_status == 404
    assert status.shows_a_page is False
    await client.aclose()


async def test_a_serving_root_comes_through_the_same_seam() -> None:
    """The companion bound. A mapping hardwired to "no page" would pass the test above and
    suppress the preview on every healthy build instead — the failure that is quiet in telemetry
    and loud with the citizen, because a preview that never uncovers looks like a platform that
    hung."""
    client = _client(
        _status_handler({"running": True, "ready": True, "port": 3000, "root_status": 200})
    )
    status = await client.dev_status(_handle())
    assert status.root_status == 200
    assert status.shows_a_page is True
    await client.aclose()


async def test_a_supervisor_that_never_heard_of_root_status_reads_none_not_an_error() -> None:
    """★ THE GRANDFATHER ARM, and the whole rollout rests on it. Every container provisioned
    before this field shipped answers a `/dev/status` body with no `root_status` key, and there is
    no version handshake to ask with. `.get` is what makes that body legal: an absent field reads
    as "this container cannot say", which `shows_a_page` then resolves to today's behaviour.
    Reading it with `data["root_status"]` instead would raise on the ENTIRE EXISTING FLEET — and
    because that raise is a `SandboxError`, the best-effort callers would swallow it and the
    breakage would present as previews that silently never appear.

    Mutation check: `data.get("root_status")` -> `data["root_status"]` and this goes red with
    `SandboxError: dev/status returned a malformed body`."""
    client = _client(_status_handler({"running": True, "ready": True, "port": 3000}))
    status = await client.dev_status(_handle())
    assert status.root_status is None
    assert status.shows_a_page is True, "an old container keeps framing exactly as it does today"
    await client.aclose()


async def test_a_root_status_sent_as_a_string_is_coerced_not_carried_through() -> None:
    """`int(raw_root)` is deliberate, and this test exists so the next reader knows that rather
    than guessing. The supervisor sends a JSON number today, but it ships in a separately-released
    image, and a string `"404"` carried through uncoerced would be a truthy non-int arriving at a
    `< 400` comparison — `TypeError` at the frame decision, at request time, in production.

    Mutation check: `int(raw_root)` -> `raw_root` and this goes red on the `== 404` identity."""
    client = _client(
        _status_handler({"running": True, "ready": True, "port": 3000, "root_status": "404"})
    )
    status = await client.dev_status(_handle())
    assert status.root_status == 404
    assert status.shows_a_page is False
    await client.aclose()


async def test_a_root_status_that_is_not_a_number_at_all_stays_in_the_taxonomy() -> None:
    """The bound on that coercion. `int("teapot")` is a `ValueError`, and the existing
    `except (KeyError, TypeError, ValueError)` arm is what keeps it a `SandboxError` — the same
    guarantee `port` and `exit_code` already have one line up, now asserted for the new field too.
    Every best-effort caller of `dev_status` guards `except SandboxError` and nothing wider, so a
    raw `ValueError` escaping here would kill a turn over a diagnostic field.

    Mutation check: remove `ValueError` from that except tuple and this raises `ValueError`."""
    client = _client(
        _status_handler({"running": True, "ready": True, "port": 3000, "root_status": "teapot"})
    )
    with pytest.raises(SandboxError):
        await client.dev_status(_handle())
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


async def test_a_files_failure_carries_the_supervisors_own_sentence() -> None:
    """★ THE STATUS ALONE CANNOT BE ACTED ON. Two very different failures answer 400 here — a
    container running an image that predates an action, and a genuine path bug on this side —
    and "files op failed with status 400" is the same sentence for both. Whoever is holding the
    log at 2 a.m. needs the supervisor's own words.

    Safe to carry on a narrow ground, not a broad one: the supervisor does NOT redact everything
    (`_redact` covers `exec` output and `view` content only). What makes it safe is that every
    non-200 `/files` detail is one of a handful of fixed strings, plus `_resolve`'s, which echoes
    the path this side sent — no file bytes reach a non-200 body.

    Mutation check: drop `resp.text[:200]` from the raise and the two content assertions go red.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "unknown files action: create_bytes"})

    client = _client(handler)
    with pytest.raises(SandboxError) as caught:
        await client.files(_handle(), FileStrReplace(path="p", old_str="x", new_str="y"))
    assert "400" in str(caught.value)
    assert "create_bytes" in str(caught.value)
    await client.aclose()


async def test_a_files_failure_body_is_capped_rather_than_carried_whole() -> None:
    """A message that grows with its input is how a log becomes the place a payload lives. The
    cap is small enough that a supervisor sentence survives whole and nothing else does."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="z" * 5_000)

    client = _client(handler)
    with pytest.raises(SandboxError) as caught:
        await client.files(_handle(), FileStrReplace(path="p", old_str="x", new_str="y"))
    assert caught.value.args[0].count("z") == 200
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
    await client_module.aclose_sandbox_singleton()


async def test_reset_sandbox_for_tests_drops_the_singleton() -> None:
    c = create_sandbox(_config())
    client_module.set_sandbox_for_tests(c)
    assert client_module._sandbox_singleton is c
    client_module.reset_sandbox_for_tests()
    assert client_module._sandbox_singleton is None
    await c.aclose()


# --- a fresh container is a working git repo ------------------------------------------------


async def test_a_fresh_provision_leaves_the_workspace_a_git_repo() -> None:
    """★ The golden template ships no `.git`, and only the RESTORE path used to create one — so
    a first build ran against a plain directory even though downstream assumes a repo: the
    agent's per-slice commits (W1), the save-state check's `git rev-parse`/`git status`, and
    `write_snapshot`'s own `git init` fallback (which rescues the save but collapses per-slice
    history into one commit).

    Mutation-check: drop the `_make_it_a_repo` call from `provision_new` and this goes red."""
    commands: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/exec"):
            commands.append(json.loads(request.content)["cmd"])
            return httpx.Response(200, json={"stdout": "", "stderr": "", "exit": 0})
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    await client_module._make_it_a_repo(client, _handle())

    assert len(commands) == 1
    script = commands[0][-1]
    # Idempotent: a repo that already exists (the restore path, or a re-run) is left alone
    # rather than re-initialised over.
    assert "rev-parse --git-dir" in script
    assert "git init" in script
    # And a BASELINE commit, so the agent's commits are real deltas against a known start.
    assert "git add -A" in script
    assert "git commit" in script


async def test_the_repo_init_never_fails_a_container_that_otherwise_came_up() -> None:
    """Best-effort on purpose: a container serving the app is worth having even if this one
    exec failed, and `write_snapshot` still carries its `git init` fallback for that case."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="supervisor is having a day")

    await client_module._make_it_a_repo(_client(handler), _handle())


async def test_a_malformed_supervisor_reply_does_not_fail_the_provision_either() -> None:
    """A 200 whose body is not the exec shape. Narrowly catching `SandboxError` missed this and
    a `KeyError` escaped, taking down a provision that had already succeeded — the exact trade
    this best-effort step exists to avoid."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    await client_module._make_it_a_repo(_client(handler), _handle())


# --- the first-route warm request ----------------------------------------


async def test_a_warm_request_that_times_out_is_swallowed() -> None:
    """★ THE HUNG-WARM-REQUEST GUARD, written first. A hung warm request holding the preview
    frame for the whole turn is strictly WORSE than the blank card this unit exists to remove —
    the citizen would wait longer and see less. Nothing escapes, and the caller gets `None`."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("the route is still compiling", request=request)

    assert await _client(handler).someone_has_to_go_first(_handle()) is None


async def test_a_warm_request_transport_error_is_swallowed_too() -> None:
    """The container can vanish between `wait_ready` and the frame. That is the reaper's
    problem or the watcher's, never this call's."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    assert await _client(handler).someone_has_to_go_first(_handle()) is None


async def test_the_warm_request_goes_in_the_front_door() -> None:
    """It must hit the APP'S OWN ROOT through Caddy's `/*` block — the same door the iframe
    uses — and NOT the bearer-guarded `/_sup/*` supervisor. Warming a path no user takes would
    compile the wrong route and prove nothing.

    THE APP'S ROOT IS NOW `/a/<app-name>`, not `/`. Under a base path the container root belongs
    to no route, so warming `/` compiles the framework's 404 and leaves the first real visitor
    waiting on exactly the cold compile this call exists to absorb — while reporting 200."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text="<html>hello</html>")

    assert await _client(handler).someone_has_to_go_first(_handle()) == 200
    assert seen["path"] == "/a/sbx-abc"
    assert seen["auth"] is None, (
        "the app root is public; the supervisor token has no business here"
    )


async def test_a_compile_error_comes_back_as_a_status_not_an_exception() -> None:
    """A 500 is the SIGNAL, not a failure: it is what makes the `⨯` land in the dev log where
    self-heal reads it. Raising here would turn the most useful outcome into the one that
    suppresses the preview."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Ecmascript file had an error")

    assert await _client(handler).someone_has_to_go_first(_handle()) == 500


async def test_a_warm_request_that_answers_badly_says_so_in_telemetry() -> None:
    """A 500 at the app root is the single most interesting thing this call can learn, and every
    caller discards the return value — so until now only the EXCEPTION path was observable. A
    root route 500ing on every build looked exactly like a healthy one in telemetry, while
    `selfheal.verify` decided green from five text markers and shipped it.

    Distinct event name from `warm_request_failed`: that one means the request never landed at
    all, which says the opposite thing about the app."""
    from structlog.testing import capture_logs

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Ecmascript file had an error")

    with capture_logs() as logs:
        assert await _client(handler).someone_has_to_go_first(_handle()) == 500

    complaints = [entry for entry in logs if entry["event"] == "warm_request_not_ok"]
    assert len(complaints) == 1
    assert complaints[0]["status"] == 500
    assert [entry for entry in logs if entry["event"] == "warm_request_failed"] == []


async def test_a_healthy_warm_request_stays_quiet() -> None:
    """The companion bound. A log line on every successful preview frame is noise, and noise is
    how the 500 case gets ignored when it finally happens."""
    from structlog.testing import capture_logs

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hello</html>")

    with capture_logs() as logs:
        assert await _client(handler).someone_has_to_go_first(_handle()) == 200

    assert [entry for entry in logs if entry["event"] == "warm_request_not_ok"] == []


async def test_cancelling_the_turn_still_cancels_through_the_warm_request() -> None:
    """The blind `except Exception` must NOT eat cancellation. `CancelledError` is a
    `BaseException` in 3.8+, so this passes by construction — pinned because a well-meaning
    `except BaseException` would wedge every cancelled turn behind a warm request."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _client(handler).someone_has_to_go_first(_handle())


async def test_the_warm_request_does_not_follow_the_apps_redirect() -> None:
    """★ SSRF GUARD. The response to this GET is written by unreviewed, agent-authored code in
    the citizen's sandbox. Following its `Location` would let that code choose the control
    plane's next request — a blind GET pivot from inside Azure, fired on every preview frame,
    every relaunch and every self-heal iteration. A 3xx already proves the route compiled,
    which is the entire job, so there is nothing to gain by following it."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/metadata/v1/"})

    status = await _client(handler).someone_has_to_go_first(_handle())

    assert status == 302, "the redirect is REPORTED, not chased"
    assert seen == ["https://app-xyz.westeurope.azurecontainerapps.io/a/sbx-abc"], (
        "exactly one request, to the app's own root — the Location was never fetched"
    )


async def test_the_warm_request_gives_up_on_an_app_that_simply_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE ONLY BOUND THERE IS. `AcaSandboxClient` builds its client with `timeout=None` on
    purpose, so httpx contributes nothing here — `asyncio.timeout` is the entire ceiling, and
    every other warm-request test raises or returns instantly, so none of them would catch its
    deletion. An app that never answers is not exotic: the response is unreviewed, agent-authored
    code, and hanging is precisely the failure this call exists to make visible.

    Mutation check: drop `asyncio.timeout(_WARM_TIMEOUT_SECONDS)` from the `async with` tuple and
    this test hangs until the 10s handler sleep, then fails on the elapsed bound."""
    monkeypatch.setattr(client_module, "_WARM_TIMEOUT_SECONDS", 0.05)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)  # never raises, never answers — the hang, not an error
        return httpx.Response(200)

    client = AcaSandboxClient(_config(), transport=httpx.MockTransport(handler))
    started = asyncio.get_running_loop().time()
    status = await client.someone_has_to_go_first(_handle())
    elapsed = asyncio.get_running_loop().time() - started

    assert status is None, "a warm request that timed out reports nothing, and raises nothing"
    assert elapsed < 2.0, f"the warm request ran for {elapsed:.2f}s against a 0.05s budget"


async def test_the_warm_request_never_reads_the_apps_body() -> None:
    """★ MEMORY GUARD. The control plane is a single replica and this call bypasses the
    supervisor, so it is the only buffer in the path. A hostile app answering with an
    unbounded body would OOM the whole platform, not just its own build. Stopping at the
    status line is also all this call ever needed."""

    pulled = {"chunks": 0}

    async def a_body_that_never_ends() -> AsyncIterator[bytes]:
        while True:
            pulled["chunks"] += 1
            yield b"x" * 8192

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=a_body_that_never_ends())

    assert await _client(handler).someone_has_to_go_first(_handle()) == 200
    assert pulled["chunks"] == 0, (
        "the status line is the whole answer — pulling even one chunk of an app-controlled "
        "body is the start of an unbounded read the control plane cannot afford"
    )


# --- the compile-state transport ----------------------------------------------------
#
# The contract this section pins is one sentence: NO failure of this call may produce a
# confidently-clean reading, and no failure may reach the caller as an exception.


def _compile_handler(status: int, body: object) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/_sup/dev/compile"
        assert request.headers.get("authorization") == "Bearer tok-secret"
        return httpx.Response(status, json=body)

    return handler


async def test_compile_state_maps_a_failed_report_with_its_errors() -> None:
    report = await _client(
        _compile_handler(
            200,
            {
                "state": "failed",
                "errors": ["./app/page.tsx\nModule not found"],
                "reason": None,
                "connect_generation": 4,
            },
        )
    ).compile_state(_handle())
    assert report.state is CompileState.FAILED
    assert report.errors == ("./app/page.tsx\nModule not found",)
    assert report.connect_generation == 4
    assert report.protocol_drifted is False


async def test_compile_state_maps_clean_and_building() -> None:
    clean = await _client(_compile_handler(200, {"state": "clean", "errors": []})).compile_state(
        _handle()
    )
    building = await _client(_compile_handler(200, {"state": "building"})).compile_state(_handle())
    assert clean.state is CompileState.CLEAN
    assert building.state is CompileState.BUILDING


async def test_an_old_supervisor_image_reads_unknown_not_clean() -> None:
    """THE FLEET CASE. Every container provisioned before `/dev/compile` existed answers 404
    forever, and `/health` returns only `{"ok": true}` so the control plane cannot tell by
    asking. A 404 read as clean would uncover the preview over a red screen on the entire
    existing fleet — which is the population this work exists to reach."""
    report = await _client(_compile_handler(404, {"detail": "Not Found"})).compile_state(_handle())
    assert report.state is CompileState.UNKNOWN
    assert report.reason == "endpoint_absent"


async def test_a_transport_error_reads_unknown_and_does_not_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    report = await _client(handler).compile_state(_handle())
    assert report.state is CompileState.UNKNOWN
    assert report.reason == "transport_error"


async def test_a_non_200_non_404_reads_unknown_and_does_not_raise() -> None:
    report = await _client(_compile_handler(503, {"detail": "unavailable"})).compile_state(
        _handle()
    )
    assert report.state is CompileState.UNKNOWN
    assert report.reason == "status_error"


async def test_a_malformed_body_reads_unknown_and_does_not_raise() -> None:
    """Every other method on this client turns a malformed 200 into a `SandboxError`. This one
    does not, on purpose: an exception here would have to be handled at every call site, and
    the one that forgot would take a turn down over a diagnostic signal."""
    for body in ({"no": "state"}, ["not", "an", "object"]):
        report = await _client(_compile_handler(200, body)).compile_state(_handle())
        assert report.state is CompileState.UNKNOWN
        assert report.reason == "malformed_body"


async def test_an_unrecognised_state_string_reads_unknown_not_a_guess() -> None:
    """The supervisor and this client ship in separate images and can be a release apart in
    either direction, so a value one of them has not heard of is a real state — and the only
    safe reading of a state we do not understand is that we do not understand it."""
    for body in ({"state": "recompiling"}, {"state": None}):
        report = await _client(_compile_handler(200, body)).compile_state(_handle())
        assert report.state is CompileState.UNKNOWN
        assert report.reason == "unrecognised_state"


async def test_the_drift_canary_is_recognised_from_the_reason() -> None:
    """A successful connect that produced no recognisable frame — the ONE reading that means
    the upstream protocol moved, as opposed to the socket merely being down. Derived on the
    report so no call site re-spells the reason string."""
    drifted = await _client(
        _compile_handler(
            200, {"state": "unknown", "reason": "no_recognised_frame", "connect_generation": 2}
        )
    ).compile_state(_handle())
    down = await _client(
        _compile_handler(200, {"state": "unknown", "reason": "disconnected"})
    ).compile_state(_handle())
    assert drifted.protocol_drifted is True
    assert down.protocol_drifted is False


async def test_the_default_client_declines_with_unknown_rather_than_clean() -> None:
    """`SandboxClient.compile_state` is non-abstract so the frozen C2 set stays frozen (the
    `someone_has_to_go_first` precedent). Its default has to DECLINE, and declining is
    `unknown` — a default of `clean` would make every client that never implements it report
    a healthy app it has never looked at.

    Called unbound: the default touches no state, and constructing a concrete subclass would
    mean stubbing all ten abstract C2 methods to assert one line."""
    nobody = cast(SandboxClient, None)  # the default reads no state off `self`
    report = await SandboxClient.compile_state(nobody, _handle())
    assert report.state is CompileState.UNKNOWN
    assert report.reason == "no_sandbox_client"


# --- what the app is actually serving ---------------------------------------------------------
#
# `what_is_it_serving` feeds self-heal's evidence of the build's health, and is the probe with
# the most to lose from a base path: a framework 404 read as "what the app serves" becomes
# "make sure `app/page.tsx` exists", burning metered tokens repairing a file that was never wrong.


def _streamed(status: int, body: str) -> httpx.Response:
    """A response whose body must be STREAMED, matching what the probe actually does.

    `httpx.Response(status, text=...)` sets the content eagerly, and `aiter_raw()` — which the
    probe uses deliberately, to bound the read over WIRE bytes rather than decoded ones — refuses
    it. A test built on the eager form would not exercise the read path at all.
    """

    async def _chunks() -> AsyncIterator[bytes]:
        yield body.encode()

    return httpx.Response(status, content=_chunks())


async def test_the_serving_probe_reads_the_apps_own_page_not_the_container_root() -> None:
    """THE MUTANT THAT MUST FAIL. Point this back at `handle.preview_url` and it goes red.

    Under a base path the container root is a route that does not exist. Both responses below
    are a legitimate 200/404 pair, so nothing about the STATUS distinguishes them — only which
    URL was asked for does.
    """
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path == "/a/sbx-abc":
            return _streamed(200, "<html><body>the citizen's app</body></html>")
        return _streamed(404, "<html><body>This page could not be found</body></html>")

    served = await _client(handler).what_is_it_serving(_handle())

    assert asked == ["/a/sbx-abc"]
    assert served is not None
    assert served.status == 200
    assert "the citizen's app" in served.head


async def test_the_serving_probe_still_reports_a_real_404_as_a_404() -> None:
    """Moving the probe must not make it fail-open. An app that genuinely has no page at its own
    root is UNHEALTHY, and saying so is the whole point of the verdict."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return _streamed(404, "<html>nothing here</html>")

    served = await _client(handler).what_is_it_serving(_handle())
    assert served is not None and served.status == 404


async def test_the_serving_probe_sends_no_bearer_to_the_app() -> None:
    """The app's own root is public and is written by unreviewed, agent-authored code. The
    supervisor token has no business travelling there."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return _streamed(200, "<html>ok</html>")

    await _client(handler).what_is_it_serving(_handle())
    assert seen["auth"] is None
