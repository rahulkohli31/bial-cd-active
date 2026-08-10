"""FastAPI application factory + composition root.

Configures structlog at import, then `create_app()` wires the middleware
(security headers + credentialed CORS), the boundary exception handlers, and the
v1 router. The lifespan opens AND PROBES the Redis coordination pool when configured
(the sandbox lock/heartbeat/registry — C5) and, on shutdown, closes the Redis pool +
the sandbox client + the object-store client(s) so no aiohttp session / connection
pool leaks. No task queue runs (ADR-0011).
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Final

import structlog
from fastapi import FastAPI, Request, Response
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
    OPERATOR at deploy time, instead of to the first citizen developer whose build fails.

    Why a PING and not just `get_redis()`: `create_redis` opens no socket — `redis.asyncio`
    connects lazily on the first command — so merely CONSTRUCTING the client proves
    nothing about reachability. Only a command does.

    Why the singleton and not a throwaway probe client: a dedicated client would validate
    a connection no real caller ever uses while leaving the actual pool unproven, and
    `aclose_redis()` tracks only the singleton, so a second client would add teardown
    surface nothing closes. The probe therefore inherits the configured retry policy by
    design — which is precisely why it needs `REDIS_PROBE_CEILING_SECONDS` around it.

    Boot is NOT blocked. This warns loudly and returns; it must never raise out of the
    lifespan, or a Redis blip becomes a container restart loop and the API stops serving
    the many routes that need no Redis at all. The broad catch is the sanctioned kind
    (same shape as the health probe): it converts a failure into an operator signal, it
    does not swallow it. `asyncio.CancelledError` is a `BaseException` and still
    propagates, so a shutdown during boot is not eaten.
    """
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


# How often the background reap sweeps abandoned sandboxes, in seconds. Five minutes is well
# under the 30-minute stay of execution a live preview holds, so a container the citizen is still
# reading is never at risk; it only decides how quickly an ABANDONED one is noticed after its
# lease lapses.
SWEEP_INTERVAL_SECONDS: Final = 300
SWEEP_REAPED_EVENT: Final = "sandbox_background_sweep_reaped"


async def _reap_abandoned_sandboxes() -> None:
    """Reconcile abandoned sandboxes on a timer so a container cannot outlive its lease by an
    hour and a half.

    THIS REVISES A STATED DECISION, so here is the argument. `reaper.py`'s docstring says there
    is no in-process background sweeper by design, and its reasoning still stands exactly where
    it was aimed: the live-session shield reads an IN-PROCESS set, so a SECOND replica running
    this loop would be blind to the first replica's builds and could reap a sandbox somebody is
    actively building in. This does not remove that hazard — it inherits the single-replica
    constraint the deploy checklist already carries (sandboxes run `min_replicas=1`), which is
    the same constraint `internal/reap` has always run under. Scaling past one replica needs a
    shared liveness view before this loop is safe, and that is a deploy-time gate, not something
    a process can assert about its siblings.

    What changed is the measured cost of NOT having it: a sandbox was observed still Running ~90
    minutes past its lease, collected only when the same user happened to start another build. A
    citizen who does not come back never triggers their own cleanup, and ACA bills the whole
    time. An operator endpoint that nobody calls on a timer is not a reaper.

    Every failure is swallowed to a log line ON PURPOSE: a Redis blip or an ARM hiccup must not
    kill the loop and silently return the deployment to no sweeping at all. `CancelledError` is a
    `BaseException` and still propagates, so shutdown is not eaten."""
    from src.services.build_sessions import get_session_manager
    from src.services.build_sessions.reaper import sweep_all
    from src.services.redis import get_redis
    from src.services.sandbox import SandboxNotConfiguredError, get_sandbox

    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            manager = get_session_manager()
            manager.evict_ended_sessions()
            result = await sweep_all(
                get_redis(), get_sandbox(), live_users=manager.live_user_ids()
            )
            if result.reaped:
                _log.info(SWEEP_REAPED_EVENT, reaped=result.reaped)
            # A sweep where every user threw returns reaped=0 and used to log NOTHING at all,
            # which reads exactly like a sweep with nothing to do — while containers accumulate
            # and bill. Reaped-nothing-but-failed-some is the systemic shape worth waking to.
            if result.failed:
                _log.warning(
                    "sandbox_background_sweep_partial",
                    reaped=result.reaped,
                    failed=result.failed,
                )
        except SandboxNotConfiguredError:
            return  # sandbox-off deployment (dev/test): nothing to sweep, ever
        except Exception:
            _log.warning("sandbox_background_sweep_failed", exc_info=True)


# How often interrupted deploys are re-checked against ARM. Deliberately the same cadence
# as the sandbox sweep: both are cheap reconciliation against Azure, and one timer's worth of
# latency is irrelevant next to how long a deploy takes.
DEPLOY_RECONCILE_INTERVAL_SECONDS: Final = SWEEP_INTERVAL_SECONDS
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


