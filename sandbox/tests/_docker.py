"""Low-level Docker container control for the Track SANDBOX integration harness.

A private helper (leading underscore → pytest never collects it as a test module) that both
`conftest.py` fixtures and the U15 `snapshot_ref_client.py` build on. It owns exactly three
concerns: (1) is Docker usable, (2) build/resolve the sandbox image, (3) run one sandbox
container and speak the C1 supervisor HTTP API to it over the Caddy `:8080` ingress.

The container is the real pre-baked image (`sandbox/Dockerfile.sandbox`) — Caddy (`:8080`) →
supervisor (`:9000`) + `next dev` (`:3000`). We map the container's `8080` to an ephemeral host
port so many containers can run at once without collision (the snapshot round-trip runs two).

Every subprocess call uses list-form args (no shell), so there is no shell-injection surface.
"""

from __future__ import annotations

import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

# Default image tag + a throwaway supervisor token (test-only; never a real secret).
DEFAULT_IMAGE = "bial-sandbox-test"
SUPERVISOR_TOKEN = "supervisor-test-token-not-a-secret"  # noqa: S105 - test fixture value

# `sandbox/` — the docker build context (this file lives at sandbox/tests/_docker.py).
SANDBOX_DIR = Path(__file__).resolve().parent.parent


def docker_available() -> bool:
    """True iff a `docker` CLI is present AND the daemon answers — so the integration lane
    skips cleanly (not errors) on a machine without Docker."""
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def image_exists(tag: str) -> bool:
    r = subprocess.run(["docker", "image", "inspect", tag], capture_output=True)
    return r.returncode == 0


def build_image(tag: str = DEFAULT_IMAGE) -> str:
    """Build the sandbox base image from `sandbox/`. Slow (bakes node_modules) — callers build
    once per session. NOTE: this is the local dev-loop build; the SHIPPED artifact is the Windows
    `az acr build` (ADR-0015 / plan D9), which this does not stand in for."""
    proc = subprocess.run(
        ["docker", "build", "-f", "Dockerfile.sandbox", "-t", tag, "."],
        cwd=SANDBOX_DIR,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"docker build failed:\n{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}")
    return tag


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Sandbox:
    """A running sandbox container + a thin C1 client over its Caddy ingress."""

    name: str
    port: int  # host port mapped to the container's 8080 (Caddy ingress)
    token: str

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def sup(self) -> str:
        """The `/_sup` prefix Caddy strips before forwarding to the supervisor (C1)."""
        return f"{self.base}/_sup"

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def wait_health(self, timeout: float = 60.0) -> None:
        """Poll the supervisor's open `/_sup/health` until it answers `{"ok": true}`."""
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"{self.sup}/health", timeout=2.0)
                if r.status_code == 200 and r.json().get("ok") is True:
                    return
            except httpx.HTTPError as e:  # not up yet
                last = e
            time.sleep(0.5)
        logs = _logs(self.name)
        raise TimeoutError(f"supervisor never became healthy in {timeout}s; last={last}\n{logs}")

    # --- C1 surface (authed unless noted) --------------------------------------------------
    def health(self) -> httpx.Response:
        return httpx.get(f"{self.sup}/health", timeout=5.0)

    def exec_cmd(
        self, cmd: list[str], *, cwd: str | None = None, timeout: int = 900
    ) -> httpx.Response:
        """POST /_sup/exec (C1). Returns the raw response; a non-zero `exit` is a normal 200."""
        body: dict[str, object] = {"cmd": cmd, "timeout": timeout}
        if cwd is not None:
            body["cwd"] = cwd
        return httpx.post(
            f"{self.sup}/exec", json=body, headers=self._auth(), timeout=timeout + 30
        )

    def files(self, body: dict[str, object]) -> httpx.Response:
        return httpx.post(f"{self.sup}/files", json=body, headers=self._auth(), timeout=30.0)

    def dev_start(self, cmd: list[str] | None = None) -> httpx.Response:
        body = {"cmd": cmd} if cmd is not None else {}
        return httpx.post(f"{self.sup}/dev/start", json=body, headers=self._auth(), timeout=30.0)

    def dev_status(self) -> httpx.Response:
        return httpx.get(f"{self.sup}/dev/status", headers=self._auth(), timeout=10.0)

    def dev_logs(self, since: int = 0) -> httpx.Response:
        return httpx.get(
            f"{self.sup}/dev/logs", params={"since": since}, headers=self._auth(), timeout=10.0
        )

    def raw_head(self, path: str = "/") -> httpx.Response:
        """A HEAD to the un-prefixed `next dev` block (`/`) — used for the C8 framing headers,
        which Caddy emits even on the 502 when `next dev` is not started yet (verified)."""
        return httpx.head(f"{self.base}{path}", timeout=10.0)

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


def _logs(name: str) -> str:
    r = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
    return (r.stdout + r.stderr)[-4000:]


def run_sandbox(
    env: dict[str, str] | None = None,
    *,
    image: str = DEFAULT_IMAGE,
    token: str = SUPERVISOR_TOKEN,
    wait: bool = True,
    health_timeout: float = 60.0,
) -> Sandbox:
    """`docker run -d` the sandbox image with `SUPERVISOR_TOKEN` + `env`, mapped to an ephemeral
    host port. Waits for the supervisor to answer health (unless `wait=False`, e.g. the fail-fast
    no-token case). Caller owns teardown via `Sandbox.stop()`."""
    port = _free_port()
    name = f"bial-sbx-{uuid.uuid4().hex[:10]}"
    full_env = {"SUPERVISOR_TOKEN": token, **(env or {})}
    args = ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8080"]
    for k, v in full_env.items():
        args += ["-e", f"{k}={v}"]
    args += [image]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"docker run failed: {proc.stderr}")
    sbx = Sandbox(name=name, port=port, token=token)
    if wait:
        try:
            sbx.wait_health(health_timeout)
        except Exception:
            sbx.stop()
            raise
    return sbx


def import_supervisor(
    image: str = DEFAULT_IMAGE, *, token: str | None = None, timeout: float = 60.0
) -> tuple[int | None, str]:
    """Import the supervisor module (`python3 -c "import app"`) INSIDE the image, with or without
    `SUPERVISOR_TOKEN`. Returns `(returncode, output)`. This exercises the exact fail-first
    guard the shipped container hits (app.py resolves the token at MODULE import → `KeyError` when
    absent → the entrypoint's `exec uvicorn` dies as PID 1) WITHOUT booting caddy or the uvicorn
    server, so it is fast + deterministic, not sensitive to container-startup timing under load.
    `returncode=None` means our own wall-clock timeout fired."""
    args = ["docker", "run", "--rm", "--entrypoint", "python3", "-w", "/supervisor"]
    if token is not None:
        args += ["-e", f"SUPERVISOR_TOKEN={token}"]
    args += [image, "-c", "import app"]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return None, str(e.stdout or "")[-4000:]  # text=True → stdout is str|None
    return proc.returncode, (proc.stdout + proc.stderr)[-4000:]
