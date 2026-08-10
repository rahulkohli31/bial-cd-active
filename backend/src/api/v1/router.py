"""Aggregate v1 router. Domain routers (auth, projects, admin, …) mount here as
they land; the foundation exposes only the public health endpoint.
"""

from fastapi import APIRouter

from src.api.v1.admin.router import router as admin_router
from src.api.v1.admin.router import users_router as admin_users_router
from src.api.v1.apps.router import router as apps_router
from src.api.v1.attachments.router import router as attachments_router
from src.api.v1.auth.router import router as auth_router
from src.api.v1.auth.sandbox_router import router as auth_sandbox_router
from src.api.v1.build_sessions.router import router as build_sessions_router
from src.api.v1.claude.router import router as claude_router
from src.api.v1.conversations.router import router as conversations_router
from src.api.v1.conversations.transition import router as transition_router
from src.api.v1.conversations.turns import router as turns_router
from src.api.v1.feedback.router import router as feedback_router
from src.api.v1.health.router import router as health_router
from src.api.v1.projects.router import router as projects_router
from src.api.v1.usage.router import router as usage_router
from src.schemas import AUTH_403_SUSPENDED, DetailBody, error_responses

# Cross-cutting error codes are documented ONCE here as v1-router-level defaults:
# the unhandled-exception 500 (`{"detail": "Internal server error"}`,
# `unhandled_exception_handler`) so every v1 route clears SonarQube S8415 without a
# per-route declaration, and the suspension 403 `current_user` raises on every
# authenticated route (deps.py, R11). FastAPI merges `{**router.responses,
# **route.responses}`, so a route with its own declaration — claude's
# `ErrorEnvelope`-shaped 500, admin's superadmin 403 — overrides these defaults.
v1_router = APIRouter(
    prefix="/v1",
    responses=error_responses(AUTH_403_SUSPENDED, (500, DetailBody, "Internal server error")),
)
v1_router.include_router(health_router)
v1_router.include_router(auth_router)
v1_router.include_router(auth_sandbox_router)
v1_router.include_router(usage_router)
v1_router.include_router(feedback_router)
v1_router.include_router(projects_router)
v1_router.include_router(conversations_router)
v1_router.include_router(turns_router)
v1_router.include_router(transition_router)
v1_router.include_router(attachments_router)
v1_router.include_router(claude_router)
v1_router.include_router(apps_router)
v1_router.include_router(build_sessions_router)
v1_router.include_router(admin_router)
v1_router.include_router(admin_users_router)
