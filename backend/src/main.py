"""FastAPI application factory + composition root.

Configures structlog at import, then `create_app()` wires the middleware
(security headers + credentialed CORS), the boundary exception handlers, and the
v1 router. The lifespan's only teardown is closing the object-store client(s) on
shutdown (an unclosed Azure credential / aiohttp session leaks otherwise); no task
queue / Redis runs yet (ADR-0011).
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from starlette.middleware.sessions import SessionMiddleware

from src.config import settings
from src.schemas import DetailBody, error_responses
from src.services.appkey.cors import ScopedCORSMiddleware

# Configure structlog process-wide at import: a human ConsoleRenderer in dev,
# one-line JSON in production (for log aggregation).
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
        if settings.is_production
        else structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # No background services to start this phase.
    yield
    # Shutdown: close the cached object-store client(s) + Azure credential so
    # their aiohttp sessions don't leak. A no-op when storage was never used.
    from src.services.storage import aclose_storage

    await aclose_storage()


def create_app() -> FastAPI:
    from src.api.v1.apps.runner import router as runner_router
    from src.api.v1.router import v1_router
    from src.core.errors import register_exception_handlers
    from src.services.ratelimit import install_rate_limiting

    # Hide the interactive docs + the OpenAPI schema in production (U17): the enriched
    # spec (full error taxonomy, named quota/rate-limit codes, admin route enumeration)
    # would otherwise be served UNAUTHENTICATED. `openapi_url=None` also makes /docs and
    # /redoc 404 since they depend on the schema URL. Dev/staging keep Swagger + ReDoc.
    app = FastAPI(
        title="BIAL Backend",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    register_exception_handlers(app)
    # Register the 429 handler for the in-process rate limiters and log the
    # single-replica store assumption at startup (R31; Redis deferred, ADR-0011).
    install_rate_limiting(app)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Applied to every response (including framework-generated 4xx/5xx),
        # which a route dependency cannot reach — so this must be middleware.
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # The runner shell frames its OWN sandboxed frame same-origin, so /apps and
        # /preview get SAMEORIGIN (their per-route CSP adds `frame-ancestors 'self'`);
        # everything else is DENY — never framed. Without this, a global DENY would
        # block the shell from loading its frame.
        path = request.url.path
        response.headers["X-Frame-Options"] = (
            "SAMEORIGIN" if path == "/preview" or path.startswith("/apps/") else "DENY"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        # Default to no-store, but let a route keep its own caching policy (e.g. the
        # attachment download's `private, max-age=3600` for image re-rendering): setdefault
        # only writes when the route left it unset, so the strong default still covers all else.
        response.headers.setdefault("Cache-Control", "no-store")
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    # ONE path-branching CORS layer (P2), NOT Starlette's global CORSMiddleware:
    # the sandbox data routes (/v1/apps/{id}/records|files|parse) reflect the
    # Origin — including the opaque-origin iframe's `null` — with NO credentials,
    # while the SPA/auth routes get credentialed CORS for FRONTEND_URL only. A single
    # global CORSMiddleware would short-circuit the `null` preflight before any
    # route-level reflection could run, and two stacked instances can't be path-scoped.
    app.add_middleware(ScopedCORSMiddleware, frontend_url=settings.FRONTEND_URL)

    # Holds the transient OAuth state (PKCE verifier, nonce, state) BETWEEN
    # /auth/login and the callback. Named "oauth_transient" (not the default
    # "session") so it never collides with the app session-JWT cookie once __Host-
    # drops over http in dev (KD-4). same_site="lax" (never "strict") so the
    # top-level redirect back from login.microsoftonline.com still carries it.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.auth.session_secret.get_secret_value(),
        session_cookie="oauth_transient",
        same_site="lax",
        https_only=settings.is_production,
        max_age=settings.auth.session_cookie_max_age,
    )

    if settings.is_production:
        # In production FastAPI is reachable ONLY through the edge/gateway (KD-8),
        # so the forwarded scheme/host are trusted — this makes any request.url_for
        # render https + the external host. (The callback redirect_uri itself comes
        # from AUTH__REDIRECT_URI, not url_for, because the edge strips /api — KD-8.)
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

    app.include_router(v1_router)
    # Runner + preview HTML serving is mounted OUTSIDE the /v1 API prefix (at /apps
    # and /preview) to match the hosted-app URLs deployed apps already use. Ordered
    # before the SPA static/catch-all below so those paths are never shadowed.
    app.include_router(runner_router)
    _mount_spa(app)
    return app


# Path segments owned by the API/runner — the SPA history fallback must refuse them
# so an unmatched /v1/... stays a JSON 404, never the SPA's index.html.
_RESERVED_ROOTS = frozenset({"v1", "apps", "preview", "api"})


def _mount_spa(app: FastAPI) -> None:
    """Serve the built React/Vite SPA + a history fallback so the no-Node image can
    answer `/` and deep-linked routes (U10). Mounted LAST — every real API/runner
    route is registered first, so `/v1`, `/apps`, `/preview` always win.

    An UNSET `spa_dist_dir` is a no-op with a defined meaning (two-process local dev: Vite
    serves the SPA). A CONFIGURED one whose built shell is missing refuses to boot: skipping
    the mount made a broken image look healthy while `/` and every deep link 404'd — and a
    packaging slip is exactly the class of bug the cross-platform build path keeps producing."""
    dist = settings.spa_dist_dir
    if dist is None:
        return

    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    from starlette.staticfiles import StaticFiles

    index = dist / "index.html"
    if not dist.is_dir() or not index.is_file():
        raise RuntimeError(
            f"SPA_DIST_DIR={dist} does not contain a built index.html. Build the SPA into that "
            "directory, or unset SPA_DIST_DIR to run the API without serving the SPA."
        )
    dist_root = dist.resolve()
    assets = dist / "assets"
    if assets.is_dir():
        # Hashed, immutable bundles — mounted explicitly so the catch-all never sees them.
        app.mount("/assets", StaticFiles(directory=assets), name="spa-assets")

    @app.get(
        "/{full_path:path}",
        include_in_schema=False,
        # Documents the two bare HTTPException(404) raises below for SonarQube S8415
        # (U15). The route is out-of-schema, and the body stays FastAPI's default
        # `{"detail":"Not Found"}` — no envelope migration, HTTPException is idiomatic here.
        responses=error_responses((404, DetailBody, "Not Found")),
    )
    async def spa_history_fallback(full_path: str) -> FileResponse:
        # Never shadow the API/runner: their routes match first, but a genuinely
        # unmatched /v1|/apps|/preview|/api path must 404 as JSON, not return HTML.
        if full_path.split("/", 1)[0] in _RESERVED_ROOTS:
            raise HTTPException(status_code=404)
        # A real static file at the web root (favicon, logo) wins; otherwise return
        # index.html so the SPA router resolves the deep link client-side. Confine the
        # candidate to the dist root: an absolute (`//etc/passwd`) or `..` path must
        # never escape it (arbitrary-file-read guard) — 404 as JSON instead.
        candidate = (dist / full_path).resolve()
        try:
            candidate.relative_to(dist_root)
        except ValueError:
            raise HTTPException(status_code=404) from None
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
