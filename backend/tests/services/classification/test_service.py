"""The review runner, end to end against the real store, real Postgres, and scripted models.

What is under test is the ORDER and the OUTCOMES: the scan runs first and its hits reach
the prompt (never a value), a truncation short-circuits at the model seam instead of
burning the agent's own retries, every failure lands in its taxonomy bucket, a review is
pinned to the class definitions it ran under, the throwaway extraction is gone on every exit
path, the citizen's build budget is untouched, and every terminal run leaves an audit row.

The extract seam is faked (it materializes a real tree and records the `cache_root` it is
handed, for the directory-lifecycle assertions); everything downstream — scan, agent loop,
store writes, usage fold, audit append — is real."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai import models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from src.db.models.audit import AuditLog
from src.db.models.classification_config import ClassificationKind
from src.db.models.classification_review import ClassificationReview, ClassificationReviewStatus
from src.db.models.token_usage import TokenUsage, TokenUsageKind
from src.services.classification import service as service_module
from src.services.classification import store
from src.services.classification.agent import OUTPUT_TOOL_NAME
from src.services.classification.config import LiveClass, LiveConfig
from src.services.classification.service import (
    AUDIT_ACTION,
    FAIL_ABANDONED,
    FAIL_BUNDLE_UNREADABLE,
    FAIL_NO_APP,
    FAIL_REVIEW,
    FAIL_STORAGE,
    FAIL_VERSION_DRIFT,
    ClassificationReviewService,
)
from src.services.storage.bundle import BundleValidationError
from src.services.storage.errors import StorageError
from src.services.storage.snapshot_read import ExtractedSnapshot, NoAppYet
from src.services.usage.gate import (
    DailyTokenLimitExceededError,
    effective_daily_limit,
    enforce_daily_limit,
    record_usage,
)
from tests.factories import AppRegistryFactory, UserFactory

_V1 = "a" * 40
_V2 = "b" * 40

# A value-shaped Tier A line (Stripe live key) whose VALUE must never reach a prompt.
_TIER_A_VALUE = "sk_live_" + "a1b2c3d4e5" * 3
_TIER_A_LINE = f'const stripeKey = "{_TIER_A_VALUE}"\n'
_TIER_B_LINE = 'const password = "hunter2-fixture"\n'

_CLEAN_FILES = {"app/page.tsx": "export default () => <div>VISITOR-LOG</div>\n"}

_CONFIG = LiveConfig(
    threshold=100,
    owners_can_change_answers=True,
    classes=(
        LiveClass(
            key="credentials_keys",
            title="Credentials & keys",
            description="Yes if the code holds a real secret. No: a login form.",
            kind=ClassificationKind.SCORED,
            weight=20,
        ),
        LiveClass(
            key="pii",
            title="PII",
            description="Yes if the app stores identity documents. No: a name field.",
            kind=ClassificationKind.HARD_BLOCK,
            weight=None,
        ),
        LiveClass(
            key="public_data",
            title="Public data",
            description="Yes if the app shows published information. No: a calculator.",
            kind=ClassificationKind.SCORED,
            weight=20,
        ),
    ),
)
_KEYS = tuple(entry.key for entry in _CONFIG.classes)
# The same classes with one more added: different definitions, so a different fingerprint.
_ADDED = LiveConfig(
    threshold=100,
    owners_can_change_answers=True,
    classes=(
        *_CONFIG.classes,
        LiveClass(
            key="ai_usage",
            title="AI usage",
            description="Yes if the app calls an AI model. No: fixed rules.",
            kind=ClassificationKind.SCORED,
            weight=20,
        ),
    ),
)


@pytest.fixture(autouse=True)
def _no_live_model():
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = False
    yield
    models.ALLOW_MODEL_REQUESTS = previous


# ---------------------------------------------------------------------------------------
# Scripted-model helpers (the test_agent.py shapes)
# ---------------------------------------------------------------------------------------


def _answer(key: str, verdict: str = "no", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": key,
        "evidence": [],
        "reason": "Nothing of this kind was found in the app.",
        "verdict": verdict,
    }
    payload.update(overrides)
    return payload


def _output_response(args: dict[str, Any], **response_overrides: Any) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(OUTPUT_TOOL_NAME, args)], **response_overrides)


def _complete(keys: tuple[str, ...] = _KEYS, **per_key: dict[str, Any]) -> ModelResponse:
    return _output_response({"answers": [per_key.get(key, _answer(key)) for key in keys]})


def _truncated() -> ModelResponse:
    # What a real max_tokens stop looks like through pydantic-ai: normalized `length`,
    # the provider's raw reason kept alongside.
    return _output_response(
        {"answers": []},
        finish_reason="length",
        provider_details={"finish_reason": "max_tokens"},
    )


def _scripted(response: ModelResponse) -> FunctionModel:
    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return response

    return FunctionModel(respond)


def _raising(error: Exception) -> FunctionModel:
    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise error

    return FunctionModel(respond)


def _request_text(messages: list[ModelMessage]) -> str:
    out: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                out.append(part.content)
    return "\n".join(out)


# ---------------------------------------------------------------------------------------
# The wired service: real store + Postgres, fake extract seam, queued scripted models
# ---------------------------------------------------------------------------------------


@dataclass
class FakeExtractor:
    """Materializes a real tree into whatever `cache_root` the runner hands over, and
    records that root — the seam the directory-lifecycle assertions read."""

    head_sha: str = _V1
    files: dict[str, str] = field(default_factory=lambda: dict(_CLEAN_FILES))
    error: Exception | None = None
    no_app: bool = False
    cache_roots: list[Path] = field(default_factory=list)

    async def __call__(
        self, app_id: uuid.UUID, *, cache_root: Path | None = None
    ) -> ExtractedSnapshot | NoAppYet:
        assert cache_root is not None, "the runner must pass its own per-run cache root"
        self.cache_roots.append(cache_root)
        if self.error is not None:
            raise self.error
        if self.no_app:
            return NoAppYet(app_id=app_id)
        root = cache_root / app_id.hex / self.head_sha
        root.mkdir(parents=True, exist_ok=True)
        for rel_path, content in self.files.items():
            target = root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return ExtractedSnapshot(app_id=app_id, head_sha=self.head_sha, root=root)


class ModelQueue:
    """One scripted model per expected model run, popped in order — a run that starts
    when none was queued fails loudly, and `calls` is the fourth-claim assertion."""

    def __init__(self) -> None:
        self.models: list[FunctionModel] = []
        self.calls = 0

    def queue(self, *scripted: FunctionModel) -> None:
        self.models.extend(scripted)

    def factory(self) -> FunctionModel:
        self.calls += 1
        if not self.models:
            raise AssertionError("a review run started but no scripted model was queued")
        return self.models.pop(0)


@pytest.fixture
def wire(db_session, monkeypatch):
    extractor = FakeExtractor()
    monkeypatch.setattr(service_module, "extract_snapshot", extractor)

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    queue = ModelQueue()
    return SimpleNamespace(
        service=ClassificationReviewService(
            session_factory=lambda: _session(), model_factory=queue.factory
        ),
        extractor=extractor,
        models=queue,
    )


async def _citizen_app(db):
    user = await UserFactory.create(db)
    app = await AppRegistryFactory.create(db, user_id=user.id)
    return user, app


async def _run_to_settled(wire, db, *, app_id, user_id, head_sha=_V1, config=_CONFIG):
    record = await wire.service.start(
        db, app_id=app_id, user_id=user_id, head_sha=head_sha, config=config
    )
    await wire.service.drain()
    stored = await store.get_for_app(db, app_id=app_id)
    assert stored is not None
    return record, stored


async def _audit_rows(db, *, app_id) -> list[AuditLog]:
    rows = (
        (
            await db.execute(
                sa.select(AuditLog)
                .where(AuditLog.action == AUDIT_ACTION, AuditLog.resource_id == str(app_id))
                # `created_at` is the transaction's start, so one test's rows tie on it.
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _detail(row: AuditLog) -> dict[str, Any]:
    detail = row.detail
    assert detail is not None
    return detail


def _own_root(wire) -> Path:
    assert wire.extractor.cache_roots, "the run never extracted"
    root = wire.extractor.cache_roots[-1]
    assert root.name.startswith("bial-classification-review-")  # its own mkdtemp, never shared
    return root


# ---------------------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------------------


async def test_a_clean_app_lands_a_complete_row_of_nos_stamped_with_its_definitions(
    wire, db_session
) -> None:
    user, app = await _citizen_app(db_session)
    wire.models.queue(_scripted(_complete()))

    record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert record.status is ClassificationReviewStatus.RUNNING  # what `start` handed back
    assert stored.status is ClassificationReviewStatus.COMPLETE
    assert stored.head_sha == _V1
    assert stored.definitions_fingerprint == _CONFIG.fingerprint
    assert stored.answers_complete is True
    assert stored.failure_code is None
    assert stored.verdicts is not None
    answers = stored.verdicts["classes"]
    assert set(answers) == set(_KEYS)
    assert all(set(entry) == {"verdict", "reason"} for entry in answers.values())
    assert all(entry["verdict"] == "no" for entry in answers.values())


async def test_rereading_the_same_version_returns_the_stored_row_without_a_run(
    wire, db_session
) -> None:
    user, app = await _citizen_app(db_session)
    wire.models.queue(_scripted(_complete()))
    await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    again = await wire.service.start(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, config=_CONFIG
    )

    assert again.status is ClassificationReviewStatus.COMPLETE
    assert wire.models.calls == 1  # no second model run
    readout = await wire.service.read(db_session, app_id=app.id)
    assert readout is not None
    assert readout.review.status is ClassificationReviewStatus.COMPLETE
    assert readout.aged_out is False


async def test_the_extraction_directory_is_gone_after_a_successful_run(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    wire.models.queue(_scripted(_complete()))

    await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert not _own_root(wire).exists()


async def test_concurrent_same_commit_consumers_keep_disjoint_roots(
    wire, db_session, tmp_path
) -> None:
    # Another consumer (the read path) holds an extraction of the SAME commit in the
    # shared SHA-keyed cache; the review must neither reuse nor remove it.
    user, app = await _citizen_app(db_session)
    shared = tmp_path / "bial-snapshot-reads"
    consumer_dir = shared / app.id.hex / _V1
    consumer_dir.mkdir(parents=True)
    (consumer_dir / "page.tsx").write_text("the read path is mid-read on this\n")
    wire.models.queue(_scripted(_complete()))

    await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    own = _own_root(wire)
    assert own != shared and not own.is_relative_to(shared)
    assert not own.exists()  # the run's own root is gone
    assert (consumer_dir / "page.tsx").read_text().startswith("the read path")


# ---------------------------------------------------------------------------------------
# The scan feeding the prompt
# ---------------------------------------------------------------------------------------


async def test_the_prompt_carries_hit_location_and_family_and_never_the_value(
    wire, db_session
) -> None:
    user, app = await _citizen_app(db_session)
    wire.extractor.files = {**_CLEAN_FILES, "app/db.ts": "// conn\n" + _TIER_A_LINE}
    captured: dict[str, Any] = {}

    async def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured.setdefault("text", _request_text(messages))
        return _complete()

    wire.models.queue(FunctionModel(capture))

    await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    text = captured["text"]
    assert "app/db.ts" in text
    assert "stripe-live-key" in text
    assert "line 2" in text
    assert _TIER_A_VALUE not in text  # NEVER the value


# ---------------------------------------------------------------------------------------
# Evidence validation and redaction
# ---------------------------------------------------------------------------------------


async def test_a_reason_containing_a_secret_is_redacted_before_storage(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    wire.models.queue(
        _scripted(
            _complete(
                credentials_keys=_answer(
                    "credentials_keys",
                    verdict="yes",
                    evidence=[{"path": "app/page.tsx", "kind": "hardcoded-value"}],
                    reason="The app hardcodes Password=hunter2-the-actual-value in its code.",
                )
            )
        )
    )

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.verdicts is not None
    reason = stored.verdicts["classes"]["credentials_keys"]["reason"]
    assert "hunter2-the-actual-value" not in reason
    assert "***" in reason


async def test_an_answer_set_missing_a_class_fails_after_the_retries(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    wire.models.queue(_scripted(_complete(keys=_KEYS[:-1])))

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.status is ClassificationReviewStatus.FAILED
    assert stored.failure_code == FAIL_REVIEW
    assert stored.verdicts is None  # never stored as an answer set


# ---------------------------------------------------------------------------------------
# Truncation: the tripwire, the one guided retry, and its budget
# ---------------------------------------------------------------------------------------


async def test_one_truncation_is_retried_in_conversation_and_the_retry_does_not_reread(
    wire, db_session
) -> None:
    user, app = await _citizen_app(db_session)
    calls = {"n": 0}
    captured: dict[str, Any] = {}

    async def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            captured["original_prompt"] = _request_text(messages)
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "app/page.tsx"})])
        if calls["n"] == 2:
            return _truncated()
        captured["retry_messages"] = list(messages)
        return _complete()

    wire.models.queue(FunctionModel(script))

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.status is ClassificationReviewStatus.COMPLETE
    assert calls["n"] == 3  # read, truncated output, retried output — nothing more

    retry_messages = captured["retry_messages"]
    # The retained conversation rode along: the run-1 tool exchange is in the retry's
    # input, so the retry did NOT have to re-read the tree...
    tool_returns = [
        part
        for message in retry_messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart) and part.tool_name == "read_file"
    ]
    assert len(tool_returns) == 1
    # ...and the truncated assistant turn itself was dropped, not resent.
    resent_output_calls = [
        part
        for message in retry_messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolCallPart) and part.tool_name == OUTPUT_TOOL_NAME
    ]
    assert resent_output_calls == []
    # The guided nudge DIFFERS from the original ask and constrains the output.
    retry_prompt = _request_text([retry_messages[-1]])
    assert retry_prompt != captured["original_prompt"]
    assert "cut off" in retry_prompt
    assert "one" in retry_prompt and "sentence" in retry_prompt


async def test_a_second_truncation_is_review_failed_with_nothing_salvaged(
    wire, db_session
) -> None:
    user, app = await _citizen_app(db_session)
    calls = {"n": 0}

    async def always_truncates(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        return _truncated()

    wire.models.queue(FunctionModel(always_truncates))

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.status is ClassificationReviewStatus.FAILED
    assert stored.failure_code == FAIL_REVIEW
    assert stored.verdicts is None  # no partial verdicts salvaged from either attempt
    assert stored.failure_detail is not None
    assert "max_tokens" in stored.failure_detail  # the RAW finish reason, for diagnosis
    # THE SHORT-CIRCUIT PIN: exactly two model calls — the truncated one and its one
    # guided retry. The agent's own output-validation retries (retries=2) never re-ran
    # at the same cap; without the model-seam tripwire this would be 3+ calls.
    assert calls["n"] == 2


async def test_a_truncation_with_no_budget_left_is_review_failed_not_a_usage_limit_error(
    wire, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(service_module, "REVIEW_REQUEST_BUDGET", 1)
    user, app = await _citizen_app(db_session)
    calls = {"n": 0}

    async def truncates(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        return _truncated()

    wire.models.queue(FunctionModel(truncates))

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert calls["n"] == 1  # the retry was never attempted — no budget for it
    assert stored.status is ClassificationReviewStatus.FAILED
    assert stored.failure_code == FAIL_REVIEW
    assert stored.failure_detail is not None
    assert "no request budget" in stored.failure_detail
    assert "max_tokens" in stored.failure_detail


async def test_the_request_budget_running_out_mid_run_is_review_failed(
    wire, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(service_module, "REVIEW_REQUEST_BUDGET", 1)
    user, app = await _citizen_app(db_session)

    async def wants_two_requests(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("read_file", {"path": "app/page.tsx"})])

    wire.models.queue(FunctionModel(wants_two_requests))

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.status is ClassificationReviewStatus.FAILED
    assert stored.failure_code == FAIL_REVIEW  # never an empty answer set


# ---------------------------------------------------------------------------------------
# The failure taxonomy — each bucket reachable, each with its own code
# ---------------------------------------------------------------------------------------


async def test_no_bundle_lands_the_no_app_bucket(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    wire.extractor.no_app = True

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.status is ClassificationReviewStatus.FAILED
    assert stored.failure_code == FAIL_NO_APP
    assert wire.models.calls == 0  # the model was never touched


async def test_a_malformed_bundle_lands_the_unreadable_bucket(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    wire.extractor.error = BundleValidationError("not a v2 bundle")

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.failure_code == FAIL_BUNDLE_UNREADABLE


async def test_storage_down_lands_the_storage_bucket(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    wire.extractor.error = StorageError("blob store unreachable")

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.failure_code == FAIL_STORAGE


async def test_a_model_error_lands_the_review_failed_bucket(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    wire.models.queue(_raising(ModelHTTPError(status_code=429, model_name="opus", body="quota")))

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.failure_code == FAIL_REVIEW
    assert stored.failure_detail is not None
    assert "429" in stored.failure_detail  # a quota refusal is a failure, never "no findings"


async def test_a_malformed_output_lands_review_failed_not_an_empty_review(
    wire, db_session
) -> None:
    user, app = await _citizen_app(db_session)
    wire.models.queue(_scripted(_output_response({"answers": "?"})))

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.failure_code == FAIL_REVIEW
    assert stored.verdicts is None


async def test_the_wall_clock_ceiling_lands_the_abandoned_bucket(
    wire, db_session, monkeypatch
) -> None:
    # The ceiling is charged from the row's `started_at`, which Postgres stamps when the test's
    # transaction begins, so it has to outlast setup, claiming and extracting on a loaded
    # machine; a shorter one can elapse before extraction and never reach the model.
    monkeypatch.setattr(service_module, "REVIEW_WALL_CLOCK_CEILING_S", 1.0)
    user, app = await _citizen_app(db_session)

    async def too_slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(30)
        return _complete()

    wire.models.queue(FunctionModel(too_slow))

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.status is ClassificationReviewStatus.FAILED
    assert stored.failure_code == FAIL_ABANDONED
    assert not _own_root(wire).exists()  # the ceiling path still unwinds the extraction


async def test_version_drift_fails_closed_with_its_own_code(wire, db_session) -> None:
    # A save landed between the caller's metadata read and the extraction: the tree is
    # a different commit than the claimed stamp.
    user, app = await _citizen_app(db_session)
    wire.extractor.head_sha = _V2

    _record, stored = await _run_to_settled(
        wire, db_session, app_id=app.id, user_id=user.id, head_sha=_V1
    )

    assert stored.status is ClassificationReviewStatus.FAILED
    assert stored.failure_code == FAIL_VERSION_DRIFT
    assert stored.head_sha == _V1  # stamped with the version it ATTEMPTED
    assert wire.models.calls == 0
    assert not _own_root(wire).exists()


async def test_the_extraction_directory_is_gone_after_a_model_failure(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    wire.models.queue(_raising(ModelHTTPError(status_code=500, model_name="opus", body="boom")))

    await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert not _own_root(wire).exists()


# ---------------------------------------------------------------------------------------
# The ceiling is measured from the ROW, and a restart ages out
# ---------------------------------------------------------------------------------------


async def _rewind_started_at(db, *, app_id, seconds: float) -> store.ReviewRecord:
    await db.execute(
        sa.update(ClassificationReview)
        .where(ClassificationReview.app_id == app_id)
        .values(started_at=datetime.now(UTC) - timedelta(seconds=seconds))
    )
    await db.commit()
    record = await store.get_for_app(db, app_id=app_id)
    assert record is not None
    return record


async def test_the_ceiling_is_measured_from_the_rows_started_at_not_the_run(
    wire, db_session
) -> None:
    # The row was claimed long "ago" (a rewound stamp); a runner picking it up must see
    # the ceiling already spent — measuring from its own start would happily proceed.
    user, app = await _citizen_app(db_session)
    outcome = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_CONFIG.fingerprint
    )
    assert outcome.claimed
    record = await _rewind_started_at(db_session, app_id=app.id, seconds=10_000)
    wire.models.queue(_raising(AssertionError("the model must not be invoked past the ceiling")))

    await wire.service._run(review=record, classes=_CONFIG.classes)

    stored = await store.get_for_app(db_session, app_id=app.id)
    assert stored is not None
    assert stored.failure_code == FAIL_ABANDONED


async def test_a_running_row_past_the_ceiling_reads_as_aged_out(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    outcome = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_CONFIG.fingerprint
    )
    assert outcome.claimed
    await _rewind_started_at(db_session, app_id=app.id, seconds=10_000)

    readout = await wire.service.read(db_session, app_id=app.id)

    assert readout is not None
    assert readout.review.status is ClassificationReviewStatus.RUNNING
    assert readout.aged_out is True


async def test_start_unwedges_an_orphaned_running_row_instead_of_hanging(wire, db_session) -> None:
    # A restart killed the detached task and left the row RUNNING. The next start must
    # age it out (with its own audit row) and claim a fresh attempt — never return the
    # zombie as "still running" forever.
    user, app = await _citizen_app(db_session)
    outcome = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_CONFIG.fingerprint
    )
    assert outcome.claimed
    await _rewind_started_at(db_session, app_id=app.id, seconds=10_000)
    wire.models.queue(_scripted(_complete()))

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.status is ClassificationReviewStatus.COMPLETE
    assert stored.attempt == 2  # the orphan was attempt 1, failed as abandoned
    audits = await _audit_rows(db_session, app_id=app.id)
    assert [_detail(row)["outcome"] for row in audits] == [FAIL_ABANDONED, "complete"]


# ---------------------------------------------------------------------------------------
# The attempt cap and the per-run records
# ---------------------------------------------------------------------------------------


async def test_three_runs_leave_three_audit_rows_but_one_store_row(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    boom = ModelHTTPError(status_code=500, model_name="opus", body="boom")
    wire.models.queue(_raising(boom), _raising(boom), _raising(boom))

    for _ in range(3):
        await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(ClassificationReview)
        .where(ClassificationReview.app_id == app.id)
    )
    assert count == 1
    stored = await store.get_for_app(db_session, app_id=app.id)
    assert stored is not None and stored.attempt == 3
    audits = await _audit_rows(db_session, app_id=app.id)
    assert len(audits) == 3  # the trail counts what the one-row store cannot
    assert all(_detail(row)["outcome"] == FAIL_REVIEW for row in audits)
    assert {_detail(row)["attempt"] for row in audits} == {1, 2, 3}


async def test_the_fourth_claim_returns_the_stored_failure_without_the_model(
    wire, db_session
) -> None:
    user, app = await _citizen_app(db_session)
    boom = ModelHTTPError(status_code=500, model_name="opus", body="boom")
    wire.models.queue(_raising(boom), _raising(boom), _raising(boom))
    for _ in range(3):
        await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    fourth = await wire.service.start(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, config=_CONFIG
    )

    assert fourth.status is ClassificationReviewStatus.FAILED
    assert fourth.attempt == 3
    assert wire.models.calls == 3  # the model was NOT invoked a fourth time
    assert len(await _audit_rows(db_session, app_id=app.id)) == 3  # and no phantom run row


async def test_every_terminal_run_writes_an_app_scoped_audit_row(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    wire.models.queue(_scripted(_complete()))

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    audits = await _audit_rows(db_session, app_id=app.id)
    assert len(audits) == 1
    row = audits[0]
    assert row.actor_id == user.id
    assert row.resource_type == "app"
    assert row.resource_id == str(app.id)  # app-scoped: visible in the admin drawer
    detail = _detail(row)
    assert detail["appId"] == str(app.id)
    assert detail["email"] == user.email  # survives the actor reference nulling
    assert detail["headSha"] == _V1
    assert detail["attempt"] == 1
    assert detail["outcome"] == "complete"
    assert detail["verdicts"] == dict.fromkeys(_KEYS, "no")


async def test_a_failed_run_audits_its_bucket(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    wire.extractor.error = StorageError("down")

    await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    audits = await _audit_rows(db_session, app_id=app.id)
    assert len(audits) == 1
    assert _detail(audits[0])["outcome"] == FAIL_STORAGE
    assert _detail(audits[0])["verdicts"] is None


# ---------------------------------------------------------------------------------------
# The class definitions a review ran under
# ---------------------------------------------------------------------------------------


async def test_three_failures_under_old_definitions_do_not_block_a_review_of_new_ones(
    wire, db_session
) -> None:
    """The attempt cap belongs to the pair. Without the fingerprint in the cap check, adding a
    class would keep returning the old failure and the app could never be reviewed again."""
    user, app = await _citizen_app(db_session)
    boom = ModelHTTPError(status_code=500, model_name="opus", body="boom")
    wire.models.queue(_raising(boom), _raising(boom), _raising(boom))
    for _ in range(3):
        await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)
    wire.models.queue(_scripted(_complete(keys=tuple(sorted(c.key for c in _ADDED.classes)))))

    record, stored = await _run_to_settled(
        wire, db_session, app_id=app.id, user_id=user.id, config=_ADDED
    )

    assert record.attempt == 1
    assert record.status is ClassificationReviewStatus.RUNNING
    assert wire.models.calls == 4
    assert stored.status is ClassificationReviewStatus.COMPLETE
    assert stored.definitions_fingerprint == _ADDED.fingerprint
    assert stored.verdicts is not None
    assert "ai_usage" in stored.verdicts["classes"]


async def test_a_complete_review_under_old_definitions_is_reviewed_again(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    wire.models.queue(_scripted(_complete()))
    await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)
    captured: dict[str, Any] = {}

    async def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["instructions"] = info.instructions
        return _complete(keys=tuple(sorted(c.key for c in _ADDED.classes)))

    wire.models.queue(FunctionModel(capture))

    record, stored = await _run_to_settled(
        wire, db_session, app_id=app.id, user_id=user.id, config=_ADDED
    )

    assert record.status is ClassificationReviewStatus.RUNNING
    assert record.attempt == 1
    assert wire.models.calls == 2
    assert stored.definitions_fingerprint == _ADDED.fingerprint
    assert '<class key="ai_usage">' in captured["instructions"]


async def test_a_weight_kind_or_policy_edit_keeps_the_stored_review_current(
    wire, db_session
) -> None:
    user, app = await _citizen_app(db_session)
    wire.models.queue(_scripted(_complete()))
    await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)
    pii, credentials, public = _CONFIG.classes
    edited = LiveConfig(
        threshold=40,
        owners_can_change_answers=False,
        classes=(
            LiveClass(
                key=credentials.key,
                title=credentials.title,
                description=credentials.description,
                kind=ClassificationKind.HARD_BLOCK,
                weight=None,
            ),
            pii,
            LiveClass(
                key=public.key,
                title=public.title,
                description=public.description,
                kind=public.kind,
                weight=80,
            ),
        ),
    )

    again = await wire.service.start(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, config=edited
    )

    assert edited.fingerprint == _CONFIG.fingerprint
    assert again.status is ClassificationReviewStatus.COMPLETE
    assert wire.models.calls == 1


# ---------------------------------------------------------------------------------------
# A newer version taking over mid-run
# ---------------------------------------------------------------------------------------


async def test_a_newer_start_supersedes_and_the_old_runs_completion_writes_nothing(
    wire, db_session
) -> None:
    user, app = await _citizen_app(db_session)
    gate = asyncio.Event()

    async def blocked_v1(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await gate.wait()
        return _complete(
            credentials_keys=_answer(
                "credentials_keys",
                verdict="yes",
                evidence=[{"path": "app/page.tsx", "kind": "hardcoded-value"}],
                reason="A stale verdict that must never dress the new claim.",
            )
        )

    wire.models.queue(FunctionModel(blocked_v1))
    await wire.service.start(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, config=_CONFIG
    )
    task_v1 = next(iter(wire.service._tasks))
    await asyncio.sleep(0.05)  # let run 1 reach the model and block

    wire.extractor.head_sha = _V2
    wire.models.queue(_scripted(_complete()))
    await wire.service.start(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V2, config=_CONFIG
    )
    task_v2 = next(task for task in wire.service._tasks if task is not task_v1)
    await task_v2  # run 2 settles first (sequential sessions — the test's one session)

    gate.set()
    await wire.service.drain()

    count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(ClassificationReview)
        .where(ClassificationReview.app_id == app.id)
    )
    assert count == 1  # exactly one row survives
    stored = await store.get_for_app(db_session, app_id=app.id)
    assert stored is not None
    assert stored.head_sha == _V2
    assert stored.status is ClassificationReviewStatus.COMPLETE
    assert stored.verdicts is not None
    assert stored.verdicts["classes"]["credentials_keys"]["verdict"] == "no"  # run 2's
    audits = await _audit_rows(db_session, app_id=app.id)
    assert len(audits) == 2  # both RUNS are on the trail
    superseded = [row for row in audits if _detail(row).get("superseded")]
    assert len(superseded) == 1
    assert _detail(superseded[0])["headSha"] == _V1


# ---------------------------------------------------------------------------------------
# The spend: raw four-class usage on the review kind, never the citizen's budget
# ---------------------------------------------------------------------------------------


async def test_usage_is_recorded_raw_across_all_four_classes(wire, db_session) -> None:
    # The double-count pin: pydantic-ai's input_tokens already INCLUDES the cache
    # classes; the persisted row must carry the four RAW values, cache NOT re-added.
    user, app = await _citizen_app(db_session)
    response = _complete()
    response.usage = RequestUsage(
        input_tokens=1_000,  # grand total, 500 of which are the cache classes below
        output_tokens=80,
        cache_read_tokens=300,
        cache_write_tokens=200,
    )
    wire.models.queue(_scripted(response))

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    usage_row = (
        await db_session.execute(
            sa.select(TokenUsage).where(
                TokenUsage.user_id == user.id, TokenUsage.kind == TokenUsageKind.REVIEW
            )
        )
    ).scalar_one()
    assert usage_row.input_tokens == 1_000  # NOT 1_500
    assert usage_row.output_tokens == 80
    assert usage_row.cache_read_tokens == 300
    assert usage_row.cache_write_tokens == 200
    # The same four raw classes land on the review row itself.
    assert stored.input_tokens == 1_000
    assert stored.output_tokens == 80
    assert stored.cache_read_tokens == 300
    assert stored.cache_write_tokens == 200


async def test_usage_is_recorded_even_when_the_run_fails(wire, db_session) -> None:
    user, app = await _citizen_app(db_session)
    calls = {"n": 0}

    async def one_read_then_boom(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            response = ModelResponse(parts=[ToolCallPart("read_file", {"path": "app/page.tsx"})])
            response.usage = RequestUsage(input_tokens=400, output_tokens=20)
            return response
        raise ModelHTTPError(status_code=500, model_name="opus", body="boom")

    wire.models.queue(FunctionModel(one_read_then_boom))

    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.status is ClassificationReviewStatus.FAILED
    usage_row = (
        await db_session.execute(
            sa.select(TokenUsage).where(
                TokenUsage.user_id == user.id, TokenUsage.kind == TokenUsageKind.REVIEW
            )
        )
    ).scalar_one()
    assert usage_row.input_tokens == 400  # what the run spent learning nothing is still real
    assert stored.input_tokens == 400


async def test_a_review_leaves_the_citizens_build_budget_untouched(wire, db_session) -> None:
    # Integration with the REAL gate: a citizen one token under their cap runs a heavy
    # review and can still start a build immediately afterwards.
    user, app = await _citizen_app(db_session)
    limit = await effective_daily_limit(db_session, user.id)
    await record_usage(db_session, user.id, input_tokens=limit - 1, output_tokens=0)
    await db_session.commit()
    await enforce_daily_limit(db_session, user.id)  # just under the cap — allowed

    response = _complete()
    response.usage = RequestUsage(input_tokens=500_000, output_tokens=4_000)
    wire.models.queue(_scripted(response))
    await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    # The heavy review spend changed NOTHING the gate reads.
    await enforce_daily_limit(db_session, user.id)  # still allowed — would raise otherwise
    build_row = (
        await db_session.execute(
            sa.select(TokenUsage).where(
                TokenUsage.user_id == user.id, TokenUsage.kind == TokenUsageKind.BUILD
            )
        )
    ).scalar_one()
    assert build_row.input_tokens == limit - 1  # the build dimension is exactly where it was


async def test_a_citizen_over_their_cap_is_not_blocked_from_a_review(wire, db_session) -> None:
    # The other half of the carve-out: the review never consults the daily gate, so a
    # heavy build day cannot make an app unpublishable.
    user, app = await _citizen_app(db_session)
    limit = await effective_daily_limit(db_session, user.id)
    await record_usage(db_session, user.id, input_tokens=limit + 100, output_tokens=0)
    await db_session.commit()
    with pytest.raises(DailyTokenLimitExceededError):
        await enforce_daily_limit(db_session, user.id)  # builds ARE refused...

    wire.models.queue(_scripted(_complete()))
    _record, stored = await _run_to_settled(wire, db_session, app_id=app.id, user_id=user.id)

    assert stored.status is ClassificationReviewStatus.COMPLETE  # ...the review is not