async def _reconcile_deploys_periodically() -> None:
    """The same reconciliation on a timer. `CancelledError` is a `BaseException` and still
    propagates, so shutdown is not eaten."""
    while True:
        await asyncio.sleep(DEPLOY_RECONCILE_INTERVAL_SECONDS)
        await _reconcile_interrupted_deploys()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup: open AND probe the app-global Redis coordination pool when configured
    # (the sandbox lock/heartbeat/registry ride it — C5). None-safe: a dev/test boot
    # with no REDIS__* env opens nothing and probes nothing (D2). The per-user sandbox
    # client (C2) is provisioned on demand by SESSION-API (Wave 1), not opened here.
    if settings.redis is not None:
        await _probe_redis()
    # …and start the background reap on the same condition: it is pure Redis + ACA
    # reconciliation, so a Redis-off boot has nothing for it to walk.
    sweeper = asyncio.create_task(_reap_abandoned_sandboxes()) if settings.redis else None
    # Settle any deploy the LAST process died in the middle of, before serving. A pipeline
    # runs for minutes and every platform deploy kills it, so a deploy straddling a restart
    # is the expected case during a rollout — not an edge case. Startup alone is not enough
    # (a crash-loop can run this before ARM has settled), so the periodic sweeper repeats it.
    await _reconcile_interrupted_deploys()
    deploy_sweeper = (
        asyncio.create_task(_reconcile_deploys_periodically()) if settings.deploy else None
    )
    yield
    if deploy_sweeper is not None:
        deploy_sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await deploy_sweeper
    # Stop the sweeper FIRST: it reaches for Redis, the sandbox client and ACA, all of which the
    # shutdown below is about to close underneath it.
    if sweeper is not None:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
    # Shutdown: close the coordination pool, the sandbox client, the object-store
    # client(s) + Azure credential, and the app-database maintenance engine so no aiohttp
    # session / connection pool leaks. Each is a no-op when its resource was never opened.
    from src.services.appdb import aclose_maintenance_engine
    from src.services.deploy.aca_publish import aclose_published_apps
    from src.services.deploy.images import aclose_image_builder
    from src.services.redis import aclose_redis
    from src.services.sandbox import aclose_sandbox
    from src.services.storage import aclose_storage

    await aclose_redis()
    await aclose_sandbox()
    await aclose_storage()
    await aclose_maintenance_engine()
    # The publish path holds its OWN managed-identity credential and mgmt client, plus an
    # httpx pool for the registry. Without these two it leaks a second token cache and a
    # second connection pool alongside the sandbox's.
    await aclose_published_apps()
    await aclose_image_builder()


def create_app() -> FastAPI:
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
    # single-replica store assumption at startup (R31). The limiters are deliberately
    # in-process counters, NOT Redis-backed — a Redis-backed limiter was rejected in
    # scope, and ADR-0011 defers the TASK QUEUE, not Redis (which this app very much
    # uses, for the C5 sandbox lock/heartbeat/registry).
    # SINGLE-REPLICA CONSTRAINT (binding — see the deploy checklist): because the counters
    # are per-process, N replicas give N× the intended ceiling. This is one of three
    # sites that assume a single replica (with the reaper's live-session shield and the
    # manager's double-session guard); scaling out needs a shared store for all three.
    install_rate_limiting(app)

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
        # `frame-ancestors <portal-origin>` (C8), not from this control plane.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Default to no-store, but let a route keep its own caching policy (e.g. the
        # attachment download's `private, max-age=3600` for image re-rendering): setdefault
        # only writes when the route left it unset, so the strong default still covers all else.
        response.headers.setdefault("Cache-Control", "no-store")
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    # ONE path-branching CORS layer (P2), NOT Starlette's global CORSMiddleware:
    # the sandbox data route (/v1/apps/{id}/records) reflects the Origin — including
    # the opaque-origin iframe's `null` — with NO credentials, while the SPA/auth
    # routes get credentialed CORS for FRONTEND_URL only. A single global
    # CORSMiddleware would short-circuit the `null` preflight before any route-level
    # reflection could run, and two stacked instances can't be path-scoped.
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
        # Documents the two bare HTTPException(404) raises below for SonarQube S8415
        # (U15). The route is out-of-schema, and the body stays FastAPI's default
        # `{"detail":"Not Found"}` — no envelope migration, HTTPException is idiomatic here.
        responses=error_responses((404, DetailBody, "Not Found")),
    )
    async def spa_history_fallback(full_path: str) -> FileResponse:
        # Never shadow the API: its routes match first, but a genuinely unmatched
        # /v1|/api path must 404 as JSON, not return HTML.
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
