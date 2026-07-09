"""Aggregate v1 router. Domain routers (auth, projects, admin, …) mount here as
they land; the foundation exposes only the public health endpoint.
"""

from fastapi import APIRouter

from src.api.v1.admin.router import router as admin_router
from src.api.v1.admin.router import users_router as admin_users_router
from src.api.v1.apps.files_router import router as files_router
from src.api.v1.apps.parse_router import router as parse_router
from src.api.v1.apps.records_router import router as records_router
from src.api.v1.apps.router import router as apps_router
from src.api.v1.attachments.router import router as attachments_router
from src.api.v1.auth.router import router as auth_router
from src.api.v1.claude.router import router as claude_router
from src.api.v1.conversations.router import router as conversations_router
from src.api.v1.feedback.router import router as feedback_router
from src.api.v1.health.router import router as health_router
from src.api.v1.projects.router import router as projects_router
from src.api.v1.usage.router import router as usage_router
from src.schemas import DetailBody, error_responses

# The unhandled-exception 500 (`{"detail": "Internal server error"}`,
# `unhandled_exception_handler`) is documented ONCE here as a v1-router-level default
# so every v1 route clears SonarQube S8415 for the universal 500 without a per-route
# declaration. FastAPI merges `{**router.responses, **route.responses}`, so a route
# that raises an explicit, differently-shaped 500 (claude renders the `ErrorEnvelope`)
# overrides this default. runner mounts at the app level, outside v1_router, so it
# carries its own 500 default (U8).
v1_router = APIRouter(
    prefix="/v1",
    responses=error_responses((500, DetailBody, "Internal server error")),
)
v1_router.include_router(health_router)
v1_router.include_router(auth_router)
v1_router.include_router(usage_router)
v1_router.include_router(feedback_router)
v1_router.include_router(projects_router)
v1_router.include_router(conversations_router)
v1_router.include_router(attachments_router)
v1_router.include_router(claude_router)
v1_router.include_router(apps_router)
v1_router.include_router(records_router)
v1_router.include_router(files_router)
v1_router.include_router(parse_router)
v1_router.include_router(admin_router)
v1_router.include_router(admin_users_router)
