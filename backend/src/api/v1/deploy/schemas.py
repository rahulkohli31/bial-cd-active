"""Request/response bodies for one-click deploy.

The response deliberately carries BOTH the machine-readable failure code and the prose: a
client needs the code to decide what to offer next (retry, open the chat, tell an admin),
and the citizen needs the sentence. Reporting only one of the two has to be worked around
later by whichever consumer was left short.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from src.db.models.deployment import Deployment
from src.schemas import CamelModel
from src.services.deploy.classification import CLASSIFICATION_KEYS, notes_required


class DataClassificationAnswers(CamelModel):
    """What the citizen declares their app handles, answered fresh at every deploy.

    All six categories are required booleans. A partly-answered set never reaches this
    schema — the portal only builds one once every question has a Yes or a No — so
    "unanswered" is a pre-submission state on the client and not something this boundary
    has to represent. Making them required rather than defaulting to False also means a
    caller cannot under-declare by omission, which a default would have made invisible.
    """

    credentials_secrets: bool
    health_data: bool
    personal_information: bool
    financial_data: bool
    confidential_business_data: bool
    public_data: bool
    # Bounded at the boundary the way admin's `RejectRequest.note` is — an over-long
    # explanation is rejected, never silently truncated into a record that misrepresents
    # what was said.
    notes: str | None = Field(default=None, max_length=1000)

    def classification_flags(self) -> dict[str, bool]:
        """The six answers as the plain mapping the policy module scores.

        Built from `CLASSIFICATION_KEYS` rather than a literal dict so a question added to
        the questionnaire cannot be silently dropped here — it would fail loudly at the
        `getattr` instead of quietly scoring as No.
        """
        return {key: bool(getattr(self, key)) for key in CLASSIFICATION_KEYS}

    @model_validator(mode="after")
    def _explanation_required_above_threshold(self) -> DataClassificationAnswers:
        """The server's own notes gate, not merely the portal's disabled Confirm button.

        A 422 rather than a scoring refusal on purpose: an unexplained sensitive
        declaration is an INCOMPLETE submission, not a rejected one. Conflating the two
        would tell someone whose answers actually qualify that they failed the gate.
        """
        if notes_required(self.classification_flags()) and not (self.notes or "").strip():
            raise ValueError(
                "This app handles higher-sensitivity data — please explain what it does "
                "with it before deploying."
            )
        return self


class DeployRequest(CamelModel):
    """`saveFirst` is the citizen's explicit "save and deploy".

    Default False, and that is the safe default: a deploy ships the last SAVED version, so
    deploying over unsaved work without being asked publishes something they never chose and
    gives them no way to notice.

    `answers` is REQUIRED, which is what makes the questionnaire a gate rather than a
    prompt: there is no shape of this request that deploys without a declaration, so no
    caller can reach the pipeline by simply not asking for the modal. It is re-answered at
    every deploy rather than remembered on the app, because the agent edits the app between
    deploys — a declaration made three deploys ago is not evidence about what is shipping
    now. A client that wants one-click redeploys prefills the form from the previous
    answers; that is a client affordance and still arrives here as a fresh declaration.
    """

    save_first: bool = False
    answers: DataClassificationAnswers


class DeployStartedResponse(CamelModel):
    """The 202 body. Carries the id to poll — the deploy itself has barely begun."""

    deployment_id: str
    app_id: str
    status: str


class DeploymentResponse(CamelModel):
    """The latest deploy attempt, or an empty envelope when there has never been one.

    Empty rather than a 404: "this app has never been deployed" is a normal state a client
    renders as a Deploy button, not an error."""

    deployment_id: str | None = None
    app_id: str | None = None
    status: str | None = None
    step: str | None = None
    url: str | None = None
    head_sha: str | None = None
    failure_code: str | None = None
    failure_detail: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def of(cls, row: Deployment) -> DeploymentResponse:
        # `image_digest`, `acr_run_id` and `revision_name` are deliberately NOT surfaced:
        # they are operator facts with no meaning to a citizen, and the digest in particular
        # is the reconciler's proof of ownership rather than something a client should be
        # able to read back and reason about.
        return cls(
            deployment_id=str(row.id),
            app_id=str(row.app_id),
            status=row.status.value,
            step=row.step,
            url=row.url,
            head_sha=row.head_sha,
            failure_code=row.failure_code,
            failure_detail=row.failure_detail,
            started_at=row.created_at,
            finished_at=row.finished_at,
        )
