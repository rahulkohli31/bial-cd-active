"""FastAPI application factory + composition root.

Configures structlog at import, then `create_app()` wires the middleware
(security headers + credentialed CORS), the boundary exception handlers, and the
v1 router. The lifespan opens AND PROBES the Redis coordination pool when configured
(the sandbox lock/heartbeat/registry), runs the embedding client's Foundry-only guard once
(#191 slice 3, R23 — a mis-wired resource fails the deploy rather than a citizen's first
save), and, on shutdown, closes the Redis pool + the sandbox client + the object-store
client(s) so no aiohttp session / connection pool leaks.

Nothing recurring runs here: a sweep here would run in every API replica beside the copy
the worker already schedules; the one boot-path item, `_reconcile_interrupted_deploys`,
is a one-shot, not a loop.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Final

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from src.config import settings
from src.schemas import DetailBody, error_responses
from src.services.cors.middleware import ScopedCORSMiddleware
from src.services.redis import get_redis

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


_log = structlog.get_logger()

# Outer ceiling on the startup Redis probe, in seconds. It exists because the retry
# policy's budget is a SUM, not a deadline: with `Retry(..., retries=3)` and both
# socket timeouts at 2s, a genuinely-hung Redis costs (3+1) x (2+2) + ~0.7s of
# backoff ≈ 16.7s before the client gives up (≈ 8.7s against a merely-dead host).
# Boot must not stall that long for a dependency the API can serve without, so the
# ceiling is deliberately set BELOW the internal worst case — a ceiling at or above
# it would be decorative. A refusing host still completes all four attempts well
# inside this (each attempt fails in ~0.006s; the ~0.7s backoff sum dominates), so
# the ceiling truncates only the black-holed case, which is what it is for.
REDIS_PROBE_CEILING_SECONDS: Final = 3.0

# Distinguishable structlog event names: an operator (or an alert rule) greps for
# exactly these, so they are constants rather than inline literals.
REDIS_PROBE_OK_EVENT: Final = "redis_startup_probe_ok"
REDIS_PROBE_FAILED_EVENT: Final = "redis_startup_probe_failed"


async def _probe_redis() -> None:
    """PING the coordination pool at startup so a misconfigured Redis is visible to an
    OPERATOR at deploy time, not to the first citizen developer whose build fails —
    construction alone proves nothing (`redis.asyncio` connects lazily), only a command does.

    Uses the singleton, not a throwaway client, so it validates the pool real callers
    actually use. Boot is NOT blocked: warns and returns, never raises — a blip must not
    restart-loop the container — and `CancelledError` still propagates so a shutdown
    isn't eaten."""
    try:
        await asyncio.wait_for(get_redis().ping(), timeout=REDIS_PROBE_CEILING_SECONDS)
    except Exception as exc:
        _log.warning(
            REDIS_PROBE_FAILED_EVENT,
            error_type=type(exc).__name__,
            error=str(exc),
            ceiling_seconds=REDIS_PROBE_CEILING_SECONDS,
            hint=(
                "the API will serve, but build sessions will fail until Redis is "
                "reachable; check REDIS__URL, the private endpoint, and TLS (port 6380)"
            ),
        )
    else:
        _log.info(REDIS_PROBE_OK_EVENT)


DEPLOY_RECONCILED_EVENT: Final = "deploy_startup_reconcile"


