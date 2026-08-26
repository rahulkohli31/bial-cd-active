"""Session fixtures for the apps-router suite.

Run:  cd portal && uv run --project ../backend pytest -m integration
The default lane skips everything here, mirroring sandbox/tests, so `pytest` on a machine
without Docker gives a clean skip rather than a hang.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import pytest
from _router import (
    APPS_DOMAIN,
    APPS_HOSTNAME,
    OTHER_SBX_KEY,
    PORTAL_ORIGIN,
    PUB_KEY,
    ROUTER_IMAGE,
    SBX_KEY,
    STUB_IMAGE,
    Router,
    _build,
    _free_port,
    _run,
)


@pytest.fixture(scope="session")
def docker_network() -> Iterator[str]:
    name = f"bial-router-test-{uuid.uuid4().hex[:8]}"
    _run(["docker", "network", "create", name], timeout=60)
    try:
        yield name
    finally:
        _run(["docker", "network", "rm", name], timeout=60)


@pytest.fixture(scope="session")
def images() -> None:
    _build(ROUTER_IMAGE, "Dockerfile.router")
    _build(STUB_IMAGE, "Dockerfile.stub")


@pytest.fixture(scope="session")
def stub_apps(images: None, docker_network: str) -> Iterator[None]:
    """One stub container answering for several app keys at once.

    Several aliases rather than several containers because the tests that matter here are about
    which NAME the router dialled and which PATH it composed, both of which the stub echoes.
    `GHOST_KEY` is deliberately absent so an unknown key fails as a real DNS miss, which is the
    actual production failure mode — the router holds no registry, so there is nothing to miss
    except a name.
    """
    name = f"stub-{uuid.uuid4().hex[:8]}"
    args = ["docker", "run", "-d", "--name", name, "--network", docker_network]
    for key in (SBX_KEY, PUB_KEY, OTHER_SBX_KEY):
        args += ["--network-alias", f"{key}.{APPS_DOMAIN}"]
    args += [STUB_IMAGE]
    proc = _run(args, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"stub failed to start: {proc.stderr.decode()[-2000:]}")
    try:
        _wait_for_tls(docker_network, f"{SBX_KEY}.{APPS_DOMAIN}")
        yield None
    finally:
        _run(["docker", "rm", "-f", name], timeout=60)


def _wait_for_tls(network: str, host: str, timeout: float = 45.0) -> None:
    """Block until the stub's TLS listener answers. It mints a key at start, which is not
    instant, and a router test that races it fails as a 404 that looks exactly like the bug."""
    deadline = time.monotonic() + timeout
    probe = (
        "import socket,ssl,sys\n"
        "c=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)\n"
        "c.check_hostname=False\nc.verify_mode=ssl.CERT_NONE\n"
        f"s=socket.create_connection(({host!r},443),timeout=3)\n"
        f"c.wrap_socket(s,server_hostname={host!r}).close()\n"
    )
    last = b""
    while time.monotonic() < deadline:
        proc = _run(
            ["docker", "run", "--rm", "--network", network, STUB_IMAGE, "python", "-c", probe],
            timeout=30,
        )
        if proc.returncode == 0:
            return
        last = proc.stderr
        time.sleep(1.0)
    raise RuntimeError(f"stub never answered TLS on {host}: {last.decode()[-1000:]}")


@pytest.fixture(scope="session")
def router(stub_apps: None, docker_network: str) -> Iterator[Router]:
    port = _free_port()
    name = f"router-{uuid.uuid4().hex[:8]}"
    proc = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            docker_network,
            "-p",
            f"127.0.0.1:{port}:8080",
            "-e",
            "PORT=8080",
            "-e",
            "DNS_RESOLVER=127.0.0.11",
            "-e",
            "BACKEND_URL=http://backend-not-used:8000",
            "-e",
            f"APPS_DOMAIN={APPS_DOMAIN}",
            "-e",
            f"APPS_HOSTNAME={APPS_HOSTNAME}",
            "-e",
            f"PORTAL_ORIGIN={PORTAL_ORIGIN}",
            ROUTER_IMAGE,
        ],
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"router failed to start: {proc.stderr.decode()[-2000:]}")
    r = Router(port=port, container=name)
    try:
        _wait_for_router(r, name)
        yield r
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
