"""Concrete C2 `SandboxClient` — the control-plane wrapper over the C1 supervisor.

This is the ACA/helper client SESSION-API supplies in Wave 1 behind the frozen C2
ABC (`base.py`). It has two halves:

* **The `/_sup/*` supervisor HTTP layer** (U1, this file's methods `run` / `files`
  / `dev_start` / `dev_status` / `dev_logs` / `wait_ready`) — everything BRAIN calls
  through the injected client, plus the readiness poll. Each call goes to
  `https://{handle.fqdn}/_sup/<endpoint>` with `Authorization: Bearer {handle.token}`;
  Caddy strips `/_sup`, so the supervisor sees the C1 paths (C1 / C2). A non-zero
  `ExecResult.exit` is a NORMAL return, never an exception (C1).
* **The ACA lifecycle** (U2, `provision_new` / `attach_existing` /
  `restore_from_snapshot` / `teardown`) — the container create/delete, the C5
  registry hash, the `token_ref` map, and the C4 restore pull.

Accessor mirrors `services/redis/client.py`: a module singleton, lazy `settings`
import (avoids the `src.config` cycle), a typed `SandboxNotConfiguredError` when
`settings.sandbox is None` (fail-first — never returns `None`), and an isolated
`aclose`. `set_sandbox_for_tests` lets the reaper's client (which reads the
singleton, not a `Depends`) be injected in tests (KTD-9). NEVER log a token or a
`token_ref` (security.md).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Final

import httpx
import structlog

from src.services.sandbox.base import (
    DevLogs,
    DevStatus,
    ExecResult,
    FileOp,
    FileResult,
    SandboxClient,
    SandboxError,
    SandboxHandle,
    SandboxNotReadyError,
)
from src.services.sandbox.config import SandboxConfig

_log = structlog.get_logger()

# Caddy routes `/_sup/*` to the supervisor (stripping the prefix); every call the
# client makes carries it. `handle.fqdn` is host-only (no scheme), so we prepend https.
_SUP_PREFIX: Final = "/_sup"

# Per-request timeouts (seconds). A command run gets the caller's `timeout_s` plus
# head-room for the round trip so a C1 504 (which IS a `SandboxError`) is the
# supervisor's own timeout, not the transport pre-empting it. Dev/files ops are quick.
_EXEC_TIMEOUT_BUFFER_SECONDS: Final = 30.0
_OP_TIMEOUT_SECONDS: Final = 30.0

# Readiness-poll cadence (C2): start ~0.5 s, exponential backoff capped ~5 s, poll
# until `dev/status.ready` or `timeout_s`. Module-level so tests can shrink them.
_READY_POLL_START_SECONDS: Final = 0.5
_READY_POLL_MAX_SECONDS: Final = 5.0
_READY_BACKOFF_FACTOR: Final = 2.0

# `dev_start` returns the child pid; on C1's 409 "already running" the running dev
# server exposes no pid (C1 `/dev/status` = {running, ready, port}), so we confirm it
# is up and return this sentinel — 0 is never a real Popen pid, so it unambiguously
# reads as "already running, pid unknown" without raising (idempotent, C2).
_ALREADY_RUNNING_PID: Final = 0


class SandboxNotConfiguredError(SandboxError):
    """`get_sandbox()` was called but no sandbox is configured (genuinely-optional in
    dev/test). Mirrors `RedisNotConfiguredError` / storage's `StorageError`: a
    dedicated type lets a caller narrow-catch the unset-sandbox case rather than a
    bare `SandboxError`."""


async def _asleep(seconds: float) -> None:
    """Poll-cadence sleep behind one indirection so tests can record the backoff
    schedule without real waits."""
    await asyncio.sleep(seconds)


class AcaSandboxClient(SandboxClient):
    """The concrete C2 client. Holds one long-lived `httpx.AsyncClient` for the
    supervisor calls; the ACA management client + credential land in U2.

    `transport` is injectable so tests drive the `/_sup/*` layer with an
    `httpx.MockTransport` standing in for the supervisor (no live container)."""

    def __init__(
        self, config: SandboxConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        # No global timeout — every call passes an explicit per-op timeout (a command
        # run may legitimately last for `timeout_s` seconds, far past a default 5 s).
        self._http = httpx.AsyncClient(transport=transport, timeout=None)
        # token_ref (the C5 registry reference) -> the live bearer token. In-process
        # only; a restart empties it -> references resolve to nothing -> SandboxGoneError
        # -> the restore path (KTD-7). Populated in U2.
        self._token_refs: dict[str, str] = {}

    # --- supervisor HTTP layer (U1) ------------------------------------------

    @staticmethod
    def _auth(handle: SandboxHandle) -> dict[str, str]:
        return {"Authorization": f"Bearer {handle.token}"}

    def _url(self, handle: SandboxHandle, endpoint: str) -> str:
        return f"https://{handle.fqdn}{_SUP_PREFIX}/{endpoint}"

    async def _post(
        self, handle: SandboxHandle, endpoint: str, body: dict[str, Any], *, timeout: float
    ) -> httpx.Response:
        try:
            return await self._http.post(
                self._url(handle, endpoint),
                json=body,
                headers=self._auth(handle),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            # Timeout / connect / unreachable supervisor — no vendor type crosses the port.
            raise SandboxError(f"supervisor {endpoint} request failed") from exc

    async def _get(
        self,
        handle: SandboxHandle,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float,
    ) -> httpx.Response:
        try:
            return await self._http.get(
                self._url(handle, endpoint),
                params=params,
                headers=self._auth(handle),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise SandboxError(f"supervisor {endpoint} request failed") from exc

    async def exec(
        self,
        handle: SandboxHandle,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout_s: int = 900,
    ) -> ExecResult:
        body: dict[str, Any] = {"cmd": cmd, "timeout": timeout_s}
        if cwd is not None:
            body["cwd"] = cwd
        resp = await self._post(
            handle, "exec", body, timeout=timeout_s + _EXEC_TIMEOUT_BUFFER_SECONDS
        )
        if resp.status_code != 200:
            # C1 504 (supervisor timeout) and any other non-200 are a SandboxError; a
            # non-zero EXIT would have come back inside a 200 (handled below).
            raise SandboxError(f"command run failed with status {resp.status_code}")
        data: Any = resp.json()
        return ExecResult(
            stdout=str(data["stdout"]), stderr=str(data["stderr"]), exit=int(data["exit"])
        )

    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        # Serialize the validated variant back to C1's flat `FilesBody` wire shape;
        # `exclude_none` drops the fields this variant doesn't carry.
        body = op.model_dump(exclude_none=True)
        resp = await self._post(handle, "files", body, timeout=_OP_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            # C1 422 (0/N str_replace matches) and 400 (missing sub-field / unknown
            # action) both surface as SandboxError (C2).
            raise SandboxError(f"files op failed with status {resp.status_code}")
        data: Any = resp.json()
        detail: dict[str, object] = {str(k): v for k, v in data.items() if k != "ok"}
        return FileResult(ok=bool(data.get("ok", True)), detail=detail)

    async def dev_start(
        self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
    ) -> int:
        body: dict[str, Any] = {}
        if cmd is not None:
            body["cmd"] = cmd
        if cwd is not None:
            body["cwd"] = cwd
        resp = await self._post(handle, "dev/start", body, timeout=_OP_TIMEOUT_SECONDS)
        if resp.status_code == 409:
            # Idempotent: the dev server is already running. Confirm via a status probe
            # (C2) and return the already-running sentinel — C1 exposes no pid here.
            status = await self.dev_status(handle)
            if status.running:
                return _ALREADY_RUNNING_PID
            raise SandboxError("dev/start reported 409 but the server is not running")
        if resp.status_code != 200:
            raise SandboxError(f"dev/start failed with status {resp.status_code}")
        data: Any = resp.json()
        return int(data["pid"])

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        resp = await self._get(handle, "dev/status", timeout=_OP_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise SandboxError(f"dev/status failed with status {resp.status_code}")
        data: Any = resp.json()
        return DevStatus(
            running=bool(data["running"]), ready=bool(data["ready"]), port=int(data["port"])
        )

    async def dev_logs(self, handle: SandboxHandle, *, since: int = 0) -> DevLogs:
        resp = await self._get(
            handle, "dev/logs", params={"since": since}, timeout=_OP_TIMEOUT_SECONDS
        )
        if resp.status_code != 200:
            raise SandboxError(f"dev/logs failed with status {resp.status_code}")
        data: Any = resp.json()
        # Map the C1 wire field `next` -> `DevLogs.next_cursor` (renamed only to avoid
        # shadowing the builtin); pass it back as `since` for only-new lines (C2).
        return DevLogs(lines=[str(line) for line in data["lines"]], next_cursor=int(data["next"]))

    async def wait_ready(
        self, handle: SandboxHandle, *, timeout_s: float = 120.0
    ) -> SandboxHandle:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        delay = _READY_POLL_START_SECONDS
        while True:
            status = await self.dev_status(handle)
            if status.ready:
                return replace(handle, ready=True)
            if loop.time() >= deadline:
                raise SandboxNotReadyError(f"dev server not ready within {timeout_s}s")
            await _asleep(delay)
            delay = min(delay * _READY_BACKOFF_FACTOR, _READY_POLL_MAX_SECONDS)

    # --- ACA lifecycle (filled in U2) ----------------------------------------

    async def provision_new(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        raise NotImplementedError("ACA provision_new lands in U2")

    async def attach_existing(self, user_id: str) -> SandboxHandle:
        raise NotImplementedError("ACA attach_existing lands in U2")

    async def restore_from_snapshot(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        raise NotImplementedError("ACA restore_from_snapshot lands in U2")

    async def teardown(self, handle: SandboxHandle) -> None:
        raise NotImplementedError("ACA teardown lands in U2")

    # --- lifecycle -----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the supervisor HTTP pool. U2 extends this to also close the ACA
        management client + credential. Safe to call more than once."""
        await self._http.aclose()


