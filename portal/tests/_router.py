"""Docker harness for the portal edge's apps router tests.

A private helper (leading underscore, so pytest never collects it as a test module) holding the
container control and the HTTP/WebSocket client the fixtures and tests share. Mirrors
`sandbox/tests/_docker.py`, which is the repo's existing shape for a Docker-backed suite.

WHY DOCKER AND NOT A STRING ASSERTION. Six implementation units land against this router before
a request from a real BIAL desk ever traverses it, and the failure modes this design is most
exposed to are the ones a structural test cannot see: a missing `proxy_http_version` answers a
WebSocket upgrade as an ordinary request, a `proxy_pass` with a stray URI collapses every
request to `/`, and a keyless request reaches the right container with the wrong path. All three
leave `nginx -t` green. The Vitest suite next door pins the config's SHAPE; this one pins its
BEHAVIOUR, and only one of the two would have caught any of those.

Every subprocess call uses list-form args (no shell), so there is no shell-injection surface.
"""

from __future__ import annotations

import http.client
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

PORTAL_DIR = Path(__file__).resolve().parent.parent

ROUTER_IMAGE = "bial-portal-router-test"
STUB_IMAGE = "bial-portal-stub-test"

# The test deployment inputs. APPS_DOMAIN is the (fake) Container Apps environment domain the
# router composes upstreams from; APPS_HOSTNAME is the public name a browser would use.
APPS_DOMAIN = "bial-apps.test"
APPS_HOSTNAME = "apps.bial.test"
PORTAL_ORIGIN = "https://portal.bial.test"

# 28 lowercase hex, exactly as `app_name_for` / `published_app_name` mint them.
SBX_KEY = "sbx-" + "1a2b3c4d5e6f70819a2b3c4d5e6f"
PUB_KEY = "pub-" + "1a2b3c4d5e6f70819a2b3c4d5e6f"
OTHER_SBX_KEY = "sbx-" + "99887766554433221100aabbccdd"
# Correctly shaped and deliberately NOT given a DNS alias: this is what an expired sandbox or a
# mistyped-but-plausible key looks like to the router.
GHOST_KEY = "sbx-" + "deadbeefdeadbeefdeadbeefdead"


def docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


requires_docker = pytest.mark.skipif(not docker_available(), reason="needs a running Docker")


def _run(args: list[str], **kw: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, **kw)  # type: ignore[arg-type]


def _build(tag: str, dockerfile: str) -> None:
    proc = _run(
        ["docker", "build", "-f", f"tests/{dockerfile}", "-t", tag, "."],
        cwd=PORTAL_DIR,
        timeout=900,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"docker build {dockerfile} failed:\n{proc.stderr.decode()[-4000:]}")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


@dataclass
class Router:
    """The router under test, addressed from the host over a published port."""

    port: int
    container: str

    def request(
        self,
        target: str,
        *,
        host: str = APPS_HOSTNAME,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], str]:
        """One HTTP request. Returns (status, headers-lowercased, body-as-text).

        `http.client` is used rather than a higher-level client on purpose: it sends the
        request target BYTE FOR BYTE as given, so a test can drive `/a/<key>/../` and see where
        nginx's own normalization lands it. Most clients would normalize that away first and
        quietly assert nothing.
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        hdrs = {"Host": host, **(headers or {})}
        try:
            conn.request(method, target, body=body, headers=hdrs)
            resp = conn.getresponse()
            payload = resp.read().decode("utf-8", "replace")
            got = {k.lower(): v for k, v in resp.getheaders()}
            # getheaders() collapses repeats; keep every Set-Cookie for the "exactly one" test.
            cookies = resp.headers.get_all("Set-Cookie") or []
            got["__set_cookie_count"] = str(len(cookies))
            return resp.status, got, payload
        finally:
            conn.close()

    def websocket(
        self, target: str, *, host: str = APPS_HOSTNAME, headers: dict[str, str] | None = None
    ) -> tuple[int, str]:
        """Attempt a real RFC 6455 upgrade. Returns (status, raw response head).

        A 101 here is the only assertion that proves live reload survives the hop. Checking that
        the `Upgrade` header was forwarded proves strictly less: under HTTP/1.0 nginx forwards
        the header and still answers the request as an ordinary one.
        """
        key = "x3JJHMbDL1EzLkh9GBhXDw=="  # the RFC's own example key; nothing secret
        lines = [
            f"GET {target} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode()
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=20)
        try:
            sock.sendall(raw)
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        finally:
            sock.close()
        head = buf.decode("latin-1").split("\r\n\r\n")[0]
        status = int(head.split(" ")[1]) if head.startswith("HTTP/") else 0
        return status, head


def boot_router(env: dict[str, str], *, network: str) -> subprocess.CompletedProcess[bytes]:
    """Start the router with `env` and WAIT FOR IT TO SETTLE, returning its logs.

    Used by the boot-guard tests, where the expected outcome is a container that refuses to
    start. The guard's whole value is that a malformed input is a refused boot rather than a
    config that loads and misroutes, so the assertion has to be on the exit, not on a response.
    """
    name = f"router-guard-{uuid.uuid4().hex[:8]}"
    args = ["docker", "run", "--name", name, "--network", network]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    args += [ROUTER_IMAGE]
    try:
        # The image runs nginx in the foreground, so a SUCCESSFUL boot never returns. The guard
        # cases all exit non-zero within milliseconds; the timeout is the failure signal.
        return _run(args, timeout=25)
    except subprocess.TimeoutExpired:
        _run(["docker", "kill", name], timeout=30)
        return subprocess.CompletedProcess(args, 0, b"", b"__STAYED_UP__")
    finally:
        _run(["docker", "rm", "-f", name], timeout=60)


def _wait_for_router(r: Router, container: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r.request("/", host="unmatched.invalid")
            return
        except OSError:
            time.sleep(0.4)
    logs = _run(["docker", "logs", container], timeout=30)
    raise RuntimeError(f"router never came up:\n{logs.stderr.decode()[-4000:]}")
