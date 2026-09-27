"""Dependency seam for the classification review routes.

The review service is ALWAYS constructible — an unconfigured Foundry surfaces at RUN time
inside the detached task (the review-failed bucket), never at
dependency-solve time — so unlike the deploy service this provider has no `| None` flavour
and no 503 to protect. It exists as a `Depends` seam so tests override THIS key.

Storage — the one genuinely unconfigurable dependency here — reuses the EXISTING shared
provider (`src.api.deps.storage_or_none_dependency`, as `OptionalStorage`) rather than
re-declaring it: splitting a provider forks the `dependency_overrides` key silently, so a test
can bind a fake to one key while the route resolves the other, with nothing failing.
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
