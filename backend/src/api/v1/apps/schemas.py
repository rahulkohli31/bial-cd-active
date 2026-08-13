"""App-lifecycle request/response schemas — submit / status.

camelCase over the wire (via the shared `CamelModel`), matching the SPA/TS
convention the `/api/apps/*` clients already consume.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from src.db.models.app_registry import AppStatus
from src.schemas import CamelModel

# Submit takes NO request body (APPROVAL R19): the artifact is the app's server-side
# git-bundle snapshot, copied to an immutable submission blob — nothing is
# client-supplied, which is the whole point of the open-sandbox pivot.


class SubmitResponse(CamelModel):
    app_id: uuid.UUID
    status: AppStatus
    # The immutable submission the copy minted (R1/R4): the id the admin's approve
    # must echo back (D5) and the bundle's HEAD commit SHA for provenance.
    submission_id: uuid.UUID
    commit_sha: str
    submitted_at: datetime


class AppStatusResponse(CamelModel):
    # A resolved app ALWAYS has a status and an appKey — an absent/cross-user one is a 404, not
    # a null-signalling 200 (the `status: null` "not provisioned" shim is gone). The submission
    # fields are legitimately absent until the first submit; `rejectionNote` is set solely on
    # the rejected transition (and cleared by the next submit).
    app_id: uuid.UUID
    status: AppStatus
    app_key: str
    rejection_note: str | None
    submission_id: uuid.UUID | None
    commit_sha: str | None
    submitted_at: datetime | None
    # "Your app is live" (R5): the manual-runbook marker, READ-ONLY here. Both are
    # absent until a superadmin marks the app deployed, and `deployedUrl` stays null
    # if they recorded the deploy without an address — so the owner's Live link is
    # gated on the URL, not on the timestamp or on `status == approved`.
    deployed_at: datetime | None
    deployed_url: str | None
