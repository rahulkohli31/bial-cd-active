"""Evaluate the classification review's budgets and its misses (U14).

WHAT THIS MEASURES. The review ships with PROVISIONAL ceilings — the wall-clock ceiling,
the request budget and the 8,000-token final-step output cap in
`src/services/classification/constants.py` — sized from nine ad-hoc runs that measured
cost and said nothing about accuracy. This script runs the real scan-first review loop
over a chosen corpus of saved app bundles and reports, per run: wall-clock, model
requests, tool calls, the four raw token classes, THE FINAL STEP'S OUTPUT-TOKEN COUNT
SEPARATELY (the 8,000 cap is later re-set from that distribution), the six verdicts,
catch/miss per seeded finding, and the Foundry deployment the run used — ceilings belong
to the deployment they were measured on and do not transfer (ASM17 / U14).

It also measures the model-free credential scan against a labeled corpus: Tier A and
Tier B precision/recall SEPARATELY (they carry different weight — Tier A stands in when
the model is down; Tier B is only a lead), and it prints the Tier A precision gate:
Tier A must reach 100% precision on the corpus, because a Tier A false positive becomes
a verdict nobody reviewed. Narrowing Tier A if the gate fails is a later design
decision, not this script's.

Two named summary figures, per the plan:
  * the FALSE-POSITIVE ROUTING RATE — known-clean bundles that would route: any
    weighted-Yes verdict OR any run failure (ASM17's adoption half); and
  * the MISS RATE — seeded findings a completed review did not answer Yes on.

WHY THE AGENT LAYER, NOT THE SERVICE. The script drives `scan_snapshot` +
`agent.run_review` directly over extracted trees, deliberately NOT
`ClassificationReviewService`:
  * the service's provisional ceilings would CENSOR the very distributions this eval
    exists to measure — a ceiling cannot be set from data it already clipped, so runs
    here go out under the script's own generous, flag-settable bounds;
  * runs need no database rows and no running control-plane — local bundles evaluate
    anywhere, and app-id pulls need only the backend env for object storage;
  * every report field is observable at this layer (a metering wrapper around the model
    records requests, tool calls and per-step usage; the structured output carries the
    verdicts).
What is NOT replicated from the service: the guided truncation retry, the Tier A
failure floor, and row storage — those are service behaviour pinned by its own tests.
Here a truncation or model error is a FAILURE ROW (never an abort of the sweep), which
the routing rate counts exactly as the publish ladder would route it. The one service
rule that IS applied is R4's evidence downgrade — a Yes whose every cited location does
not exist becomes unanswered — imported from the service itself so the eval judges
catch/miss on the verdicts production would actually store, not on raw model output
that production would discard.

ENVIRONMENT. Run from `backend/` with the backend env loaded (`ENV_FILE=.env` or
exported variables) — the platform modules the sweep imports resolve the full Settings,
so every sweep needs it (only `--help` and argument errors are env-free). Model runs
additionally need the `FOUNDRY__*` block; `--scan-only` does not touch the model and
runs without Foundry access.

INVOCATION (both forms work):
    uv run python scripts/eval_classification_review.py ...
    uv run python -m scripts.eval_classification_review ...

    # The real measurement run — object-storage bundles, live Foundry:
    ENV_FILE=.env uv run python scripts/eval_classification_review.py \\
        --app-id 01890a5d-... --app-id 01890a5e-... \\
        --seeded ~/bial-eval/seeded.json \\
        --known-clean ~/bial-eval/known-clean.json \\
        --scan-labels ~/bial-eval/scan-labels.json \\
        --out ~/bial-eval/report.jsonl

    # A local sweep over downloaded / fixture bundles:
    ENV_FILE=.env uv run python scripts/eval_classification_review.py \\
        --bundle-dir ~/bial-eval/bundles --out ~/bial-eval/report.jsonl

    # Scan-only corpus measurement (no model, no Foundry, no spend — the 20-30
    # bundle Tier A/B precision-recall sweep):
    ENV_FILE=.env uv run python scripts/eval_classification_review.py \\
        --bundle-dir ~/bial-eval/bundles --scan-labels labels.json \\
        --scan-only --out scan-report.jsonl

MANIFESTS (all optional; every id they name must match a discovered bundle — a typo'd
id would silently measure nothing, so it errors instead). A bundle's id is its file
stem for `--bundle`/`--bundle-dir` sources and the app UUID string for `--app-id`.
  * `--seeded` — which bundle is seeded with which finding category:
        {"seeded-health-app": ["health_data"], "seeded-creds": ["credentials_secrets"]}
  * `--known-clean` — bundles known to hold nothing weighted:
        ["visitor-log", "meeting-rooms"]
  * `--scan-labels` — the labeled scan corpus: per bundle, the paths holding GENUINE
    hardcoded secrets and the paths holding credential-SHAPED non-secrets (login
    forms — the false-positive population that matters):
        {"crm-app": {"secrets": ["app/lib/db.ts"],
                     "credential_shaped": ["app/login/page.tsx"]}}

OUTPUT. `--out` gets JSON Lines: one `row_type: "run"` object per bundle (a bundle that
fails to extract is a failure ROW, never an abort of the sweep) and one final
`row_type: "summary"` object carrying the named figures. The human summary prints to
stdout. THE REPORT IS AN OPERATOR ARTIFACT: rows carry file paths from citizen-built
apps (never values — the scan's hit shape structurally cannot carry one), so like the
exception register's workbooks it lives in a working directory OUTSIDE the repo tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Executed by PATH (`python scripts/eval_...py`) sys.path[0] is `scripts/`, not the
# backend root, so `src` would not resolve; `-m` mode and the test import need nothing.
# The shim makes both documented invocations work (tests/conftest.py's noqa pattern).
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import statistics  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Callable, Sequence  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from typing import TYPE_CHECKING, Any, Final, Literal, TypedDict  # noqa: E402

if TYPE_CHECKING:  # env-poisoned import chain — type-only here, runtime import is lazy
    from src.services.classification.scan import CredentialSweep

from pydantic_ai.exceptions import (  # noqa: E402
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart  # noqa: E402
from pydantic_ai.models import Model, ModelRequestParameters  # noqa: E402
from pydantic_ai.models.wrapper import WrapperModel  # noqa: E402
from pydantic_ai.settings import ModelSettings  # noqa: E402
from pydantic_ai.usage import UsageLimits  # noqa: E402

# The classification agent/scan/service modules are DELIBERATELY NOT imported here:
# their import chain reaches `src.services.agent` (package init) and the DB engine,
# which resolve the full Settings at import — so importing them at module level would
# make even `--help` demand a configured environment. They are imported inside the
# functions that run the sweep (`_evaluate_one`, `_apply_evidence_rule`); everything
# imported below is verified env-free.
from src.core.redaction import Tier, redact_and_cap  # noqa: E402
from src.services.classification.schema import Completeness, ReviewOutput  # noqa: E402
from src.services.deploy.classification import (  # noqa: E402
    CLASSIFICATION_KEYS,
    DATA_CLASSIFICATION_QUESTIONS,
)
from src.services.storage.bundle import (  # noqa: E402
    BundleValidationError,
    parse_bundle_head_sha,
)
from src.services.storage.errors import StorageError  # noqa: E402
from src.services.storage.snapshot_read import (  # noqa: E402
    NoAppYet,
    SnapshotExtractionError,
    extract_snapshot,
)

ModelFactory = Callable[[], Model]

#: The categories whose Yes routes an app — Public Data carries weight 0 and never
#: routes anything, so the routing-rate arithmetic must not count it.
WEIGHTED_KEYS: Final[tuple[str, ...]] = tuple(
    key for key, _label, weight in DATA_CLASSIFICATION_QUESTIONS if weight
)

#: Bound on one local `git clone` from a bundle (mirrors the snapshot reader's bound —
#: a HEAD-only bundle extracts in well under this; a hang is a wedged git).
_CLONE_TIMEOUT_S: Final = 60.0

# Failure kinds. `extract_failed` is the plan's named edge (a bundle that fails to
# extract is a report ROW); the model-phase kinds mirror the service's taxonomy in
# spirit, but the mapping to citizen-facing buckets stays the service's own business.
_EXTRACT_FAILED: Final = "extract_failed"
_NO_APP_YET: Final = "no_app_yet"
_STORAGE_UNAVAILABLE: Final = "storage_unavailable"
_OUTPUT_TRUNCATED: Final = "output_truncated"
_REQUEST_LIMIT: Final = "request_limit_exhausted"
_RUN_TIMEOUT: Final = "run_timeout"
_MODEL_ERROR: Final = "model_error"
_PARTIAL_REVIEW: Final = "partial_review"

#: How much of a failure detail is worth keeping in the report, matching the runner's own
#: ceiling. Its own constant rather than an import of the service's private one: this
#: script deliberately keeps the classification modules out of its import chain (see the
#: module docstring), and how much diagnostic to keep is each pipeline's to decide.
_DETAIL_MAX_CHARS: Final = 2_000


class SpecError(Exception):
    """The sample spec (arguments + manifests) is unusable. `main` turns this into the
    parser's usage-and-exit — never a traceback, never a partial sweep over a spec with
    a typo in it."""


class _EvalRunFailedError(Exception):
    """One bundle's run failed. Carries the report row's failure kind and detail; the
    sweep records the row and moves on."""

    def __init__(self, kind: str, detail: str | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


class _TruncatedError(Exception):
    """The model stopped at the output token cap (`finish_reason == "length"`). Raised
    from inside the model seam — the service's tripwire, minus its guided retry: for
    the eval a truncation is a failure row, and the at-cap output-token count it leaves
    in the recorder is itself a data point for the cap distribution."""

    def __init__(self, raw_finish_reason: str) -> None:
        super().__init__(f"model output truncated (finish_reason={raw_finish_reason!r})")
        self.raw_finish_reason = raw_finish_reason


class _FlightRecorder(WrapperModel):
    """The run's black box: it survives whatever happens to the flight. Counts model
    requests and non-output tool calls, accumulates the four RAW token classes (raw
    means raw — pydantic-ai's `input_tokens` already includes the cache classes, and
    re-adding them is the documented double-count regression), keeps the LAST step's
    output-token count separately, and trips on truncation."""

    def __init__(self, wrapped: Model, *, output_tool_name: str) -> None:
        super().__init__(wrapped)
        self._output_tool_name = output_tool_name
        self.requests = 0
        self.tool_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.final_step_output_tokens = 0

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
        # Overwritten every step: after the run, this holds the FINAL step's count —
        # the one the 8,000 cap binds on (only the structured output step is large).
        self.final_step_output_tokens = usage.output_tokens
        self.tool_calls += sum(
            1
            for part in response.parts
            if isinstance(part, ToolCallPart) and part.tool_name != self._output_tool_name
        )
        if response.finish_reason == "length":
            details = response.provider_details or {}
            raise _TruncatedError(raw_finish_reason=str(details.get("finish_reason", "length")))
        return response


# ---------------------------------------------------------------------------------------
# The sample spec
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Source:
    """One bundle to evaluate: a local `.bundle` file, or an app id to pull from
    object storage."""

    bundle_id: str
    kind: Literal["local", "storage"]
    path: Path | None = None
    app_id: uuid.UUID | None = None

    @property
    def origin(self) -> str:
        return str(self.path) if self.kind == "local" else str(self.app_id)


@dataclass(frozen=True)
class _ScanLabels:
    """One labeled bundle: paths holding genuine secrets, and paths holding
    credential-shaped non-secrets (the login-form population)."""

    secrets: frozenset[str]
    credential_shaped: frozenset[str]


@dataclass(frozen=True)
class _EvalSpec:
    sources: tuple[_Source, ...]
    seeded: dict[str, tuple[str, ...]]
    known_clean: frozenset[str]
    scan_labels: dict[str, _ScanLabels]


def _load_json(path: Path, what: str) -> Any:
    if not path.is_file():
        raise SpecError(f"{what} manifest not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecError(f"{what} manifest {path} is not readable JSON: {exc}") from exc


def _load_seeded(path: Path) -> dict[str, tuple[str, ...]]:
    raw = _load_json(path, "--seeded")
    if not isinstance(raw, dict):
        raise SpecError(f"--seeded {path}: expected an object of bundle-id -> [category, ...]")
    seeded: dict[str, tuple[str, ...]] = {}
    for bundle_id, categories in raw.items():
        if not isinstance(categories, list) or not all(
            isinstance(category, str) for category in categories
        ):
            raise SpecError(f"--seeded {path}: {bundle_id!r} must map to a list of categories")
        unknown = sorted(set(categories) - set(CLASSIFICATION_KEYS))
        if unknown:
            raise SpecError(
                f"--seeded {path}: unknown category(s) {', '.join(unknown)} on "
                f"{bundle_id!r}; the six keys are {', '.join(CLASSIFICATION_KEYS)}"
            )
        seeded[str(bundle_id)] = tuple(categories)
    return seeded


def _load_known_clean(path: Path) -> frozenset[str]:
    raw = _load_json(path, "--known-clean")
    if not isinstance(raw, list):
        raise SpecError(f"--known-clean {path}: expected a JSON list of bundle ids")
    ids: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise SpecError(f"--known-clean {path}: expected a JSON list of bundle ids")
        ids.append(item)
    return frozenset(ids)


def _load_scan_labels(path: Path) -> dict[str, _ScanLabels]:
    raw = _load_json(path, "--scan-labels")
    if not isinstance(raw, dict):
        raise SpecError(f"--scan-labels {path}: expected an object of bundle-id -> labels")
    labels: dict[str, _ScanLabels] = {}
    for bundle_id, entry in raw.items():
        if not isinstance(entry, dict):
            raise SpecError(f"--scan-labels {path}: {bundle_id!r} must map to an object")
        allowed = {"secrets", "credential_shaped"}
        unknown_keys = sorted(set(entry) - allowed)
        if unknown_keys:
            raise SpecError(
                f"--scan-labels {path}: {bundle_id!r} has unknown key(s) "
                f"{', '.join(unknown_keys)}; allowed: secrets, credential_shaped"
            )
        for key in allowed:
            paths = entry.get(key, [])
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise SpecError(f"--scan-labels {path}: {bundle_id!r}.{key} must be a path list")
        labels[str(bundle_id)] = _ScanLabels(
            secrets=frozenset(entry.get("secrets", [])),
            credential_shaped=frozenset(entry.get("credential_shaped", [])),
        )
    return labels


def _collect_sources(
    bundles: Sequence[Path], bundle_dirs: Sequence[Path], app_ids: Sequence[uuid.UUID]
) -> tuple[_Source, ...]:
    files: list[Path] = []
    for bundle in bundles:
        if not bundle.is_file():
            raise SpecError(f"--bundle {bundle}: no such file")
        files.append(bundle)
    for directory in bundle_dirs:
        if not directory.is_dir():
            raise SpecError(f"--bundle-dir {directory}: no such directory")
        found = sorted(directory.glob("*.bundle"))
        if not found:
            raise SpecError(f"--bundle-dir {directory}: contains no *.bundle files")
        files.extend(found)

    sources: list[_Source] = [
        _Source(bundle_id=path.stem, kind="local", path=path) for path in files
    ]
    sources.extend(
        _Source(bundle_id=str(app_id), kind="storage", app_id=app_id) for app_id in app_ids
    )
    if not sources:
        raise SpecError("no bundles to evaluate: pass --bundle, --bundle-dir, or --app-id")
    seen: set[str] = set()
    for source in sources:
        if source.bundle_id in seen:
            raise SpecError(
                f"duplicate bundle id {source.bundle_id!r} — manifests key on the id, "
                "so two sources sharing one would be indistinguishable"
            )
        seen.add(source.bundle_id)
    return tuple(sources)


def _build_spec(args: argparse.Namespace) -> _EvalSpec:
    sources = _collect_sources(args.bundle, args.bundle_dir, args.app_id)
    seeded = _load_seeded(args.seeded) if args.seeded else {}
    known_clean = _load_known_clean(args.known_clean) if args.known_clean else frozenset()
    scan_labels = _load_scan_labels(args.scan_labels) if args.scan_labels else {}

    ids = {source.bundle_id for source in sources}
    for name, referenced in (
        ("--seeded", set(seeded)),
        ("--known-clean", set(known_clean)),
        ("--scan-labels", set(scan_labels)),
    ):
        unmatched = sorted(referenced - ids)
        if unmatched:
            raise SpecError(
                f"{name} names bundle id(s) matching nothing in the sample: "
                f"{', '.join(unmatched)} — a typo'd id would silently measure nothing"
            )
    contradictions = sorted(set(seeded) & known_clean)
    if contradictions:
        raise SpecError(
            f"bundle(s) both seeded and known-clean: {', '.join(contradictions)} — "
            "a seeded bundle is by definition not clean"
        )
    return _EvalSpec(
        sources=sources, seeded=seeded, known_clean=known_clean, scan_labels=scan_labels
    )


# ---------------------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------------------


def _clone_local_bundle(bundle_path: Path, scratch: Path) -> tuple[str, Path]:
    """Extract a local bundle the way the platform does: validated header parse for the
    HEAD SHA, then a jailed clone — scrubbed HOME (no user gitconfig hooks/filters),
    `--template=`, and `core.symlinks=false` so a planted symlink materializes inert.
    PATH passes through: the eval host is a workstation, not the jailed server."""
    head_sha = parse_bundle_head_sha(bundle_path.read_bytes())
    clone_dir = scratch / "tree"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(scratch),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.symlinks=false",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--template=",
                str(bundle_path),
                str(clone_dir),
            ],
            cwd=scratch,
            env=env,
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _EvalRunFailedError(
            _EXTRACT_FAILED, f"git clone timed out after {_CLONE_TIMEOUT_S:.0f}s"
        ) from exc
    except FileNotFoundError as exc:
        raise _EvalRunFailedError(_EXTRACT_FAILED, "the `git` binary is not on PATH") from exc
    if completed.returncode != 0:
        raise _EvalRunFailedError(_EXTRACT_FAILED, f"git clone failed: {completed.stderr[:500]}")
    return head_sha, clone_dir


async def _extract(source: _Source, scratch: Path) -> tuple[str, Path]:
    """One bundle to an extracted tree, every disappointment mapped to a failure kind."""
    if source.kind == "local":
        if source.path is None:  # structurally impossible; fail loudly, not silently
            raise _EvalRunFailedError(_EXTRACT_FAILED, "local source carries no path")
        try:
            return await asyncio.to_thread(_clone_local_bundle, source.path, scratch)
        except BundleValidationError as exc:
            raise _EvalRunFailedError(_EXTRACT_FAILED, str(exc)) from exc
    if source.app_id is None:
        raise _EvalRunFailedError(_EXTRACT_FAILED, "storage source carries no app id")
    try:
        extracted = await extract_snapshot(source.app_id, cache_root=scratch)
    except BundleValidationError as exc:
        raise _EvalRunFailedError(_EXTRACT_FAILED, str(exc)) from exc
    except SnapshotExtractionError as exc:
        raise _EvalRunFailedError(_EXTRACT_FAILED, str(exc)) from exc
    except StorageError as exc:
        raise _EvalRunFailedError(_STORAGE_UNAVAILABLE, str(exc)) from exc
    if isinstance(extracted, NoAppYet):
        raise _EvalRunFailedError(_NO_APP_YET, "the app has no saved bundle")
    return extracted.head_sha, extracted.root


# ---------------------------------------------------------------------------------------
# One bundle's evaluation
# ---------------------------------------------------------------------------------------


def _scan_doc(sweep: CredentialSweep) -> dict[str, Any]:
    return {
        "tier_a_paths": sorted({h.path for h in sweep.hits if h.hit.tier is Tier.A}),
        "tier_b_paths": sorted({h.path for h in sweep.hits if h.hit.tier is Tier.B}),
        "hits": [
            {"path": h.path, "family": h.hit.family, "tier": h.hit.tier.value, "line": h.hit.line}
            for h in sweep.hits
        ],
        "incomplete": sweep.incomplete,
    }


def _apply_evidence_rule(
    output: ReviewOutput, root: Path
) -> tuple[dict[str, str], dict[str, str], list[str], dict[str, Any]]:
    """R4, exactly as production applies it (`_cites_a_real_location` is imported from
    the service, not copied, so the rule cannot drift): a Yes whose every cited location
    does not exist becomes unanswered. Returns (raw, effective, downgraded, evidence)."""
    from src.services.classification.service import _cites_a_real_location

    raw: dict[str, str] = {}
    effective: dict[str, str] = {}
    downgraded: list[str] = []
    evidence: dict[str, Any] = {}
    for question in output.questions:
        refs = [
            {"path": ref.path, "kind": ref.kind, "valid": _cites_a_real_location(root, ref.path)}
            for ref in question.evidence
        ]
        raw[question.key] = question.verdict.value
        verdict = question.verdict.value
        if verdict == "yes" and not any(ref["valid"] for ref in refs):
            verdict = "unanswered"
            downgraded.append(question.key)
        effective[question.key] = verdict
        evidence[question.key] = refs
    return raw, effective, downgraded, evidence


class EvalRow(TypedDict):
    """One `row_type: "run"` report row — the wire shape, named once.

    A TypedDict rather than a dataclass on purpose: this IS the on-disk JSONL record, so a
    structure that has to be converted before writing would add a second shape to keep in
    step with the first. What was missing was never the object, it was the CONTRACT — an
    18-parameter builder returning `dict[str, Any]` read back by string key in three
    summarisers, where a typo is silent at every gate. Every key is always present (a
    consumer greps a field name and gets every run, `None` where it could not apply), so
    `total=True` is the honest declaration."""

    row_type: str
    bundle_id: str
    source: str
    origin: str
    timestamp: str
    deployment: str | None
    head_sha: str | None
    status: str
    failure_kind: str | None
    failure_detail: str | None
    wall_clock_s: float
    requests: int | None
    tool_calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    final_step_output_tokens: int | None
    completeness: str | None
    verdicts: dict[str, str] | None
    effective_verdicts: dict[str, str] | None
    downgraded: list[str] | None
    evidence: dict[str, Any] | None
    scan: dict[str, Any] | None
    seeded: list[str] | None
    caught: dict[str, bool] | None
    known_clean: bool
    would_route: bool | None


def _row(
    source: _Source,
    *,
    deployment: str | None,
    status: str,
    wall_clock_s: float,
    known_clean: bool,
    head_sha: str | None = None,
    failure_kind: str | None = None,
    failure_detail: str | None = None,
    recorder: _FlightRecorder | None = None,
    completeness: str | None = None,
    verdicts: dict[str, str] | None = None,
    effective_verdicts: dict[str, str] | None = None,
    downgraded: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
    scan: dict[str, Any] | None = None,
    seeded: tuple[str, ...] | None = None,
    caught: dict[str, bool] | None = None,
    would_route: bool | None = None,
) -> EvalRow:
    """One report row. EVERY key is always present — a consumer greps a field name and
    gets every run, with null where a field could not apply."""
    return {
        "row_type": "run",
        "bundle_id": source.bundle_id,
        "source": source.kind,
        "origin": source.origin,
        "timestamp": datetime.now(UTC).isoformat(),
        "deployment": deployment,
        "head_sha": head_sha,
        "status": status,
        "failure_kind": failure_kind,
        # The ONE place a detail reaches the report file, so the redact-then-cap rule is
        # applied here rather than at each producer — same rule the production runner
        # applies before storing a detail (`service.py`), same ceiling. A model error can
        # quote the source it was reading, and this file outlives the run.
        "failure_detail": redact_and_cap(failure_detail, _DETAIL_MAX_CHARS),
        "wall_clock_s": round(wall_clock_s, 3),
        "requests": recorder.requests if recorder else None,
        "tool_calls": recorder.tool_calls if recorder else None,
        "input_tokens": recorder.input_tokens if recorder else None,
        "output_tokens": recorder.output_tokens if recorder else None,
        "cache_read_tokens": recorder.cache_read_tokens if recorder else None,
        "cache_write_tokens": recorder.cache_write_tokens if recorder else None,
        "final_step_output_tokens": (
            recorder.final_step_output_tokens if recorder and recorder.requests else None
        ),
        "completeness": completeness,
        "verdicts": verdicts,
        "effective_verdicts": effective_verdicts,
        "downgraded": downgraded,
        "evidence": evidence,
        "scan": scan,
        "seeded": list(seeded) if seeded is not None else None,
        "caught": caught,
        "known_clean": known_clean,
        "would_route": would_route,
    }


async def _evaluate_one(
    source: _Source,
    spec: _EvalSpec,
    *,
    model_factory: ModelFactory | None,
    deployment: str | None,
    scan_only: bool,
    request_limit: int,
    run_timeout: float,
    sweep_root: Path,
) -> EvalRow:
    """Run one bundle end to end. NEVER raises for a per-bundle problem — a bundle that
    fails to extract (or a run that fails in the model) is a failure ROW, and the sweep
    moves on to the next bundle."""
    # Lazy on purpose — these modules' import chain resolves the full Settings (see
    # the import-block note at the top of the file).
    from src.services.classification.agent import OUTPUT_TOOL_NAME, run_review
    from src.services.classification.scan import scan_snapshot

    known_clean = source.bundle_id in spec.known_clean
    seeded = spec.seeded.get(source.bundle_id)
    scratch = sweep_root / f"run-{uuid.uuid4().hex[:12]}"
    await asyncio.to_thread(scratch.mkdir, parents=True)
    started = time.monotonic()
    try:
        try:
            head_sha, root = await _extract(source, scratch)
        except _EvalRunFailedError as failure:
            return _row(
                source,
                deployment=deployment,
                status="failed",
                wall_clock_s=time.monotonic() - started,
                known_clean=known_clean,
                failure_kind=failure.kind,
                failure_detail=failure.detail,
                seeded=seeded,
                would_route=None if scan_only else True,
            )

        try:
            sweep = await scan_snapshot(root)
        except Exception as exc:  # noqa: BLE001 — an unreadable tree is this bundle's
            # failure row, never the sweep's abort (BaseException still propagates).
            return _row(
                source,
                deployment=deployment,
                status="failed",
                wall_clock_s=time.monotonic() - started,
                known_clean=known_clean,
                head_sha=head_sha,
                failure_kind=f"unexpected:{type(exc).__name__}",
                failure_detail=str(exc),
                seeded=seeded,
                would_route=None if scan_only else True,
            )
        scan = _scan_doc(sweep)
        if scan_only:
            return _row(
                source,
                deployment=deployment,
                status="scan_only",
                wall_clock_s=time.monotonic() - started,
                known_clean=known_clean,
                head_sha=head_sha,
                scan=scan,
                seeded=seeded,
            )

        if model_factory is None:  # argument validation already prevents this
            raise SpecError("model runs requested but no model factory resolved")
        recorder = _FlightRecorder(model_factory(), output_tool_name=OUTPUT_TOOL_NAME)
        failure_kind: str | None = None
        failure_detail: str | None = None
        output: ReviewOutput | None = None
        try:
            async with asyncio.timeout(run_timeout):
                result = await run_review(
                    model=recorder,
                    user_id=uuid.uuid4(),  # attribution-only; nothing persists it here
                    snapshot_root=root,
                    scan_hits=sweep.hits,
                    usage_limits=UsageLimits(request_limit=request_limit),
                )
            output = result.output
        except TimeoutError:
            failure_kind = _RUN_TIMEOUT
            failure_detail = f"over the eval's --run-timeout ({run_timeout:.0f}s)"
        except _TruncatedError as exc:
            failure_kind = _OUTPUT_TRUNCATED
            failure_detail = str(exc)
        except UsageLimitExceeded as exc:
            failure_kind = _REQUEST_LIMIT
            failure_detail = str(exc)
        except UnexpectedModelBehavior as exc:
            failure_kind = _MODEL_ERROR
            failure_detail = str(exc)
        except ModelAPIError as exc:
            failure_kind = _MODEL_ERROR
            failure_detail = str(exc)
        except Exception as exc:  # noqa: BLE001 — a paid 30-bundle sweep must not abort
            # on run 29; anything unforeseen is a failure ROW with its type named, and
            # BaseException (Ctrl-C, cancellation) still propagates.
            failure_kind = f"unexpected:{type(exc).__name__}"
            failure_detail = str(exc)

        if output is None:
            return _row(
                source,
                deployment=deployment,
                status="failed",
                wall_clock_s=time.monotonic() - started,
                known_clean=known_clean,
                head_sha=head_sha,
                failure_kind=failure_kind,
                failure_detail=failure_detail,
                recorder=recorder,
                scan=scan,
                seeded=seeded,
                would_route=True,  # the ladder routes every run failure (R20)
            )

        raw, effective, downgraded, evidence = _apply_evidence_rule(output, root)
        partial = output.completeness is Completeness.PARTIAL
        status = "failed" if partial else "complete"
        caught: dict[str, bool] | None = None
        if seeded is not None and not partial:
            caught = {category: effective[category] == "yes" for category in seeded}
        return _row(
            source,
            deployment=deployment,
            status=status,
            wall_clock_s=time.monotonic() - started,
            known_clean=known_clean,
            head_sha=head_sha,
            failure_kind=_PARTIAL_REVIEW if partial else None,
            failure_detail="the model reported a partial review" if partial else None,
            recorder=recorder,
            completeness=output.completeness.value,
            verdicts=raw,
            effective_verdicts=effective,
            downgraded=downgraded,
            evidence=evidence,
            scan=scan,
            seeded=seeded,
            caught=caught,
            would_route=(
                True if partial else any(effective[key] == "yes" for key in WEIGHTED_KEYS)
            ),
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)


# ---------------------------------------------------------------------------------------
# The summary — the named figures, and the distributions the ceilings are re-set from
# ---------------------------------------------------------------------------------------


def _dist(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": round(min(values), 1),
        "median": round(statistics.median(values), 1),
        "max": round(max(values), 1),
    }


def _summarize(rows: list[EvalRow], spec: _EvalSpec, deployment: str | None) -> dict[str, Any]:
    """The machine-readable summary row. NOTE — the distributions here (wall-clock,
    requests, final-step output tokens) are the inputs that later re-set
    `REVIEW_WALL_CLOCK_CEILING_S`, `REVIEW_REQUEST_BUDGET` and the 8,000-token
    `MAX_TOKENS` cap in `src/services/classification/constants.py`. This script NEVER
    modifies those ceilings itself: the plan's "Modify service.py (ceilings, once
    measured)" happens after a real measured run against live Foundry, as its own
    reviewed change, and the ceilings belong to the deployment recorded here."""
    complete = [row for row in rows if row["status"] == "complete"]
    failed = [row for row in rows if row["status"] == "failed"]

    # The false-positive routing rate — the ASM17 figure the ceilings are tuned
    # against: known-clean bundles that would route (weighted-Yes OR run failure).
    clean_rows = [row for row in rows if row["known_clean"]]
    clean_routed = [row for row in clean_rows if row["would_route"]]

    # The miss rate, over seeded findings a COMPLETED review judged. Seeded findings on
    # failed runs are not misses (a failed run routes to a human anyway) — they are
    # counted separately so nobody reads "0 missed" off a sweep where every run died.
    evaluated = 0
    missed = 0
    on_failed_runs = 0
    for row in rows:
        seeded = row["seeded"] or []
        if row["caught"] is not None:
            evaluated += len(seeded)
            missed += sum(1 for category in seeded if not row["caught"][category])
        elif seeded:
            on_failed_runs += len(seeded)

    # Tier A / Tier B precision-recall over the labeled corpus, micro-averaged on file
    # paths. Reported SEPARATELY: Tier A stands in when the model is down, Tier B is
    # only a lead, and blending them would hide the one number that gates the design.
    tier_stats = {tier: {"tp": 0, "fp": 0, "secrets_hit": 0} for tier in ("A", "B")}
    secrets_total = 0
    tier_a_false_paths: list[str] = []
    labeled_unscanned: list[str] = []
    for row in rows:
        labels = spec.scan_labels.get(row["bundle_id"])
        if labels is None:
            continue
        if row["scan"] is None:
            labeled_unscanned.append(row["bundle_id"])
            continue
        secrets_total += len(labels.secrets)
        for tier, paths_key in (("A", "tier_a_paths"), ("B", "tier_b_paths")):
            hit_paths = set(row["scan"][paths_key])
            tier_stats[tier]["tp"] += len(hit_paths & labels.secrets)
            tier_stats[tier]["fp"] += len(hit_paths - labels.secrets)
            tier_stats[tier]["secrets_hit"] += len(labels.secrets & hit_paths)
        for path in sorted(set(row["scan"]["tier_a_paths"]) - labels.secrets):
            tier_a_false_paths.append(f"{row['bundle_id']}:{path}")

    def _precision_recall(tier: str) -> dict[str, Any]:
        stats = tier_stats[tier]
        hits = stats["tp"] + stats["fp"]
        return {
            "hits": hits,
            "true_positives": stats["tp"],
            "false_positives": stats["fp"],
            "precision": round(stats["tp"] / hits, 4) if hits else None,
            "recall": round(stats["secrets_hit"] / secrets_total, 4) if secrets_total else None,
        }

    tier_a = _precision_recall("A")
    if not spec.scan_labels:
        gate = "no-labeled-corpus"
    else:
        # 100% precision required: a Tier A false positive becomes a verdict nobody
        # reviewed. Zero hits passes vacuously — the hit count above keeps that visible.
        gate = "pass" if tier_a["false_positives"] == 0 else "fail"

    return {
        "row_type": "summary",
        "timestamp": datetime.now(UTC).isoformat(),
        "deployment": deployment,
        "runs": len(rows),
        "complete": len(complete),
        "failed": len(failed),
        "scan_only": sum(1 for row in rows if row["status"] == "scan_only"),
        "known_clean_total": len(clean_rows),
        "known_clean_routed": len(clean_routed),
        "false_positive_routing_rate": (
            round(len(clean_routed) / len(clean_rows), 4) if clean_rows else None
        ),
        "seeded_findings_evaluated": evaluated,
        "seeded_findings_missed": missed,
        "seeded_findings_on_failed_runs": on_failed_runs,
        "miss_rate": round(missed / evaluated, 4) if evaluated else None,
        "tier_a": tier_a,
        "tier_b": _precision_recall("B"),
        "tier_a_precision_gate": gate,
        "tier_a_false_positive_paths": tier_a_false_paths,
        "labeled_bundles_unscanned": labeled_unscanned,
        "wall_clock_s": _dist([row["wall_clock_s"] for row in complete]),
        "requests": _dist([float(row["requests"]) for row in complete if row["requests"]]),
        "tool_calls": _dist(
            [float(row["tool_calls"]) for row in complete if row["tool_calls"] is not None]
        ),
        "final_step_output_tokens": _dist(
            [
                float(row["final_step_output_tokens"])
                for row in complete
                if row["final_step_output_tokens"] is not None
            ]
        ),
    }


def _human_summary(summary: dict[str, Any]) -> str:
    def _rate(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    def _spread(entry: dict[str, float] | None) -> str:
        if entry is None:
            return "n/a"
        return f"min {entry['min']} / median {entry['median']} / max {entry['max']}"

    def _tier_line(name: str, tier: dict[str, Any]) -> str:
        precision = "n/a" if tier["precision"] is None else f"{tier['precision']:.1%}"
        recall = "n/a" if tier["recall"] is None else f"{tier['recall']:.1%}"
        return (
            f"  Tier {name}: precision {precision}, recall {recall} "
            f"({tier['hits']} hit path(s), {tier['false_positives']} false)"
        )

    lines = [
        "=== classification-review evaluation ===",
        f"deployment: {summary['deployment'] or 'n/a (scan-only)'}",
        f"runs: {summary['runs']} ({summary['complete']} complete, "
        f"{summary['failed']} failed, {summary['scan_only']} scan-only)",
        "",
        "-- the two named figures --",
        f"false-positive routing rate: {_rate(summary['false_positive_routing_rate'])} "
        f"({summary['known_clean_routed']}/{summary['known_clean_total']} known-clean "
        "bundles would route: weighted-Yes or run failure)",
        f"miss rate: {_rate(summary['miss_rate'])} "
        f"({summary['seeded_findings_missed']}/{summary['seeded_findings_evaluated']} seeded "
        f"findings missed; {summary['seeded_findings_on_failed_runs']} on failed runs, "
        "routed regardless)",
        "",
        "-- scan precision/recall (labeled corpus) --",
        _tier_line("A", summary["tier_a"]),
        _tier_line("B", summary["tier_b"]),
        f"  TIER A PRECISION GATE (must be 100%): {summary['tier_a_precision_gate'].upper()}",
    ]
    if summary["tier_a_false_positive_paths"]:
        lines.append(
            "  Tier A false positives (narrow Tier A — a later decision, not this run's):"
        )
        lines.extend(f"    {path}" for path in summary["tier_a_false_positive_paths"])
    if summary["labeled_bundles_unscanned"]:
        lines.append(
            "  labeled but never scanned (extraction failed): "
            + ", ".join(summary["labeled_bundles_unscanned"])
        )
    lines += [
        "",
        "-- distributions the ceilings are re-set from (this script changes NO ceilings;",
        "   constants.py is modified separately, after a real measured run) --",
        f"wall-clock (s):            {_spread(summary['wall_clock_s'])}",
        f"model requests:            {_spread(summary['requests'])}",
        f"tool calls:                {_spread(summary['tool_calls'])}",
        f"final-step output tokens:  {_spread(summary['final_step_output_tokens'])}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------------------


def _default_model_factory() -> ModelFactory:
    """The real thing: the Foundry deployment the platform is configured with, resolved
    through the app's own settings (so `ENV_FILE=.env` behaves exactly as it does for
    the server). Fails BEFORE any extraction or spend when Foundry is unconfigured."""
    from src.config import settings
    from src.services.agent.model import build_foundry_model

    foundry = settings.foundry
    if foundry is None:
        raise SpecError(
            "no Foundry deployment is configured (FOUNDRY__* env) — model runs need "
            "one; use --scan-only for a model-free sweep"
        )
    return lambda: build_foundry_model(foundry)


async def _run_sweep(
    spec: _EvalSpec,
    *,
    model_factory: ModelFactory | None,
    out_path: Path,
    scan_only: bool,
    request_limit: int,
    run_timeout: float,
) -> list[EvalRow]:
    deployment: str | None = None
    if not scan_only:
        if model_factory is None:
            model_factory = _default_model_factory()
        # Resolve the deployment label once, up front — every row records the
        # deployment it ran on, because ceilings measured on one model do not
        # transfer to another (the "Build on Opus now" decision's re-run clause).
        deployment = model_factory().model_name

    sweep_root = Path(tempfile.mkdtemp(prefix="bial-review-eval-"))
    rows: list[EvalRow] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out_path.open("w", encoding="utf-8") as out:
            for source in spec.sources:
                row = await _evaluate_one(
                    source,
                    spec,
                    model_factory=None if scan_only else model_factory,
                    deployment=deployment,
                    scan_only=scan_only,
                    request_limit=request_limit,
                    run_timeout=run_timeout,
                    sweep_root=sweep_root,
                )
                rows.append(row)
                out.write(json.dumps(row) + "\n")
                out.flush()  # a crash mid-sweep keeps every paid row already written
                print(
                    f"[{len(rows)}/{len(spec.sources)}] {source.bundle_id}: {row['status']}"
                    + (f" ({row['failure_kind']})" if row["failure_kind"] else ""),
                    file=sys.stderr,
                )
            summary = _summarize(rows, spec, deployment)
            out.write(json.dumps(summary) + "\n")
        print(_human_summary(summary))
    finally:
        await asyncio.to_thread(shutil.rmtree, sweep_root, ignore_errors=True)
    return rows


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_classification_review",
        description=(
            "Measure the classification review over a corpus of saved app bundles: "
            "budgets (wall-clock, requests, tokens, the final step's output tokens), "
            "verdict accuracy against seeded/known-clean manifests, and the credential "
            "scan's Tier A/B precision-recall against a labeled corpus."
        ),
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        action="append",
        default=[],
        help="a local .bundle file (repeatable)",
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        action="append",
        default=[],
        help="a directory; every *.bundle inside is evaluated (repeatable)",
    )
    parser.add_argument(
        "--app-id",
        type=uuid.UUID,
        action="append",
        default=[],
        help="an app id whose bundle is pulled from object storage (repeatable; "
        "needs the backend env loaded)",
    )
    parser.add_argument(
        "--seeded",
        type=Path,
        default=None,
        help="JSON manifest: {bundle-id: [seeded category, ...]} — catch/miss is "
        "reported per seeded finding",
    )
    parser.add_argument(
        "--known-clean",
        type=Path,
        default=None,
        help="JSON list of bundle ids known to hold nothing weighted — the "
        "false-positive routing rate is measured over these",
    )
    parser.add_argument(
        "--scan-labels",
        type=Path,
        default=None,
        help="JSON manifest: {bundle-id: {secrets: [path...], credential_shaped: "
        "[path...]}} — Tier A/B precision-recall is measured against it",
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="JSONL report path (run rows + a summary row)"
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="run only the credential scan — no model, no Foundry config, no spend",
    )
    parser.add_argument(
        "--request-limit",
        type=int,
        default=50,
        help="the eval's per-run model-request bound (default 50 — deliberately above "
        "the service's provisional budget, so the measurement is not censored by "
        "the ceiling it exists to set)",
    )
    parser.add_argument(
        "--run-timeout",
        type=float,
        default=600.0,
        help="per-run wall-clock bound in seconds (default 600); exceeding it is a "
        "failure row, never a hung sweep",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, model_factory: ModelFactory | None = None) -> int:
    """Entry point. `model_factory` is the test seam: tests inject a scripted
    (FunctionModel) factory so no real Foundry is ever called; the default resolves
    the platform's configured Foundry deployment."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.request_limit < 1:
        parser.error("--request-limit must be at least 1")
    if args.run_timeout <= 0:
        parser.error("--run-timeout must be positive")
    try:
        spec = _build_spec(args)
        asyncio.run(
            _run_sweep(
                spec,
                model_factory=model_factory,
                out_path=args.out,
                scan_only=args.scan_only,
                request_limit=args.request_limit,
                run_timeout=args.run_timeout,
            )
        )
    except SpecError as exc:
        parser.error(str(exc))  # exits 2, with usage
    return 0


if __name__ == "__main__":
    sys.exit(main())