async def _reconcile_interrupted_deploys() -> None:
    """Settle deployment rows whose pipeline died with a previous process.

    Every failure is swallowed to a log line ON PURPOSE: this runs on the boot path, and a
    transient ARM blip must never turn into a container that refuses to start. Publishing
    being unconfigured is the ordinary dev/test posture and returns silently."""
    from src.services.deploy.aca_publish import DeployNotConfiguredError, get_published_apps
    from src.services.deploy.reconcile import reconcile_stalled_deployments

    if settings.deploy is None:
        return
    try:
        from src.db.base import async_session_factory

        resolved = await reconcile_stalled_deployments(async_session_factory, get_published_apps())
        if resolved:
            _log.info(DEPLOY_RECONCILED_EVENT, resolved=resolved)
    except DeployNotConfiguredError:
        return
    except Exception:
        _log.warning("deploy_startup_reconcile_failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup: open AND probe the app-global Redis coordination pool when configured
    # (the sandbox lock/heartbeat/registry ride it). None-safe: a dev/test boot
    # with no REDIS__* env opens nothing and probes nothing. The per-user sandbox
    # client is provisioned on demand by SESSION-API, not opened here.
    if settings.redis is not None:
        await _probe_redis()
    # The embedding client's Foundry-only guard (#191 slice 3, R23), run once here rather
    # than left to the first request that needs it — a mis-wired FOUNDRY__RESOURCE /
    # FOUNDRY__EMBEDDING_DEPLOYMENT fails the deploy instead of degrading silently into
    # EMBEDDING_WRITE_FAILED_EVENT on the first citizen's save. A no-op when Foundry or the
    # embedding deployment isn't configured (dev/test, or semantic search deliberately off).
    from src.services.embeddings import assert_embedding_guard_at_startup

    assert_embedding_guard_at_startup(settings.foundry)
    # Settle any deploy the LAST process died in the middle of, before serving. A pipeline
    # runs for minutes and every platform deploy kills it, so a deploy straddling a restart
    # is the expected case during a rollout — not an edge case. Startup alone is not enough
    # (a crash-loop can run this before ARM has settled), so the scheduled pass repeats it.
    await _reconcile_interrupted_deploys()
    yield
    # Shutdown: close every client so no aiohttp session / connection pool leaks. Each is
    # a no-op when its resource was never opened.
    from src.services.appdb import aclose_maintenance_engine
    from src.services.deploy.aca_publish import aclose_published_apps
    from src.services.deploy.images import aclose_image_builder
    from src.services.lake import aclose_lake
    from src.services.redis import aclose_redis
    from src.services.sandbox import aclose_sandbox
    from src.services.storage import aclose_storage

    # EVERY CLOSER RUNS, WHATEVER THE ONE BEFORE IT DID. Written as a loop rather than a column
    # of `await`s because a column has a property nobody wants: the first one that raises
    # abandons every closer after it, so the pools that leak are decided by list position rather
    # than by anything real — and the ones at the bottom are the newest, least-exercised clients.
    # A shutdown is exactly where "recover or re-raise" gives way to "close the rest anyway":
    # the process is going away, and the failure has nowhere to be handled.
    #
    # `aclose_published_apps` and `aclose_image_builder` hold the publish path's OWN
    # managed-identity credential, mgmt client and httpx registry pool — a second token cache and
    # connection pool alongside the sandbox's. `aclose_lake` holds a THIRD managed-identity
    # credential, a different identity from the other two and named by client id rather than
    # resolved from the ambient environment, plus its own blob client. Each is a no-op when its
    # resource was never opened.
    for close in (
        aclose_redis,
        aclose_sandbox,
        aclose_storage,
        aclose_maintenance_engine,
        aclose_published_apps,
        aclose_image_builder,
        aclose_lake,
    ):
        try:
            await close()
        except Exception:
            _log.exception("shutdown_closer_failed", closer=close.__name__)


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
"""The methods a cross-origin caller could use to change something. `GET`/`HEAD` are
excluded deliberately: they are not supposed to mutate, and refusing them would break
ordinary cross-origin reads the CORS layer already governs."""


def create_app() -> FastAPI:
    from src.api.v1.router import v1_router
    from src.core.errors import register_exception_handlers
    from src.services.ratelimit import install_rate_limiting

    # Hide the interactive docs + the OpenAPI schema in production: the enriched
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
    # Register the 429 handler for the in-process rate limiters and log the single-replica
    # store assumption at startup. The limiters are deliberately in-process counters, NOT
    # Redis-backed — a Redis-backed limiter was rejected in scope.
    # SINGLE-REPLICA CONSTRAINT (binding): because the counters are per-process, N replicas
    # give N× the intended ceiling. This is one of three sites that assume a single replica
    # (with the reaper's live-session shield and the manager's double-session guard);
    # scaling out needs a shared store for all three.
    install_rate_limiting(app)

    # CROSS-ORIGIN WRITE GUARD — the cost of moving generated apps onto a BIAL hostname.
    #
    # Generated apps used to live on `*.azurecontainerapps.io`, a different registrable domain
    # from the portal's, so they were CROSS-SITE to it and `SameSite=Lax` withheld the session
    # cookie from anything they sent here. Serving them from `citizenapps.bialairport.com` makes
    # them SAME-SITE with `blrcitizen.bialairport.com`, and Lax stops being a barrier: a POST
    # from app code now carries the viewer's session. The app's code is written by a model from
    # a citizen's prompt — it is untrusted by construction.
    #
    # CSRF protection here is opt-in per route (`RequireCsrf`), and the admin, attachment and
    # feedback routers do not declare it. Rather than change that design late, this closes the
    # newly-opened door directly: a mutating request that announces an Origin which is not the
    # portal's is refused before it reaches a route.
    #
    # WHY AN ABSENT ORIGIN IS ALLOWED. Browsers attach `Origin` to every mutating request,
    # cross-site form posts included, so absence means a non-browser caller — curl, a health
    # check, a server-to-server call — which is not the threat and which no cookie authenticates
    # anyway. Blocking it would break every scripted client for no gain.
    #
    # Middleware, not a dependency: it must cover routes that never declared one, which is the
    # entire point. Declared BEFORE `security_headers` so that one stays outermost and the 403
    # still carries the standard headers.
    @app.middleware("http")
    async def refuse_cross_origin_writes(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        origin = request.headers.get("origin")
        if request.method in _MUTATING_METHODS and origin and origin != settings.FRONTEND_URL:
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "cross_origin_write_refused",
                        "detail": "This request came from another origin and was refused.",
                    }
                },
            )
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Applied to every response (including framework-generated 4xx/5xx),
        # which a route dependency cannot reach — so this must be middleware.
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Nothing is framed same-origin anymore (the old runner shell that needed
        # SAMEORIGIN for its /apps/ frame was retired) — DENY everywhere. The Phase-2
        # cross-origin preview is framed from the sandbox's own Caddy via
        # `frame-ancestors <portal-origin>`, not from this control plane.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Default to no-store, but let a route keep its own caching policy (e.g. the
        # attachment download's `private, max-age=3600` for image re-rendering): setdefault
        # only writes when the route left it unset, so the strong default still covers all else.
        response.headers.setdefault("Cache-Control", "no-store")
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    # ONE path-branching CORS layer, NOT Starlette's global CORSMiddleware:
    # the sandbox data route (/v1/apps/{id}/records) reflects the Origin — including
    # the opaque-origin iframe's `null` — with NO credentials, while the SPA/auth
    # routes get credentialed CORS for FRONTEND_URL only. A single global
    # CORSMiddleware would short-circuit the `null` preflight before any route-level
    # reflection could run, and two stacked instances can't be path-scoped.
    app.add_middleware(ScopedCORSMiddleware, frontend_url=settings.FRONTEND_URL)

    # Holds the transient OAuth state (PKCE verifier, nonce, state) BETWEEN
    # /auth/login and the callback. Named "oauth_transient" (not the default
    # "session") so it never collides with the app session-JWT cookie once __Host-
    # drops over http in dev. same_site="lax" (never "strict") so the
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
        # In production FastAPI is reachable ONLY through the edge/gateway,
        # so the forwarded scheme/host are trusted — this makes any request.url_for
        # render https + the external host. (The callback redirect_uri itself comes
        # from AUTH__REDIRECT_URI, not url_for, because the edge strips /api.)
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

    app.include_router(v1_router)
    _mount_spa(app)
    return app


# Path segments owned by the API — the SPA history fallback must refuse them so an
# unmatched /v1/... stays a JSON 404, never the SPA's index.html. (The old-JSX runner
# that mounted at bare /apps was retired — deployed apps are served from the sandbox's
# own Caddy, not this control plane, so /apps is no longer reserved here.)
_RESERVED_ROOTS = frozenset({"v1", "api"})


def _mount_spa(app: FastAPI) -> None:
    """Serve the built React/Vite SPA + a history fallback so the no-Node image can
    answer `/` and deep-linked routes. Mounted LAST — every real API route is
    registered first, so `/v1` always wins.

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
        # Documents the two bare HTTPException(404) raises below for SonarQube S8415.
        # The route is out-of-schema, and the body stays FastAPI's default
        # `{"detail":"Not Found"}` — no envelope migration, HTTPException is idiomatic here.
        responses=error_responses((404, DetailBody, "Not Found")),
    )
    async def spa_history_fallback(full_path: str) -> FileResponse:
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
