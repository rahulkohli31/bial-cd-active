"""App-lifecycle request/response schemas — submit / status.

camelCase over the wire (via the shared `CamelModel`), matching the SPA/TS
convention the `/api/apps/*` clients already consume.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, model_validator

from src.db.models.app_registry import DATA_CLASSIFICATION_QUESTIONS, AppStatus
from src.schemas import CamelModel

# Submit's artifact is still server-side (the git-bundle snapshot, copied to an
# immutable submission blob) — but submit now ALSO takes a required body (V4 Part 1):
# the data-classification questionnaire the owner must answer. Nothing about the
# artifact itself is client-supplied. As of V4 Part 2, submit ALSO decides the
# resulting approve/reject outcome itself, from the same answers — there is no human
# review step in between (see `AUTO_APPROVE_AT` below and `apps/router.py::submit`).

# The soft-gate threshold (V4 Part 1): at or above this weighted total the explanation
# box stops being optional.
_NOTES_REQUIRED_AT = 25

# The auto-approve/reject threshold (V4 Part 2): `submit` decides the outcome itself —
# no human review — at this weighted total. `score >= AUTO_APPROVE_AT` auto-approves;
# below it auto-rejects. DELIBERATELY INDEPENDENT of `_NOTES_REQUIRED_AT` above: a
# submission can cross the notes gate (must explain itself) while still landing below
# this one (still gets auto-rejected) — e.g. Personal Information + Financial Data (40)
# requires an explanation but is not enough to auto-approve on its own. Do not conflate
# the two constants or assume one implies the other.
AUTO_APPROVE_AT = 50


class DataClassificationAnswers(CamelModel):
    """The submit-time data-classification questionnaire (V4). All six categories are
    required booleans — a partially-answered set never reaches this schema at all (the
    portal only constructs one once every question has a Yes/No), so "unanswered" is a
    frontend-only, pre-submission state that this boundary never has to represent.
    `notes` is optional UNLESS the weighted total reaches `_NOTES_REQUIRED_AT`, checked
    by the validator below — the server's own gate, not just the portal's disabled
    Confirm button."""

    credentials_secrets: bool
    health_data: bool
    personal_information: bool
    financial_data: bool
    confidential_business_data: bool
    public_data: bool
    # Bounded the same way admin's `RejectRequest.note` is — an over-long explanation
    # is rejected at the boundary, never silently truncated.
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _notes_required_above_threshold(self) -> DataClassificationAnswers:
        if total_weight(self) >= _NOTES_REQUIRED_AT and not (self.notes or "").strip():
            raise ValueError(
                "This submission involves higher-sensitivity data — an explanation is required."
            )
        return self


def total_weight(answers: DataClassificationAnswers) -> int:
    """The weighted total the soft gate (both here and in the portal) keys off —
    computed from `DATA_CLASSIFICATION_QUESTIONS`, never a hardcoded sum, so a future
    reweight only ever touches the one tuple."""
    return sum(
        weight for key, _label, weight in DATA_CLASSIFICATION_QUESTIONS if getattr(answers, key)
    )


class SubmitRequest(CamelModel):
    answers: DataClassificationAnswers


class SubmitResponse(CamelModel):
    app_id: uuid.UUID
    # V4 Part 2: no longer always `pending` — `submit` decides `approved` or `rejected`
    # itself, from the same request, before this response is built.
    status: AppStatus
    # The immutable submission the copy minted (R1/R4): the id the (now-automatic)
    # decision pins on approval, and the bundle's HEAD commit SHA for provenance.
    submission_id: uuid.UUID
    commit_sha: str
    submitted_at: datetime
    # V4 Part 2: set when `status == rejected` (the auto-reject copy), else `None`. The
    # old "submit always clears the rejection note" contract no longer holds — a submit
    # can now itself PRODUCE one, so the caller must read it from here rather than
    # assume it was cleared.
    rejection_note: str | None


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
    # "Your app is live" (R5): the manual-runbook marker, READ-ONLY here. Both are
    # absent until a superadmin marks the app deployed, and `deployedUrl` stays null
    # if they recorded the deploy without an address — so the owner's Live link is
    # gated on the URL, not on the timestamp or on `status == approved`.
    deployed_at: datetime | None
    deployed_url: str | None
    # V4: absent for a never-submitted app AND for a legacy submission predating this
    # feature — both render as `null`, distinguishable from an answered set (which is
    # always all six keys present, by construction at `DataClassificationAnswers`).
    data_classification: DataClassificationAnswers | None
