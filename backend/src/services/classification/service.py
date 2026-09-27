"""The classification review runner: start a review for a version, land exactly one
result, and turn every way it can fail into a state the rest of the system can act on.
START is synchronous (cap check, claim, detach); RUN is a detached task that never
raises and outlives the request. A restart still strands the row RUNNING — that is what
`_aged_out` and the `FAIL_ABANDONED` settle exist to recover, not a case that cannot happen.

WHY THIS EXISTS
The version is always the CALLER's to resolve: the commit and the live configuration it hands
to `start`. The run reviews against exactly the classes it was claimed under, and fails closed on
commit drift rather than trusting a second, possibly stale, read of its own. Review spend is
metered but deliberately excluded from the citizen's daily token gate (`kind=REVIEW`;
`enforce_daily_limit` is never called here) — a heavy build day must not block
publishing, nor must opening the publish dialog spend budget the citizen never chose
to spend, though the spend is still recorded per-citizen.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from collections.abc import Callable, Coroutine
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

from src.core.redaction import redact_and_cap, redact_secrets
from src.db.models.classification_review import ClassificationReviewStatus
from src.db.models.token_usage import TokenUsageKind
from src.db.models.user import User
from src.services.audit.log import append_audit
from src.services.classification import store
from src.services.classification.agent import run_review
from src.services.classification.config import LiveClass, LiveConfig
from src.services.classification.constants import (
    REVIEW_REQUEST_BUDGET,
    REVIEW_WALL_CLOCK_CEILING_S,
)
from src.services.classification.scan import CredentialSweep, scan_snapshot
from src.services.classification.schema import ReviewOutput
from src.services.classification.store import ReviewRecord, is_for
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

# Failure buckets plus the drift code. Stable and greppable: the routes map each to its
# own citizen-facing sentence, and an operator alerts on the string.
FAIL_NO_APP: Final = "no_app_yet"
"""Nothing saved to check yet (`NoAppYet`) — publishing already refuses this state."""
FAIL_BUNDLE_UNREADABLE: Final = "bundle_unreadable"
"""The saved bundle exists but could not be read or extracted — retrying cannot succeed."""
FAIL_STORAGE: Final = "storage_unavailable"
"""Object storage is down or unconfigured — publishing itself is equally unavailable."""
FAIL_REVIEW: Final = "review_failed"
"""The model never produced a usable answer: model/API error, output that never answered exactly
its classes Yes or No, a double truncation, a quota refusal, or an internal crash."""
FAIL_ABANDONED: Final = "review_abandoned"
"""Over the wall-clock ceiling (measured from the ROW's `started_at`), or a row a
restart orphaned that aged out."""
FAIL_VERSION_DRIFT: Final = "version_drift"
"""The extracted tree was a different commit than the claimed stamp — a save landed
between the caller's metadata read and the extraction. Failed closed; the citizen's next
open claims a fresh review for the real version."""

MAX_MODEL_RUNS_PER_VERSION: Final = 3
"""The attempt cap that makes the token-gate carve-out honest: at most three model runs per
(commit, class-definition fingerprint) pair, counted on the review row. A fourth start returns
the stored failure without touching the model, and the app routes to an administrator either
way."""

AUDIT_ACTION: Final = "classification_review"
"""The audit action. App-scoped (`resource_type="app"`, `resource_id=str(app_id)`) with
the app id ALSO repeated in detail, so the admin app drawer's resource-or-detail match
finds the row either way."""

_DETAIL_MAX_CHARS: Final = 2_000

# The guided-retry nudge (user-role). It must DIFFER from the original ask and CONSTRAIN
# the output — an identical re-ask truncates identically one step later, at twice the
# price. The conversation it lands in already carries the instructions, the scan's hits
# and every tool exchange, so nothing is repeated here.
_TRUNCATION_NUDGE: Final = (
    "Your previous answer was cut off at the output token limit and has been discarded. "
    "Record the complete review again, and keep it short: at most one sentence per "
    "reason, and only the single strongest evidence location per class."
)


class _TruncatedAtTheCapError(Exception):
    """The model stopped at the output token cap (`finish_reason == "length"`), raised
    by `_MeteredModel` INSIDE the model seam so the truncated response never reaches
    output validation and the agent's own retries never re-run at the same cap.

    Carries the raw finish reason (for diagnosing a cap overshoot vs. an endpoint
    problem) and the conversation UP TO the truncated turn — exactly what the guided
    retry must resend."""

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
    prose lives in the route (keyed on the code), not here."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class _RunScratch:
    """The meter, however far the run got: usage is recorded whether it succeeded or not."""

    metered: _MeteredModel | None = None


@dataclass(frozen=True)
class ReviewReadout:
    """The read verb's answer: the stored row plus the one derivation the store cannot
    make — whether a RUNNING row has aged out past the wall-clock ceiling. A restart
    kills the detached task but leaves the row RUNNING; readers must render an
    aged-out row as the review-abandoned state, never as still-in-flight, and `start`
    un-wedges it on the next request."""

    review: ReviewRecord
    aged_out: bool


class ReviewModelUnavailableError(RuntimeError):
    """Foundry is not configured, so no review model can be built. Raised from the
    model factory at RUN time, so the failure lands in the review-failed bucket."""


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


class ClassificationReviewService:
    """Owns the in-flight review tasks. One process-wide instance (the routes wire in
    the singleton below; tests build their own with a scripted model)."""

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
        #
        # A SUPERSEDED RUN IS DELIBERATELY NOT CANCELLED. Keying this by app and cancelling
        # the previous task looks like the obvious tidy-up, but the superseded run still
        # has a job to do: it writes its OWN audit row marked `superseded` on the way out,
        # and the trail counts RUNS, not rows — the store keeps one row per app and
        # overwrites it, so that trail is the only place a re-run is recorded. Cancelling
        # trades a recorded run for a silent one. Its WRITE is already harmless: the
        # store's compare-and-swap settles only a run's own claim (id + running + commit +
        # fingerprint + attempt), and every phase is inside the wall-clock ceiling, so a
        # superseded run cannot outlive it either.
        self._tasks: set[asyncio.Task[None]] = set()

    # --- the start verb ---------------------------------------------------------

    async def start(
        self,
        db: AsyncSession,
        *,
        app_id: uuid.UUID,
        user_id: uuid.UUID,
        head_sha: str,
        config: LiveConfig,
    ) -> ReviewRecord:
        """Ensure a review exists for this app at `head_sha` under `config`'s class definitions
        and return its row: the stored answer when that pair is unchanged, the stored failure
        when the pair's attempt cap is spent, or a fresh RUNNING row with the run detached. The
        run reviews against exactly `config.classes`, never a later read."""
        fingerprint = config.fingerprint
        stored = await store.get_for_app(db, app_id=app_id)
        if stored is not None and is_for(stored, head_sha=head_sha, fingerprint=fingerprint):
            if _aged_out(stored):
                # A restart orphaned this run: the task died, the row hung RUNNING.
                # Settle it as abandoned so it can be re-claimed — a restart must age
                # out, not wedge the app's review forever.
                settled = await store.fail(
                    db,
                    review_id=stored.review_id,
                    head_sha=stored.head_sha,
                    fingerprint=fingerprint,
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
                and is_for(stored, head_sha=head_sha, fingerprint=fingerprint)
                and stored.status is ClassificationReviewStatus.FAILED
                and stored.attempt >= MAX_MODEL_RUNS_PER_VERSION
            ):
                # The fourth claim: the cap is the review's real spend bound. Return
                # the stored failure WITHOUT claiming or touching the model — the app
                # routes to an administrator either way.
                return stored

        outcome = await store.claim(
            db, app_id=app_id, user_id=user_id, head_sha=head_sha, fingerprint=fingerprint
        )
        if not outcome.claimed:
            return outcome.review

        task = asyncio.create_task(self._run(review=outcome.review, classes=config.classes))
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

    async def _run(self, *, review: ReviewRecord, classes: tuple[LiveClass, ...]) -> None:
        """The detached run. NEVER raises: an escaping exception would leave the row
        RUNNING until it ages out, with the citizen staring at a spinner the whole
        ceiling long."""
        scratch = _RunScratch()
        try:
            verdicts, evidence = await self._review(
                review=review, classes=classes, scratch=scratch
            )
        except _ReviewFailedError as failure:
            await self._settle(self._settle_failed(review, failure=failure, scratch=scratch))
        except asyncio.CancelledError:
            # Shutdown. The extraction was already unwound by `_review`'s finally; the
            # row is left RUNNING and ages out, which `start` and `read` both handle.
            raise
        except Exception as exc:
            _log.exception("classification_review_crashed", review_id=str(review.review_id))
            await self._settle(
                self._settle_failed(
                    review,
                    failure=_ReviewFailedError(FAIL_REVIEW, type(exc).__name__),
                    scratch=scratch,
                )
            )
        else:
            await self._settle(
                self._settle_complete(
                    review, verdicts=verdicts, evidence=evidence, scratch=scratch
                )
            )

    async def _settle(self, write: Coroutine[Any, Any, None]) -> None:
        """The terminal write, guarded so `_run`'s "NEVER raises" is true on every exit.

        A transient Postgres error here (dropped connection, timeout, deadlock) would otherwise
        escape the detached task on the SUCCESS path, which nothing awaits: the review
        succeeded but the row never learns it. Swallowing is the lesser harm (the row ages out
        and `start` re-claims it) but is logged loudly. `CancelledError` is NOT caught:
        shutdown must keep propagating."""
        try:
            await write
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("classification_review_settle_failed")

    async def _review(
        self,
        *,
        review: ReviewRecord,
        classes: tuple[LiveClass, ...],
        scratch: _RunScratch,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Extraction ownership, and nothing else. The run extracts into a throwaway root of
        its own and removes it in the `finally` — unconditionally: success, every failure
        bucket, the wall-clock ceiling, and cancellation."""
        # Never the shared SHA-keyed cache: verdicts live in a row, so reuse buys nothing,
        # and a private root can never delete a directory another request is mid-read on.
        own_root = await asyncio.to_thread(_make_throwaway_root)
        try:
            extracted = await self._bounded(
                review,
                self._extract(review.app_id, cache_root=own_root),
                phase="the snapshot extraction",
            )
            return await self._examine(
                review=review, extracted=extracted, classes=classes, scratch=scratch
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, own_root, ignore_errors=True)

    async def _examine(
        self,
        *,
        review: ReviewRecord,
        extracted: ExtractedSnapshot,
        classes: tuple[LiveClass, ...],
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

        # The scan FIRST: model-free, fast, and its hits become the prompt's directed
        # evidence while the credentials class is active.
        sweep = await self._bounded(
            review, scan_snapshot(extracted.root), phase="the credential scan"
        )

        metered = _MeteredModel(self._model_factory())
        scratch.metered = metered

        result = await self._call_model(
            metered,
            review=review,
            snapshot_root=extracted.root,
            classes=classes,
            sweep=sweep,
        )
        return _build_record(result.output, sweep=sweep)

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

    async def _bounded[T](
        self, review: ReviewRecord, work: Coroutine[Any, Any, T], *, phase: str
    ) -> T:
        """One PRE-MODEL phase under the same wall-clock ceiling the model call uses.

        The ceiling is measured from the row's `started_at` and every phase is charged
        against it, but only the model call was ever BOUNDED by it. Extraction pulls the
        saved bundle out of object storage and the sweep walks the whole tree, so a hung
        storage read ran past the ceiling that was supposed to end it, with the citizen
        watching a spinner the row had already given up on. The bound belongs on every
        phase that waits, not only the last one."""
        remaining = _seconds_left(review)
        if remaining <= 0:
            raise _ReviewFailedError(
                FAIL_ABANDONED, f"the wall-clock ceiling elapsed before {phase}"
            )
        try:
            async with asyncio.timeout(remaining):
                return await work
        except TimeoutError:
            raise _ReviewFailedError(
                FAIL_ABANDONED,
                f"over the {REVIEW_WALL_CLOCK_CEILING_S:.0f}s wall-clock ceiling during {phase}",
            ) from None

    async def _call_model(
        self,
        metered: _MeteredModel,
        *,
        review: ReviewRecord,
        snapshot_root: Path,
        classes: tuple[LiveClass, ...],
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
                    metered,
                    review=review,
                    snapshot_root=snapshot_root,
                    classes=classes,
                    sweep=sweep,
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
            # Output that never answered exactly its classes past the agent's retries, a quota
            # refusal, any provider error — one bucket, distinguished by the stored detail.
            raise _ReviewFailedError(FAIL_REVIEW, str(exc)) from exc

    async def _run_with_truncation_retry(
        self,
        metered: _MeteredModel,
        *,
        review: ReviewRecord,
        snapshot_root: Path,
        classes: tuple[LiveClass, ...],
        sweep: CredentialSweep,
    ) -> AgentRunResult[ReviewOutput]:
        try:
            return await run_review(
                model=metered,
                user_id=review.user_id,
                snapshot_root=snapshot_root,
                classes=classes,
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
                    classes=classes,
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
                fingerprint=_claimed_fingerprint(review),
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
        async with self._session_factory() as db:
            settled = await store.fail(
                db,
                review_id=review.review_id,
                head_sha=review.head_sha,
                fingerprint=_claimed_fingerprint(review),
                attempt=review.attempt,
                code=failure.code,
                detail=redact_and_cap(failure.detail, _DETAIL_MAX_CHARS),
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
            verdict_summary=None,
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
        """The per-run records: the citizen's spend (raw, `review` kind) and the audit
        row. Best-effort by design — the review row is the record of truth, and a
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
        """One audit row in the caller's transaction (the caller commits). App-scoped, and
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


def _claimed_fingerprint(review: ReviewRecord) -> str:
    """The fingerprint a claim stamped; every claim writes one, so a run never holds a row
    without it."""
    if review.definitions_fingerprint is None:
        raise RuntimeError(f"review {review.review_id} was claimed without a fingerprint")
    return review.definitions_fingerprint


def _build_record(
    output: ReviewOutput, *, sweep: CredentialSweep
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The completed output → the two stored documents: `verdicts` (owner- and admin-safe:
    each class's verdict and its REDACTED reason) and `evidence` (internal: cited locations and
    the scan's located hits, never rendered to a person). Every reason passes the shared
    redactor, the deterministic backstop behind the prompt's own plain-language ask."""
    verdicts_doc: dict[str, Any] = {
        "classes": {
            answer.key: {"verdict": answer.verdict.value, "reason": redact_secrets(answer.reason)}
            for answer in output.answers
        }
    }
    evidence_doc: dict[str, Any] = {
        "classes": {
            answer.key: [{"path": ref.path, "kind": ref.kind} for ref in answer.evidence]
            for answer in output.answers
        },
        "scan_hits": _scan_hit_refs(sweep),
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
    """The verdict strings alone — what the audit row carries. Reasons and locations stay out
    of the trail; the row is about WHO ran WHAT and what came back."""
    answers: dict[str, Any] = verdicts["classes"]
    return {key: str(entry["verdict"]) for key, entry in answers.items()}


# --- the process-wide singleton -----------------------------------------------------


def _default_model_factory() -> Model:
    """The Foundry model for a real run, built lazily PER RUN so importing (and
    constructing) the service never requires a configured Foundry. Unconfigured
    Foundry raises here, at run time, landing in the review-failed bucket."""
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
