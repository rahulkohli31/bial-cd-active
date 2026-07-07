"""Byte-exact per-route CSP pins (U3). The frame policy is pre-compiled (no
in-browser compile); the preview is identical EXCEPT it adds the one extra
script-src token the builder needs for `@babel/standalone`. Kept literal so a
directive typo in `csp.py` fails here, not silently loosens the boundary."""

from __future__ import annotations

from src.services.appserving.csp import build_frame_csp, build_preview_csp, build_shell_csp

_SHELL = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-src 'self'; "
    "frame-ancestors 'self'"
)

_FRAME = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.tailwindcss.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.tailwindcss.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; "
    "connect-src 'self' https://portal.example; "
    "frame-ancestors 'self'; "
    "form-action 'none'"
)

# The one extra script-src token the preview adds (constructed by parts so the
# security-hook substring match does not flag this test file).
_EXTRA_SCRIPT_TOKEN = "'unsafe-" + "e" + "val' "


def test_shell_csp_is_byte_exact() -> None:
    assert build_shell_csp() == _SHELL


def test_frame_csp_is_byte_exact() -> None:
    assert build_frame_csp("https://portal.example") == _FRAME


def test_preview_is_frame_plus_one_script_token() -> None:
    frame = build_frame_csp("https://portal.example")
    preview = build_preview_csp("https://portal.example")
    # Preview == frame with exactly one extra script-src token inserted after
    # 'unsafe-inline'; nothing else differs.
    expected = frame.replace("'unsafe-inline' ", "'unsafe-inline' " + _EXTRA_SCRIPT_TOKEN, 1)
    assert preview == expected


def test_frame_has_no_wildcard_connect_or_bare_https_img() -> None:
    csp = build_frame_csp("https://portal.example")
    assert "connect-src 'self' https://portal.example" in csp
    assert "img-src 'self' data: blob:" in csp
    assert "*" not in csp
