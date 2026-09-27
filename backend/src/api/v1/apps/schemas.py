"""App-lifecycle request/response schemas — withdraw / status.

camelCase over the wire (via the shared `CamelModel`), matching the SPA/TS
convention the `/api/apps/*` clients already consume.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from src.db.models.app_registry import AppStatus
from src.schemas import CamelModel

# Withdraw takes NO request body: the target is fully named by the path (the app's
# one pending submission), and there is nothing to parameterize about removal.


class WithdrawResponse(CamelModel):
    # Always `draft` on success — returned explicitly so the SPA renders the
    # post-withdraw state from the response instead of guessing it.
    app_id: uuid.UUID
    status: AppStatus


class AppStatusResponse(CamelModel):
    # A resolved app ALWAYS has a status and an appKey — an absent/cross-user one is a 404, not
    # a null-signalling 200 (the `status: null` "not provisioned" shim is gone). The submission
    # fields are legitimately absent until the first submit; `rejectionNote` is set solely on
    # the rejected transition (and cleared by the next submit).
    app_id: uuid.UUID
    status: AppStatus
    app_key: str
    login_required: bool
    rejection_note: str | None
    submission_id: uuid.UUID | None
    commit_sha: str | None
    submitted_at: datetime | None
