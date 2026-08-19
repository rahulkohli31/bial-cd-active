"""The classification review runner (U6): start a review for a version, land exactly one
result, and turn every way it can fail into a state the rest of the system can act on.

Two halves with a hard line between them, the deploy service's shape deliberately.

The START half is synchronous and fast: enforce the three-runs-per-version cap, claim (or
get back) the app's one review row, and detach the run. `head_sha` is the CALLER'S to
resolve — U7's routes read it from the snapshot blob's stored metadata (the save-state
reader's exact move, never an extraction), and U10's drift path hands over the extraction
it already holds. The service takes the stamp as input and fails closed if the tree it
extracts turns out to be a different commit; resolving metadata here would put a storage
read inside a service that otherwise only needs it mid-run, and would give the two
callers two different ways to disagree with themselves.

The RUN half is a detached task held in a strong-reference set. It NEVER raises, and
every write opens its own short session from the session factory — it outlives its
request. It extracts into a throwaway directory of its own (under the process temp root —
the plan's deferred location question, settled as the one-line choice) and deletes it in
a `finally`, unconditionally: success, every failure bucket, the wall-clock ceiling, and
cancellation. A root a CALLER handed over (U10) is never deleted — ownership stays with
whoever created it — and the run never joins the shared SHA-keyed extraction cache, so
nothing it removes was ever another consumer's.

THE SCAN RUNS FIRST, then the model (P8): hits go into the prompt as directed evidence —
location and family, never a value. The review's verdict is the credentials answer,
including against the scan; a Tier A overrule is recorded as a dispute, and when the
model never returned at all, a Tier A hit from a COMPLETE sweep stands in as the
credentials answer on the failed row (the floor), with the other five left unanswered.

TRUNCATION IS CAUGHT AT THE MODEL SEAM, not in the agent loop. `_MeteredModel` wraps
whatever model the run was given and raises on `finish_reason == "length"` BEFORE the
response reaches output validation — otherwise a clipped structured output presents as a
validation failure and the agent's `retries=2` re-runs it at the same cap, twice, for a
guaranteed second failure. One guided retry follows, in the same conversation minus
exactly the truncated turn, with a nudge that CONSTRAINS the output; a second truncation
is review-failed, and no partial verdicts are ever salvaged.

THE SPEND IS METERED BUT IS NOT THE CITIZEN'S TO PAY (ASM14). Two halves, both
deliberate: the daily token gate is NEVER consulted (a heavy build day must not make an
app unpublishable), and usage is recorded on the `review` kind, which that gate does not
read (opening the publish dialog must not silently spend build budget the citizen never
chose to spend). The spend is still recorded against them — knowing who generates review
cost is the point. The real bound is `MAX_MODEL_RUNS_PER_VERSION` plus the per-run
request budget and wall-clock ceiling in `constants.py`.

EVERY TERMINAL RUN WRITES AN AUDIT ROW (P7): the triggering citizen as actor plus their
email in detail (the actor reference nulls when a user is removed), the version stamp,
the outcome or failure bucket, and the six verdicts. The store row is one-per-app and
overwritten, so the audit trail is the only place re-runs can be counted — a run not
recorded is gone.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa
import structlog
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.run import AgentRunResult
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.redaction import Tier, redact_secrets
from src.db.models.classification_review import ClassificationReviewStatus
from src.db.models.token_usage import TokenUsageKind
from src.db.models.user import User
from src.services.audit.log import append_audit
from src.services.classification import store
from src.services.classification.agent import run_review
from src.services.classification.constants import (
    REVIEW_REQUEST_BUDGET,
    REVIEW_WALL_CLOCK_CEILING_S,
)
from src.services.classification.scan import CredentialSweep, scan_snapshot
from src.services.classification.schema import Completeness, ReviewOutput, Verdict
from src.services.classification.store import ReviewRecord
from src.services.deploy.classification import CLASSIFICATION_KEYS
from src.services.storage.bundle import BundleValidationError
from src.services.storage.errors import StorageError
from src.services.storage.snapshot_read import (
    ExtractedSnapshot,
    NoAppYet,
    SnapshotExtractionError,
    extract_snapshot,
)
from src.services.usage.gate import record_usage

_log = structlog.get_logger()

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# Failure buckets (the plan's taxonomy) plus the drift code. Stable and greppable — U7
# maps each to its own citizen-facing sentence, and an operator alerts on the string.
FAIL_NO_APP: Final = "no_app_yet"
"""Nothing saved to check yet (`NoAppYet`) — publishing already refuses this state."""
FAIL_BUNDLE_UNREADABLE: Final = "bundle_unreadable"
"""The saved bundle exists but could not be read or extracted — retrying cannot succeed."""
FAIL_STORAGE: Final = "storage_unavailable"
"""Object storage is down or unconfigured — publishing itself is equally unavailable."""
FAIL_REVIEW: Final = "review_failed"
"""The model never produced a usable answer: model/API error, malformed output, a double
truncation, a quota refusal, a partial completeness signal, or an internal crash."""
FAIL_ABANDONED: Final = "review_abandoned"
"""Over the wall-clock ceiling (measured from the ROW's `started_at`), or a row a
restart orphaned that aged out."""
FAIL_VERSION_DRIFT: Final = "version_drift"
"""The extracted tree was a different commit than the claimed stamp — a save landed
between the caller's metadata read and the extraction. Failed closed; the citizen's next
open claims a fresh review for the real version."""

MAX_MODEL_RUNS_PER_VERSION: Final = 3
"""The attempt cap that makes the token-gate carve-out honest: at most three model runs
per version, counted on the review row. A fourth start returns the stored failure without
touching the model, and the app routes to an administrator — where R20 was sending it."""

AUDIT_ACTION: Final = "classification_review"
"""The P7 audit action. App-scoped (`resource_type="app"`, `resource_id=str(app_id)`)
with the app id repeated in detail, so the admin app drawer's resource-or-detail match
finds it either way (ASM7)."""

# Failure detail is redacted then capped, in that order — capping first can slice a
# credential and leave the recognizable prefix behind (the deploy service's rule).
_DETAIL_MAX_CHARS: Final = 2_000

# The guided-retry nudge (user-role). It must DIFFER from the original ask and CONSTRAIN
# the output — an identical re-ask truncates identically one step later, at twice the
# price. The conversation it lands in already carries the instructions, the scan's hits
# and every tool exchange, so nothing is repeated here.
_TRUNCATION_NUDGE: Final = (
    "Your previous answer was cut off at the output token limit and has been discarded. "
    "Record the complete six-question review again, and keep it short: at most one "
    "sentence per reason, and only the single strongest evidence location per question."
)

# Canned floor copy — plain language, no locations, no values (R3 applies to these too).
_FLOOR_CREDENTIALS_REASON: Final = (
    "The automatic check could not finish, but a pattern scan found what looks like a "
    "real credential written into the app's saved code."
)
_FLOOR_UNANSWERED_REASON: Final = (
    "The automatic check could not finish, so this question needs your own answer."
)


class _TruncatedAtTheCapError(Exception):
    """The model stopped at the output token cap (`finish_reason == "length"`). Raised
    by `_MeteredModel` from INSIDE the model seam, so the truncated response never
    reaches output validation and the agent's own retries never re-run at the same cap.

    Carries the raw provider finish reason (for `failure_detail` — a cap overshoot must
    stay diagnosable from an endpoint problem) and the conversation UP TO the truncated
    turn: the messages handed to the model on the truncating request are exactly "the
    entire conversation minus the trailing partial assistant turn" the guided retry
    must resend."""

    def __init__(self, *, raw_finish_reason: str, history: list[ModelMessage]) -> None:
        super().__init__(f"model output truncated (finish_reason={raw_finish_reason!r})")
        self.raw_finish_reason = raw_finish_reason
        self.history = history


class _MeteredModel(WrapperModel):
    """The run's flight recorder around whatever model it was given: counts requests
    (the retry-budget arithmetic reads it), accumulates the four RAW usage classes
    across the whole run — both agent runs, failure paths included, so a failed run's
    spend is still real — and trips on truncation (see `_TruncatedAtTheCapError`).

    Raw means raw: pydantic-ai's `input_tokens` is the grand total WITH the cache
    classes folded in, and they are persisted exactly as reported — re-adding cache
    reads/writes into input is the documented double-count regression."""

    def __init__(self, wrapped: Model) -> None:
        super().__init__(wrapped)
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        self.requests += 1
        usage = response.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        if response.finish_reason == "length":
            # The truncated response was still produced and charged — its usage is
            # tallied above — but it must never be parsed, retried in place, or
            # salvaged. `messages` is the conversation as sent, i.e. WITHOUT the
            # partial assistant turn; a shallow copy pins it for the guided retry.
            details = response.provider_details or {}
            raise _TruncatedAtTheCapError(
                raw_finish_reason=str(details.get("finish_reason", "length")),
                history=list(messages),
            )
        return response


class _ReviewFailedError(Exception):
    """A run failure with its taxonomy bucket and an operator-grade detail. The citizen
    prose lives in U7's route (keyed on the code), not here."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class _RunScratch:
    """What the failure path needs from however far the run got: the sweep (the Tier A
    floor reads it) and the meter (usage is recorded whether the run succeeded or not)."""

    sweep: CredentialSweep | None = None
    metered: _MeteredModel | None = None


@dataclass(frozen=True)
class ReviewReadout:
    """The read verb's answer: the stored row plus the one derivation the store cannot
    make — whether a RUNNING row has aged out past the wall-clock ceiling. A restart
    kills the detached task but leaves the row RUNNING; readers (U7) must render an
    aged-out row as the review-abandoned state, never as still-in-flight, and `start`
    un-wedges it on the next request."""

    review: ReviewRecord
    aged_out: bool


class ReviewModelUnavailableError(RuntimeError):
    """Foundry is not configured, so no review model can be built. Raised from the
    model factory at RUN time — after the scan — so the failure lands in the
    review-failed bucket with the Tier A floor still applied."""


def _make_throwaway_root() -> Path:
    """One fresh, private extraction root under the process temp root. Synchronous —
    callers offload it with `asyncio.to_thread` like every other filesystem touch."""
    return Path(tempfile.mkdtemp(prefix="bial-classification-review-"))


def _seconds_left(review: ReviewRecord) -> float:
    """Wall-clock budget remaining, measured from the ROW's `started_at` (aware, from
    the claim's `now()`) — never from a dialog opening, so reloads cannot extend it."""
    elapsed = (datetime.now(UTC) - review.started_at).total_seconds()
    return REVIEW_WALL_CLOCK_CEILING_S - elapsed


def _aged_out(review: ReviewRecord) -> bool:
    return review.status is ClassificationReviewStatus.RUNNING and _seconds_left(review) <= 0


def _safe_detail(detail: str | None) -> str | None:
    """Redact then cap, in that order (capping first can slice a credential in half and
    leave the recognizable prefix behind)."""
    if not detail:
        return None
    return redact_secrets(detail)[:_DETAIL_MAX_CHARS]


class ClassificationReviewService:
    """Owns the in-flight review tasks. One process-wide instance (U7 wires the
    singleton below into its routes; tests build their own with a scripted model)."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        model_factory: Callable[[], Model],
    ) -> None:
        self._session_factory = session_factory
        self._model_factory = model_factory
        # Strong references — a task the loop garbage-collects mid-flight would leave a
        # RUNNING row nothing will ever settle (until it ages out).
        self._tasks: set[asyncio.Task[None]] = set()

    # --- the start verb ---------------------------------------------------------

    async def start(
        self,
        db: AsyncSession,
        *,
        app_id: uuid.UUID,
        user_id: uuid.UUID,
        head_sha: str,
        extracted: ExtractedSnapshot | None = None,
    ) -> ReviewRecord:
        """Ensure a review exists for this app at `head_sha` and return its row: the
        stored answer when the version is unchanged (R6), the stored failure when the
        attempt cap is spent, or a fresh RUNNING row with the run detached.

        `extracted` is for U10's drift path only — a tree the CALLER extracted and
        still owns; the run uses it and never deletes it. Every other caller leaves it
        None and the run extracts (and unconditionally removes) its own copy."""
        stored = await store.get_for_app(db, app_id=app_id)
        if stored is not None and stored.head_sha == head_sha:
            if _aged_out(stored):
                # A restart orphaned this run: the task died, the row hung RUNNING.
                # Settle it as abandoned so it can be re-claimed — a restart must age
                # out, not wedge the app's review forever.
                settled = await store.fail(
                    db,
                    review_id=stored.review_id,
                    head_sha=stored.head_sha,
                    attempt=stored.attempt,
                    code=FAIL_ABANDONED,
                    detail="the run aged out past the wall-clock ceiling with no runner alive",
                )
                if settled:
                    await self._append_run_audit(
                        db,
                        review=stored,
                        outcome=FAIL_ABANDONED,
                        verdict_summary=None,
                        superseded=False,
                    )
                    await db.commit()
                stored = await store.get_for_app(db, app_id=app_id)
            if (
                stored is not None
                and stored.head_sha == head_sha
                and stored.status is ClassificationReviewStatus.FAILED
                and stored.attempt >= MAX_MODEL_RUNS_PER_VERSION
            ):
                # The fourth claim: the cap is the review's real spend bound. Return
                # the stored failure WITHOUT claiming or touching the model — the app
                # routes to an administrator, which is where R20 was sending it anyway.
                return stored

        outcome = await store.claim(db, app_id=app_id, user_id=user_id, head_sha=head_sha)
        if not outcome.claimed:
            return outcome.review

        task = asyncio.create_task(self._run(review=outcome.review, extracted=extracted))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return outcome.review

    # --- the read verb ----------------------------------------------------------

    async def read(self, db: AsyncSession, *, app_id: uuid.UUID) -> ReviewReadout | None:
        """The app's stored review, or None if none was ever claimed. Version
        comparison is the CALLER's concern (the record carries its stamp); the one
        derivation added here is `aged_out` — see `ReviewReadout`."""
        record = await store.get_for_app(db, app_id=app_id)
        if record is None:
            return None
        return ReviewReadout(review=record, aged_out=_aged_out(record))

    # --- the detached run -------------------------------------------------------

    async def _run(self, *, review: ReviewRecord, extracted: ExtractedSnapshot | None) -> None:
        """The detached run. NEVER raises: an escaping exception would leave the row
        RUNNING until it ages out, with the citizen staring at a spinner the whole
        ceiling long."""
        scratch = _RunScratch()
        try:
            verdicts, evidence = await self._review(
                review=review, extracted=extracted, scratch=scratch
            )
        except _ReviewFailedError as failure:
            await self._settle_failed(review, failure=failure, scratch=scratch)
        except asyncio.CancelledError:
            # Shutdown. The extraction was already unwound by `_review`'s finally; the
            # row is left RUNNING and ages out, which `start` and `read` both handle.
            raise
        except Exception as exc:
            _log.exception("classification_review_crashed", review_id=str(review.review_id))
            await self._settle_failed(
                review,
                failure=_ReviewFailedError(FAIL_REVIEW, type(exc).__name__),
                scratch=scratch,
            )
        else:
            await self._settle_complete(
                review, verdicts=verdicts, evidence=evidence, scratch=scratch
            )

    async def _review(
        self,
        *,
        review: ReviewRecord,
        extracted: ExtractedSnapshot | None,
        scratch: _RunScratch,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Extraction ownership, and nothing else. A caller-owned tree (U10) is used and
        NEVER deleted; otherwise the run extracts into a throwaway root of its own and
        removes it in the `finally` — unconditionally: success, every failure bucket,
        the wall-clock ceiling, and cancellation."""
        if extracted is not None:
            return await self._examine(review=review, extracted=extracted, scratch=scratch)
        # The run's throwaway extraction root, under the process temp root (the plan's
        # deferred location question, settled as this one line). Never the shared
        # SHA-keyed cache: verdicts live in a row, so reuse buys nothing, and a private
        # root can never delete a directory another request is mid-read on.
        own_root = await asyncio.to_thread(_make_throwaway_root)
        try:
            extracted = await self._extract(review.app_id, cache_root=own_root)
            return await self._examine(review=review, extracted=extracted, scratch=scratch)
        finally:
            await asyncio.to_thread(shutil.rmtree, own_root, ignore_errors=True)

    async def _examine(
        self,
        *,
        review: ReviewRecord,
        extracted: ExtractedSnapshot,
        scratch: _RunScratch,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The happy path over an extracted tree; every failure leaves by raising
        `_ReviewFailedError`. Returns the verdicts and evidence documents ready to
        store."""
        if extracted.head_sha != review.head_sha:
            # A save landed between the caller's metadata read and this extraction.
            # Fail closed — the citizen's next open starts a fresh review for the
            # commit that actually exists now.
            raise _ReviewFailedError(
                FAIL_VERSION_DRIFT,
                f"claimed {review.head_sha} but extracted {extracted.head_sha}",
            )

        # The scan FIRST (P8): model-free, fast, and its hits become the prompt's
        # directed evidence. From here on the Tier A floor is armed via `scratch`.
        sweep = await scan_snapshot(extracted.root)
        scratch.sweep = sweep

        # The model is built AFTER the scan on purpose: an unconfigured Foundry is
        # a review-failed WITH the floor applied, not a floorless crash.
        metered = _MeteredModel(self._model_factory())
        scratch.metered = metered

        result = await self._call_model(
            metered, review=review, snapshot_root=extracted.root, sweep=sweep
        )

        output = result.output
        if output.completeness is Completeness.PARTIAL:
            # The model itself says it was cut short. Stored as a FAILURE, never as
            # six abstentions — the ambiguity the signal exists to remove.
            raise _ReviewFailedError(FAIL_REVIEW, "the model reported a partial review")
        return _build_record(output, root=extracted.root, sweep=sweep)

    async def _extract(self, app_id: uuid.UUID, *, cache_root: Path) -> ExtractedSnapshot:
        """Extract the saved bundle into the run's own root, mapping every way storage
        can disappoint onto its taxonomy bucket."""
        try:
            extracted = await extract_snapshot(app_id, cache_root=cache_root)
        except BundleValidationError as exc:
            raise _ReviewFailedError(FAIL_BUNDLE_UNREADABLE, str(exc)) from exc
        except SnapshotExtractionError as exc:
            raise _ReviewFailedError(FAIL_BUNDLE_UNREADABLE, str(exc)) from exc
        except StorageError as exc:
            # Down or unconfigured alike — and publishing reads the same bundle, so
            # nobody is stranded behind a gate that works while the pipeline doesn't.
            raise _ReviewFailedError(FAIL_STORAGE, str(exc)) from exc
        if isinstance(extracted, NoAppYet):
            raise _ReviewFailedError(FAIL_NO_APP)
        return extracted

    async def _call_model(
        self,
        metered: _MeteredModel,
        *,
        review: ReviewRecord,
        snapshot_root: Path,
        sweep: CredentialSweep,
    ) -> AgentRunResult[ReviewOutput]:
        """The model phase under the wall-clock ceiling, with the one guided
        truncation retry. Every framework failure is mapped to its bucket here, at the
        point it is known."""
        remaining = _seconds_left(review)
        if remaining <= 0:
            raise _ReviewFailedError(
                FAIL_ABANDONED, "the wall-clock ceiling elapsed before the model was called"
            )
        try:
            async with asyncio.timeout(remaining):
                return await self._run_with_truncation_retry(
                    metered, review=review, snapshot_root=snapshot_root, sweep=sweep
                )
        except TimeoutError:
            raise _ReviewFailedError(
                FAIL_ABANDONED,
                f"over the {REVIEW_WALL_CLOCK_CEILING_S:.0f}s wall-clock ceiling",
            ) from None
        except UsageLimitExceeded as exc:
            # The run's own request budget ran out mid-flight. A failure, never an
            # empty answer set — and never a bucket of its own, because the citizen's
            # sentence is the same either way.
            raise _ReviewFailedError(FAIL_REVIEW, str(exc)) from exc
        except (UnexpectedModelBehavior, ModelAPIError) as exc:
            # Malformed output past the agent's retries, a quota refusal, any provider
            # error — one bucket, distinguished by the stored detail.
            raise _ReviewFailedError(FAIL_REVIEW, str(exc)) from exc

    async def _run_with_truncation_retry(
        self,
        metered: _MeteredModel,
        *,
        review: ReviewRecord,
        snapshot_root: Path,
        sweep: CredentialSweep,
    ) -> AgentRunResult[ReviewOutput]:
        try:
            return await run_review(
                model=metered,
                user_id=review.user_id,
                snapshot_root=snapshot_root,
                scan_hits=sweep.hits,
                usage_limits=UsageLimits(request_limit=REVIEW_REQUEST_BUDGET),
            )
        except _TruncatedAtTheCapError as first:
            # The expensive part of the run — the tool exchanges, the file contents —
            # is in `first.history` and did not go wrong. ONE guided retry resends it
            # all, minus exactly the truncated turn, with a constraining nudge.
            budget_left = REVIEW_REQUEST_BUDGET - metered.requests
            if budget_left < 1:
                raise _ReviewFailedError(
                    FAIL_REVIEW,
                    "output truncated at the token cap with no request budget left for "
                    f"the guided retry (finish_reason={first.raw_finish_reason!r})",
                ) from first
            try:
                return await run_review(
                    model=metered,
                    user_id=review.user_id,
                    snapshot_root=snapshot_root,
                    prompt=_TRUNCATION_NUDGE,
                    message_history=first.history,
                    usage_limits=UsageLimits(request_limit=budget_left),
                )
            except _TruncatedAtTheCapError as second:
                # Twice is a genuine failure. No salvage from either attempt.
                raise _ReviewFailedError(
                    FAIL_REVIEW,
                    "output truncated twice at the token cap "
                    f"(finish_reason={second.raw_finish_reason!r})",
                ) from second

    # --- terminals --------------------------------------------------------------

    async def _settle_complete(
        self,
        review: ReviewRecord,
        *,
        verdicts: dict[str, Any],
        evidence: dict[str, Any],
        scratch: _RunScratch,
    ) -> None:
        async with self._session_factory() as db:
            settled = await store.succeed(
                db,
                review_id=review.review_id,
                head_sha=review.head_sha,
                attempt=review.attempt,
                verdicts=verdicts,
                evidence=evidence,
                answers_complete=True,
                **_usage_columns(scratch),
            )
        _log.info(
            "classification_review_complete",
            review_id=str(review.review_id),
            app_id=str(review.app_id),
            settled=settled,
        )
        await self._record_run(
            review,
            outcome="complete",
            verdict_summary=_verdict_summary(verdicts),
            scratch=scratch,
            superseded=not settled,
        )

    async def _settle_failed(
        self,
        review: ReviewRecord,
        *,
        failure: _ReviewFailedError,
        scratch: _RunScratch,
    ) -> None:
        # P8's second obligation: the model never returned, but a COMPLETE sweep with a
        # Tier A hit is strong enough to stand in as the credentials answer. The row is
        # still FAILED (it still routes, R20) — the verdicts just carry the floor.
        floor = _floor_record(scratch.sweep)
        verdicts, evidence = floor if floor is not None else (None, None)
        async with self._session_factory() as db:
            settled = await store.fail(
                db,
                review_id=review.review_id,
                head_sha=review.head_sha,
                attempt=review.attempt,
                code=failure.code,
                detail=_safe_detail(failure.detail),
                verdicts=verdicts,
                evidence=evidence,
                **_usage_columns(scratch),
            )
        _log.warning(
            "classification_review_failed",
            review_id=str(review.review_id),
            app_id=str(review.app_id),
            code=failure.code,
            settled=settled,
        )
        await self._record_run(
            review,
            outcome=failure.code,
            verdict_summary=_verdict_summary(verdicts) if verdicts is not None else None,
            scratch=scratch,
            superseded=not settled,
        )

    async def _record_run(
        self,
        review: ReviewRecord,
        *,
        outcome: str,
        verdict_summary: dict[str, str] | None,
        scratch: _RunScratch,
        superseded: bool,
    ) -> None:
        """The per-run records: the citizen's spend (raw, `review` kind) and the P7
        audit row. Best-effort by design — the review row is the record of truth, and a
        bookkeeping write that fails must not turn a settled review into a crash."""
        try:
            metered = scratch.metered
            if metered is not None and metered.requests > 0:
                # Raw across all four classes, exactly as pydantic-ai reported them —
                # `input_tokens` already INCLUDES the cache classes; re-adding them is
                # the documented double-count regression. `kind=REVIEW` keeps it off
                # the citizen's budget; `enforce_daily_limit` is deliberately NEVER
                # called anywhere in this service (the other half of the carve-out).
                async with self._session_factory() as db:
                    await record_usage(
                        db,
                        review.user_id,
                        input_tokens=metered.input_tokens,
                        output_tokens=metered.output_tokens,
                        cache_read_tokens=metered.cache_read_tokens,
                        cache_write_tokens=metered.cache_write_tokens,
                        kind=TokenUsageKind.REVIEW,
                    )
                    await db.commit()
            async with self._session_factory() as db:
                await self._append_run_audit(
                    db,
                    review=review,
                    outcome=outcome,
                    verdict_summary=verdict_summary,
                    superseded=superseded,
                )
                await db.commit()
        except Exception:
            _log.warning(
                "classification_review_records_not_written",
                review_id=str(review.review_id),
                exc_info=True,
            )

    async def _append_run_audit(
        self,
        db: AsyncSession,
        *,
        review: ReviewRecord,
        outcome: str,
        verdict_summary: dict[str, str] | None,
        superseded: bool,
    ) -> None:
        """One P7 row in the caller's transaction (the caller commits). App-scoped, and
        the actor's email rides in detail because the actor REFERENCE nulls when a user
        is removed — the trail must keep saying who triggered the run."""
        email = await db.scalar(sa.select(User.email).where(User.id == review.user_id))
        detail: dict[str, Any] = {
            "appId": str(review.app_id),
            "email": email,
            "headSha": review.head_sha,
            "attempt": review.attempt,
            "outcome": outcome,
            "verdicts": verdict_summary,
        }
        if superseded:
            # The store dropped this run's terminal write (a newer claim took over).
            # The run still happened and still spent — the trail says so.
            detail["superseded"] = True
        await append_audit(
            db,
            actor_id=review.user_id,
            action=AUDIT_ACTION,
            resource_type="app",
            resource_id=str(review.app_id),
            detail=detail,
        )

    # --- plumbing ---------------------------------------------------------------

    async def drain(self) -> None:
        """Await every in-flight run. Tests use it; the lifespan lets runs be
        cancelled instead — a cancelled run's row ages out and `start` un-wedges it."""
        for task in list(self._tasks):
            try:
                await task
            except Exception:  # noqa: BLE001 — a run's own failure was already recorded
                _log.warning("classification_review_task_error", exc_info=True)


# --- the stored record shapes -------------------------------------------------------


def _usage_columns(scratch: _RunScratch) -> dict[str, int]:
    metered = scratch.metered
    if metered is None:
        return {}
    return {
        "input_tokens": metered.input_tokens,
        "output_tokens": metered.output_tokens,
        "cache_read_tokens": metered.cache_read_tokens,
        "cache_write_tokens": metered.cache_write_tokens,
    }


def _cites_a_real_location(root: Path, rel_path: str) -> bool:
    """R4's machine check: the cited path must resolve to a real FILE inside the
    extracted tree. Resolution-jailed like the read tools — a traversal or an absolute
    path is simply not evidence."""
    try:
        resolved = (root / rel_path).resolve()
        root_resolved = root.resolve()
    except OSError:
        return False
    if resolved != root_resolved and root_resolved not in resolved.parents:
        return False
    return resolved.is_file()


def _build_record(
    output: ReviewOutput, *, root: Path, sweep: CredentialSweep
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The completed output → the two stored documents.

    `verdicts` is the citizen/administrator-safe half: per-question verdict, REDACTED
    reason, scan agreement, and the downgrade marker — plus a compact `scan` block
    (booleans only, no locations) so U7/U9 can read the Tier A dispute and an
    incomplete sweep straight off the row. `evidence` is the internal half (R4): the
    cited locations with their validity, the scan's located hits, and the downgraded
    keys — stored for machine checking, never rendered to a person (OD-B).

    Two rules run BEFORE anything is written: a Yes with no VALID cited location is
    downgraded to unanswered (R4/R5 — the question goes to the citizen, a flag is never
    silently cleared), and every reason passes through the shared redactor — the
    deterministic backstop behind the prompt's plain-language instruction, since this
    text reaches both the citizen and the administrator."""
    tier_a = any(located.hit.tier is Tier.A for located in sweep.hits)
    tier_b = any(located.hit.tier is Tier.B for located in sweep.hits)

    questions: dict[str, Any] = {}
    evidence_questions: dict[str, Any] = {}
    downgraded: list[str] = []
    tier_a_dispute = False

    for question in output.questions:
        refs = [
            {
                "path": ref.path,
                "kind": ref.kind,
                "valid": _cites_a_real_location(root, ref.path),
            }
            for ref in question.evidence
        ]
        verdict = question.verdict
        was_downgraded = False
        if verdict is Verdict.YES and not any(ref["valid"] for ref in refs):
            # R4: a Yes whose every cited location does not exist is not evidence. It
            # becomes exactly the state R5 defines — unanswered, handed to the citizen
            # — and the downgrade is recorded, never silently absorbed.
            verdict = Verdict.UNANSWERED
            was_downgraded = True
            downgraded.append(question.key)
        if question.key == "credentials_secrets" and tier_a and question.verdict is Verdict.NO:
            # The model was SHOWN a Tier A hit and said No. Its No is the verdict (P8)
            # — but an overrule nobody can see is the same as having no scan.
            tier_a_dispute = True
        questions[question.key] = {
            "verdict": verdict.value,
            "reason": redact_secrets(question.reason),
            "agreed_with_scan": question.agreed_with_scan,
            "downgraded_from_yes": was_downgraded,
        }
        evidence_questions[question.key] = refs

    verdicts_doc: dict[str, Any] = {
        "source": "review",
        "questions": questions,
        "scan": {
            "tier_a_hit": tier_a,
            "tier_b_hit": tier_b,
            "incomplete": sweep.incomplete,
            "tier_a_dispute": tier_a_dispute,
        },
    }
    evidence_doc: dict[str, Any] = {
        "questions": evidence_questions,
        "scan_hits": _scan_hit_refs(sweep),
        "downgraded": downgraded,
    }
    return verdicts_doc, evidence_doc


def _floor_record(sweep: CredentialSweep | None) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The Tier A floor's stored shape, or None when the floor does not stand.

    It stands only when the sweep RAN, is COMPLETE, and holds a Tier A hit — an
    incomplete sweep saw a prefix of the app and must not be promoted to an answer,
    and a Tier B lead was never strong enough to answer on its own. `source:
    "scan_floor"` is the marker U7/U9 read as "the Tier A floor stands" (the row's
    status is still FAILED, so the app still routes)."""
    if sweep is None or sweep.incomplete:
        return None
    if not any(located.hit.tier is Tier.A for located in sweep.hits):
        return None
    questions = {
        key: {
            "verdict": (Verdict.YES if key == "credentials_secrets" else Verdict.UNANSWERED).value,
            "reason": (
                _FLOOR_CREDENTIALS_REASON
                if key == "credentials_secrets"
                else _FLOOR_UNANSWERED_REASON
            ),
            "agreed_with_scan": None,
            "downgraded_from_yes": False,
        }
        for key in CLASSIFICATION_KEYS
    }
    verdicts_doc: dict[str, Any] = {
        "source": "scan_floor",
        "questions": questions,
        "scan": {
            "tier_a_hit": True,
            "tier_b_hit": any(located.hit.tier is Tier.B for located in sweep.hits),
            "incomplete": False,
            "tier_a_dispute": False,
        },
    }
    evidence_doc: dict[str, Any] = {
        "questions": {key: [] for key in CLASSIFICATION_KEYS},
        "scan_hits": _scan_hit_refs(sweep),
        "downgraded": [],
    }
    return verdicts_doc, evidence_doc


def _scan_hit_refs(sweep: CredentialSweep) -> list[dict[str, Any]]:
    """The sweep's hits as stored evidence — path, family, tier, line. The hit shape
    structurally carries no value, so neither can this."""
    return [
        {
            "path": located.path,
            "family": located.hit.family,
            "tier": located.hit.tier.value,
            "line": located.hit.line,
        }
        for located in sweep.hits
    ]


def _verdict_summary(verdicts: dict[str, Any]) -> dict[str, str]:
    """The six verdict strings alone — what the P7 audit row carries. Reasons and
    locations stay out of the trail; the row is about WHO ran WHAT and what came back."""
    questions: dict[str, Any] = verdicts["questions"]
    return {key: str(entry["verdict"]) for key, entry in questions.items()}


# --- the process-wide singleton -----------------------------------------------------


def _default_model_factory() -> Model:
    """The Foundry model for a real run, built lazily PER RUN so importing (and
    constructing) the service never requires a configured Foundry. Unconfigured
    Foundry raises here — at run time, after the scan — landing in the review-failed
    bucket with the Tier A floor still applied."""
    from src.config import settings
    from src.services.agent.model import build_foundry_model

    if settings.foundry is None:
        raise ReviewModelUnavailableError(
            "no Foundry deployment is configured, so the classification review cannot run a model"
        )
    return build_foundry_model(settings.foundry)


_service: ClassificationReviewService | None = None


def get_classification_review_service() -> ClassificationReviewService:
    global _service
    if _service is None:
        from src.db.base import async_session_factory

        _service = ClassificationReviewService(
            session_factory=async_session_factory,
            model_factory=_default_model_factory,
        )
    return _service


def set_classification_review_service_for_tests(
    service: ClassificationReviewService | None,
) -> None:
    global _service
    _service = service
