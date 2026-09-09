"""Aggregate v1 router. Domain routers (auth, projects, admin, …) mount here as
they land; the foundation exposes only the public health endpoint.

WHY THIS EXISTS

A route handler's docstring publishes verbatim as its OpenAPI `description` — write it for
API consumers, not just developers reading the source.

EVERY QUERY BEHIND THESE ROUTES IS SCOPED BY THE OWNING `user_id`. The platform is single-tenant
— there is no `org_id`, so that predicate IS the isolation boundary and a dropped one is a
cross-user leak, not a style nit; it belongs in the WHERE clause, never in a check made after the
row is loaded. A resource owned by someone else and a resource that does not exist get the SAME
non-leaking 404, with the same message, and never a 403: a 403 confirms the row exists, which is
precisely the probe the 404 refuses to answer. Reaching across owners is an explicit, role-gated,
audited admin action, and the one read that drops the predicate on purpose — the published-app
catalog — argues for itself in its own module header."""

from fastapi import APIRouter

from src.api.v1.admin.connectors import router as admin_connectors_router
from src.api.v1.admin.router import router as admin_router
from src.api.v1.admin.router import users_router as admin_users_router
from src.api.v1.apps.router import router as apps_router
from src.api.v1.attachments.router import router as attachments_router
from src.api.v1.auth.router import router as auth_router
from src.api.v1.build_sessions.router import router as build_sessions_router
from src.api.v1.classification.router import router as classification_router
from src.api.v1.connectors.router import project_router as project_connectors_router
from src.api.v1.connectors.router import router as connectors_router
from src.api.v1.conversations.router import router as conversations_router
from src.api.v1.conversations.transition import router as transition_router
from src.api.v1.conversations.turns import router as turns_router
from src.api.v1.deploy.router import admin_router as deploy_admin_router
from src.api.v1.deploy.router import router as deploy_router
from src.api.v1.feedback.router import router as feedback_router
from src.api.v1.health.router import router as health_router
from src.api.v1.marketplace.router import router as marketplace_router
from src.api.v1.observations.router import router as observations_router
from src.api.v1.projects.router import router as projects_router
from src.api.v1.usage.router import router as usage_router
from src.schemas import AUTH_403_SUSPENDED, DetailBody, error_responses

# Cross-cutting error codes are documented ONCE here as v1-router-level defaults:
# the unhandled-exception 500 (`{"detail": "Internal server error"}`,
# `unhandled_exception_handler`) so every v1 route clears SonarQube S8415 without a
# per-route declaration, and the suspension 403 `current_user` raises on every
# authenticated route (deps.py). FastAPI merges `{**router.responses,
# **route.responses}`, so a route with its own declaration — admin's superadmin 403 —
# overrides these defaults. This is DOCUMENTATION only: the handlers themselves
# (`core/errors.py`) are registered app-wide from `main.py`, not here.
v1_router = APIRouter(
    prefix="/v1",
    responses=error_responses(AUTH_403_SUSPENDED, (500, DetailBody, "Internal server error")),
)
v1_router.include_router(health_router)
v1_router.include_router(auth_router)
v1_router.include_router(usage_router)
v1_router.include_router(feedback_router)
v1_router.include_router(observations_router)
v1_router.include_router(projects_router)
v1_router.include_router(marketplace_router)
v1_router.include_router(deploy_router)
v1_router.include_router(classification_router)
v1_router.include_router(conversations_router)
v1_router.include_router(turns_router)
v1_router.include_router(transition_router)
v1_router.include_router(attachments_router)
v1_router.include_router(connectors_router)
v1_router.include_router(project_connectors_router)
v1_router.include_router(apps_router)
v1_router.include_router(build_sessions_router)
v1_router.include_router(admin_router)
v1_router.include_router(admin_users_router)
v1_router.include_router(admin_connectors_router)
v1_router.include_router(deploy_admin_router)
