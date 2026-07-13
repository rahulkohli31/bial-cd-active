"""Per-route Content-Security-Policy builders (R20) — byte-exact ports of the
Express `buildShellCsp` / `buildFrameCsp`.

The two policies encode the trust model:
* **Shell** (same-origin host page): tight — no CDN egress, no eval.
* **Frame** (opaque-origin sandbox serving PRE-COMPILED app JS): CDN `script-src`
  for React/Tailwind but **no `'unsafe-eval'`** (the JS is already compiled) and
  `connect-src 'self' <origin>` — the portal origin is the ONLY egress, so model
  code cannot beacon the injected token out. `img-src 'self' data: blob:` (never a
  bare `https:`), `form-action 'none'`.

(The old builder-`/preview` policy — frame + `'unsafe-eval'` for in-browser JSX
compilation — is retired with the `/preview` shell; the Phase-2 preview is served by
the sandbox's own Caddy, cross-origin, with its own CSP — C8.)

Any widening here is a real exfiltration boundary regression — keep these strings
in lockstep with the runner tests, which assert them verbatim.
"""

from __future__ import annotations

from fastapi import Request


def origin_of(request: Request) -> str:
    """`scheme://host` of the request (Express `originOf`). Behind the prod edge,
    ProxyHeadersMiddleware has already corrected scheme/host."""
    host = request.headers.get("host", "")
    return f"{request.url.scheme}://{host}"


def build_shell_csp() -> str:
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src 'self' data: https://fonts.gstatic.com",
            "img-src 'self' data:",
            "connect-src 'self'",
            "frame-src 'self'",
            "frame-ancestors 'self'",
        ]
    )


def build_frame_csp(origin: str) -> str:
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.tailwindcss.com",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.tailwindcss.com",
            "font-src 'self' data: https://fonts.gstatic.com",
            "img-src 'self' data: blob:",
            f"connect-src 'self' {origin}",
            "frame-ancestors 'self'",
            "form-action 'none'",
        ]
    )
