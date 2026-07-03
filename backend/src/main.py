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
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from src.config import settings

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
    from src.api.v1.router import v1_router
    from src.core.errors import register_exception_handlers

    app = FastAPI(title="BIAL Backend", version="0.1.0", lifespan=lifespan)

    register_exception_handlers(app)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Applied to every response (including framework-generated 4xx/5xx),
        # which a route dependency cannot reach — so this must be middleware.
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.FRONTEND_URL],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
    return app


app = create_app()
