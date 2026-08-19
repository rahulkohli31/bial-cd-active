"""Dependency seams for the deploy routes.

`OptionalDeployService` yields `None` — never raises — when `DEPLOY__*` is unconfigured.

That shape is load-bearing rather than defensive. FastAPI resolves every `Depends` BEFORE
the route body's first statement, so a provider that raised would escape the body's own
`try` and surface as an undocumented 500 carrying the wrong error envelope. The routes here
document a 503, so the provider has to hand back `None` and let the body decide. This is the
same accommodation `OptionalStorage` and `OptionalSandbox` already make, for the same reason.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from src.services.deploy.aca_publish import (
    DeployNotConfiguredError,
    PublishedAppRemover,
    get_published_apps,
)
from src.services.deploy.service import DeployService, get_deploy_service


def deploy_service_or_none() -> DeployService | None:
    try:
        return get_deploy_service()
    except DeployNotConfiguredError:
        return None


OptionalDeployService = Annotated[DeployService | None, Depends(deploy_service_or_none)]


def published_app_remover_or_none() -> PublishedAppRemover | None:
    """Same shape as `deploy_service_or_none`, and the same reason: `unpublish` (#113)
    documents a 503 when publishing is unconfigured, so the provider must hand back `None`
    rather than let `DeployNotConfiguredError` escape as an undocumented 500. Also the seam
    a test overrides with a fake `PublishedAppRemover` to assert on the delete without
    reaching Azure — mirrors `test_deploy_routes.py`'s `deploy_service_or_none` override."""
    try:
        return get_published_apps()
    except DeployNotConfiguredError:
        return None


OptionalPublishedAppRemover = Annotated[
    PublishedAppRemover | None, Depends(published_app_remover_or_none)
]
