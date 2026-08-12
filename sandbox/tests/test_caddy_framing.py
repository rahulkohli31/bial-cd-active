"""C8 preview-transport framing — asserted against the REAL image Caddy (integration lane).

The walking skeleton proved framing against an *emulated* Caddy (`scripts/skeleton/frame-proof/
servers.mjs`); this pins the SHIPPED Caddyfile of the real pre-baked image. Byte-exact header
assertions + a wildcard guard + the fail-closed default (backend `test_csp.py` discipline).

Key verified fact: Caddy emits the `handle`-block CSP header **even on the 502** it returns when
`next dev` is not started — so these header assertions need no running dev server (fast + robust).
Requirements R5, R6.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _docker import Sandbox, import_supervisor, run_sandbox

pytestmark = pytest.mark.integration

PORTAL_ORIGIN = "https://portal.example"


@pytest.fixture(scope="module")
def framed(sandbox_image: str) -> Iterator[Sandbox]:
    """A container whose C8 portal origin IS set (the normal provision case)."""
    sbx = run_sandbox({"BIAL_PORTAL_ORIGIN": PORTAL_ORIGIN}, image=sandbox_image)
    try:
        yield sbx
    finally:
        sbx.stop()


@pytest.fixture(scope="module")
def unframed(sandbox_image: str) -> Iterator[Sandbox]:
    """A container whose C8 portal origin is UNSET → the header must fail closed."""
    sbx = run_sandbox({}, image=sandbox_image)
    try:
        yield sbx
    finally:
        sbx.stop()


def _csp(resp) -> str:
    return resp.headers["content-security-policy"]


# --- R5: the next dev block frames ONLY the portal origin, with no XFO -------------------------
def test_next_dev_block_frame_ancestors_is_portal_origin(framed: Sandbox) -> None:
    # HEAD / hits the `handle` (next dev) block; the CSP rides even the 502 (next dev not started).
    resp = framed.raw_head("/")
    assert _csp(resp) == f"frame-ancestors {PORTAL_ORIGIN}"
    # XFO cannot express a cross-origin ancestor; a stray one would block the legit portal frame.
    assert "x-frame-options" not in resp.headers


def test_next_dev_block_has_no_wildcard(framed: Sandbox) -> None:
    # The wildcard guard: a `*` in frame-ancestors would let ANY origin frame the untrusted app.
    assert "*" not in _csp(framed.raw_head("/"))


# --- R5 fail-closed: an unset portal origin yields an EMPTY ancestor list ----------------------
def test_next_dev_block_fails_closed_when_origin_unset(unframed: Sandbox) -> None:
    csp = _csp(unframed.raw_head("/"))
    # Empty ancestor-list = no origin may frame. `{$BIAL_PORTAL_ORIGIN}` expands to "" → just the
    # bare directive (robust to any trailing whitespace the HTTP layer may or may not trim).
    assert csp.split() == ["frame-ancestors"], f"expected fail-closed empty list, got {csp!r}"
    assert "*" not in csp


# --- R5: the /_sup block is fenced with frame-ancestors 'none' + health is open ---------------
def test_sup_block_is_fenced_frame_ancestors_none(framed: Sandbox) -> None:
    # The supervisor holds the token; its block must NEVER be framable, whatever the portal origin.
    resp = framed.raw_head("/_sup/health")
    assert _csp(resp) == "frame-ancestors 'none'"


def test_sup_health_is_open_and_ok(framed: Sandbox) -> None:
    # C1: GET /health is unauthenticated and returns {"ok": true} through the Caddy ingress.
    resp = framed.health()
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# --- R6: the image builds + the entrypoint brings up caddy + uvicorn; no-token fails fast ------
def test_entrypoint_starts_caddy_and_supervisor(framed: Sandbox) -> None:
    # The `framed` fixture only yields after `wait_health` succeeded → both processes are up:
    # Caddy answered `/` (with the CSP header) and the supervisor answered `/_sup/health`.
    assert framed.raw_head("/").headers.get("content-security-policy")
    assert framed.health().json() == {"ok": True}


def test_missing_supervisor_token_fails_fast(sandbox_image: str) -> None:
    # Fail-first (R6): with no SUPERVISOR_TOKEN the supervisor raises `KeyError` at MODULE import
    # (app.py line 38) — so the entrypoint's `exec uvicorn` dies as PID 1 and the container never
    # boots half-configured. Probed via a direct import (no caddy/server) → deterministic.
    rc, out = import_supervisor(sandbox_image, token=None)
    assert rc not in (0, None), f"expected a non-zero exit, got {rc}; output:\n{out}"
    assert "SUPERVISOR_TOKEN" in out and "KeyError" in out


def test_supervisor_imports_cleanly_with_token(sandbox_image: str) -> None:
    # The positive control: WITH the token the module imports cleanly — proving the guard above is
    # specifically the missing token (a fail-first no-default), not an unrelated import failure.
    rc, out = import_supervisor(sandbox_image, token="present-for-this-probe")
    assert rc == 0, f"expected a clean import, got {rc}; output:\n{out}"
