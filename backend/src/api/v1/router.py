"""Aggregate v1 router. Domain routers (auth, projects, admin, …) mount here as
they land; the foundation exposes only the public health endpoint.
"""

from fastapi import APIRouter

from src.api.v1.auth.router import router as auth_router
from src.api.v1.health.router import router as health_router

v1_router = APIRouter(prefix="/v1")
v1_router.include_router(health_router)
v1_router.include_router(auth_router)
