"""Path-branching CORS (R23, review P2) — the sandbox data plane's CORS boundary.

FastAPI's single global `CORSMiddleware(allow_origins=[FRONTEND_URL],
allow_credentials=True)` short-circuits EVERY OPTIONS preflight and rejects the
`Origin: null` preflight before any route-level reflection can run — and two stacked
Starlette CORS middlewares cannot be path-scoped. So we replace it with ONE custom
ASGI layer that branches on the path:

* **Sandbox data route** (`/v1/apps/{id}/records`): reflect the request Origin
  **including the literal `null`** (the opaque-origin iframe), NO credentials —
  header auth (`X-App-Key` + Bearer) only, so reflecting `null` carries NO ambient
  authority.
* **Everything else** (SPA + `/v1/auth/*`): credentialed CORS for `FRONTEND_URL`
  only. `null` is NEVER reflected here, and credentials are NEVER sent on the data
  routes.

A same-origin request (no `Origin` header) passes straight through.
"""

from __future__ import annotations

import re

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The sandbox data route that gets null-reflecting, credential-free CORS. Matches
# /v1/apps/{appId}/records and any sub-path.
_DATA_ROUTE_RE = re.compile(r"^/v1/apps/[^/]+/records(/.*)?$")

_DATA_METHODS = "GET, POST, PATCH, DELETE, OPTIONS"
_DATA_HEADERS = "Content-Type, Authorization, X-App-Key"
_SPA_METHODS = "DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT"
_MAX_AGE = "600"


def is_data_route(path: str) -> bool:
    """True for the sandbox data route (records) that gets the null-reflecting,
    credential-free CORS."""
    return _DATA_ROUTE_RE.match(path) is not None


class ScopedCORSMiddleware:
    """One path-branching CORS layer (replaces the global `CORSMiddleware`)."""

    def __init__(self, app: ASGIApp, *, frontend_url: str) -> None:
        self.app = app
        self.frontend_url = frontend_url

    def _cors_headers(
        self, origin: str, req_headers: Headers, *, data_route: bool, preflight: bool
    ) -> dict[str, str]:
        if data_route:
            # Reflect ANY origin (incl. "null"); NEVER credentials.
            headers = {"access-control-allow-origin": origin, "vary": "Origin"}
            if preflight:
                headers["access-control-allow-methods"] = _DATA_METHODS
                headers["access-control-allow-headers"] = _DATA_HEADERS
                headers["access-control-max-age"] = _MAX_AGE
            return headers
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

        data_route = is_data_route(scope["path"])
        preflight = scope["method"] == "OPTIONS" and "access-control-request-method" in req_headers
        cors = self._cors_headers(origin, req_headers, data_route=data_route, preflight=preflight)

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