# --- accessor singleton (mirrors services/redis/client.py) -------------------

_sandbox_singleton: SandboxClient | None = None


def create_sandbox(config: SandboxConfig) -> AcaSandboxClient:
    """Build the concrete client from a `SandboxConfig`. Opens no ACA connection —
    the httpx pool and ACA client connect lazily on first use."""
    return AcaSandboxClient(config)


def get_sandbox() -> SandboxClient:
    """The configured sandbox client (app-level singleton). Raises if the sandbox is
    unset (genuinely-optional in dev/test; the prod gate in `src.config` requires it),
    so a caller never silently gets a `None` (fail-first)."""
    global _sandbox_singleton
    if _sandbox_singleton is None:
        from src.config import settings  # lazy: avoid an import cycle via src.config

        if settings.sandbox is None:
            raise SandboxNotConfiguredError(
                "sandbox is not configured: set SANDBOX__* env, or call get_sandbox() "
                "only where the sandbox is configured (it is required in production)."
            )
        _sandbox_singleton = create_sandbox(settings.sandbox)
    return _sandbox_singleton


async def aclose_sandbox_singleton() -> None:
    """Close the app-global sandbox client and drop the singleton (wired behind
    `lifecycle.aclose_sandbox`). A no-op when never opened. The close is isolated: a
    raise is logged (never swallowed) but the singleton is STILL reset, so a restart
    never reuses a half-closed client (mirrors `aclose_redis` / `aclose_storage`)."""
    global _sandbox_singleton
    client = _sandbox_singleton
    if client is None:
        return
    try:
        # Only the concrete client owns pools/credentials; a test-injected fake has
        # nothing to close.
        if isinstance(client, AcaSandboxClient):
            await client.aclose()
    except Exception:
        _log.exception("sandbox teardown failed during aclose_sandbox")
    finally:
        _sandbox_singleton = None


def set_sandbox_for_tests(client: SandboxClient | None) -> None:
    """Inject (or clear) the singleton so the reaper — which resolves its client via
    the singleton, not a `Depends` — is test-injectable (KTD-9)."""
    global _sandbox_singleton
    _sandbox_singleton = client


def reset_sandbox_for_tests() -> None:
    """Drop the singleton so a suite that builds clients with different configs never
    reuses a stale one across tests."""
    global _sandbox_singleton
    _sandbox_singleton = None
