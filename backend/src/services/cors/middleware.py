"""The app's ONE CORS layer (R23, review P2) — credentialed CORS for the SPA.

FastAPI's global `CORSMiddleware(allow_origins=[FRONTEND_URL],
allow_credentials=True)` short-circuits EVERY OPTIONS preflight before any
route-level reflection can run, and two stacked Starlette CORS middlewares cannot
be path-scoped — so `main.py` installs this custom ASGI layer *instead of* it.
There is no other CORS layer: delete this and the SPA plus `/v1/auth/*` lose CORS
entirely.

Every cross-origin request gets credentialed CORS for `FRONTEND_URL` only. The
literal `Origin: null` (an opaque-origin iframe) is NEVER reflected. The
null-reflecting, credential-free branch that once served the shared data plane's
`/v1/apps/{id}/records` routes went away with that plane.

A same-origin request (no `Origin` header) passes straight through.
"""

from __future__ import annotations

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SPA_METHODS = "DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT"
_MAX_AGE = "600"


class ScopedCORSMiddleware:
    """One CORS layer for the SPA (replaces the global `CORSMiddleware`)."""

    def __init__(self, app: ASGIApp, *, frontend_url: str) -> None:
        self.app = app
        self.frontend_url = frontend_url

    def _cors_headers(
        self, origin: str, req_headers: Headers, *, preflight: bool
    ) -> dict[str, str]:
        # SPA / auth: credentialed CORS for the configured frontend origin only.
        if origin != self.frontend_url:
            return {}
        headers = {
            "access-control-allow-origin": self.frontend_url,
            "access-control-allow-credentials": "true",
            "vary": "Origin",
        }
        if preflight:
            headers["access-control-allow-methods"] = _SPA_METHODS
            # Reflect the requested headers (a literal "*" is invalid with credentials).
            requested = req_headers.get("access-control-request-headers")
            if requested:
                headers["access-control-allow-headers"] = requested
            headers["access-control-max-age"] = _MAX_AGE
        return headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        req_headers = Headers(scope=scope)
        origin = req_headers.get("origin")
        if origin is None:
            # Same-origin / non-CORS request — no CORS headers.
            await self.app(scope, receive, send)
            return

        preflight = scope["method"] == "OPTIONS" and "access-control-request-method" in req_headers
        cors = self._cors_headers(origin, req_headers, preflight=preflight)

        if preflight:
            # Answer the preflight directly (never dispatched to a route).
            await Response(status_code=200, headers=cors)(scope, receive, send)
            return
        if not cors:
            # A disallowed cross-origin actual request: no CORS headers (the browser
            # blocks the response read). The request still runs — same as Starlette.
            await self.app(scope, receive, send)
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                mutable = MutableHeaders(raw=list(message.get("headers", [])))
                for key, value in cors.items():
                    mutable.append(key, value)
                message = {**message, "headers": mutable.raw}
            await send(message)

        await self.app(scope, receive, send_with_cors)
