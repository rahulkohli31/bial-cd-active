"""Dependency seam for the classification review routes.

The review service is ALWAYS constructible — an unconfigured Foundry surfaces at RUN
time inside the detached task (as the review-failed bucket, with the Tier A floor still
applied), never at dependency-solve time — so unlike the deploy service this provider
has no `| None` flavour and no 503 to protect. It exists as a `Depends` seam so tests
override THIS key with a scripted service.

Storage — the one genuinely unconfigurable dependency these routes take — is
deliberately the EXISTING shared provider (`src.api.deps.storage_or_none_dependency`,
consumed as `OptionalStorage`), reused rather than re-declared here: splitting a
provider forks the `dependency_overrides` key silently, so a test would bind a fake to
one key while the route resolved the other, with nothing failing (the documented burn).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from src.services.classification.service import (
    ClassificationReviewService,
    get_classification_review_service,
)


def review_service_dependency() -> ClassificationReviewService:
    return get_classification_review_service()


ReviewService = Annotated[ClassificationReviewService, Depends(review_service_dependency)]
