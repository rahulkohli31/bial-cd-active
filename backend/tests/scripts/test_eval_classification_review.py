"""U14 — the evaluation script, exercised for ARGUMENT HANDLING and REPORT SHAPE only.

The script's real output is a measurement run against real bundles and live Foundry —
that run happens outside the test suite, and nothing here asserts accuracy numbers.
What IS pinned: a run against a local fixture bundle with a SCRIPTED model emits a
report row with every field populated; a bundle that fails to extract is a failure ROW
rather than an abort of the sweep; and argument errors exit non-zero with usage. The
models are `FunctionModel` scripts throughout (`ALLOW_MODEL_REQUESTS = False` makes a
real Foundry call impossible, not merely absent).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from scripts import eval_classification_review as eval_script
from src.services.classification.agent import OUTPUT_TOOL_NAME
from src.services.deploy.classification import CLASSIFICATION_KEYS

# The row contract: every run row carries EXACTLY these keys, always — null where a
# field could not apply. A consumer greps a field name and gets every run.
EXPECTED_ROW_KEYS = {
    "row_type",
    "bundle_id",
    "source",
    "origin",
    "timestamp",
    "deployment",
    "head_sha",
    "status",
    "failure_kind",
    "failure_detail",
    "wall_clock_s",
    "requests",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "final_step_output_tokens",
    "completeness",
    "verdicts",
    "effective_verdicts",
    "downgraded",
    "evidence",
    "scan",
    "seeded",
    "caught",
    "known_clean",
    "would_route",
}

# The named summary figures (and the distributions the ceilings are re-set from).
EXPECTED_SUMMARY_KEYS = {
    "row_type",
    "timestamp",
    "deployment",
    "runs",
    "complete",
    "failed",
    "scan_only",
    "known_clean_total",
    "known_clean_routed",
    "false_positive_routing_rate",
    "seeded_findings_evaluated",
    "seeded_findings_missed",
    "seeded_findings_on_failed_runs",
    "miss_rate",
    "tier_a",
    "tier_b",
    "tier_a_precision_gate",
    "tier_a_false_positive_paths",
    "labeled_bundles_unscanned",
    "wall_clock_s",
    "requests",
    "tool_calls",
    "final_step_output_tokens",
}

_TIER_A_VALUE = "sk_live_" + "a1b2c3d4e5" * 3
_TIER_A_LINE = f'const stripeKey = "{_TIER_A_VALUE}"\n'
_TIER_B_LINE = 'const password = "hunter2-fixture"\n'


@pytest.fixture(autouse=True)
def _no_live_model():
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = False
    yield
    models.ALLOW_MODEL_REQUESTS = previous


# ---------------------------------------------------------------------------------------
# Fixture bundles (real `git bundle create`, the shape the platform stores)
# ---------------------------------------------------------------------------------------


def _make_bundle(
    tmp_path: Path, name: str, files: dict[str, str] | None = None
) -> tuple[Path, str]:
    """A real HEAD-only bundle at `<tmp_path>/<name>.bundle`, plus its head SHA."""
    repo = tmp_path / f"seed-{name}"
    repo.mkdir()
    contents = files or {"app/page.tsx": "export default () => <main>visitors</main>\n"}
    for rel_path, text in contents.items():
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def _git(*args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    _git("init", "-q")
    _git("add", "-A")
    _git("commit", "-q", "-m", "bial-snapshot")
    head_sha = _git("rev-parse", "HEAD")
    bundle_path = tmp_path / f"{name}.bundle"
    _git("bundle", "create", str(bundle_path), "HEAD")
    return bundle_path, head_sha


# ---------------------------------------------------------------------------------------
# Scripted models (the test_service.py shapes)
# ---------------------------------------------------------------------------------------


def _question(key: str, verdict: str = "no", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": key,
        "evidence": [],
        "reason": "Nothing of this kind was found in the app.",
        "verdict": verdict,
    }
    payload.update(overrides)
    return payload


def _complete_args(**per_key: dict[str, Any]) -> dict[str, Any]:
    return {
        "completeness": "complete",
        "questions": [per_key.get(key, _question(key)) for key in CLASSIFICATION_KEYS],
    }


def _usage(
    input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0
) -> RequestUsage:
    return RequestUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def _factory_for(responses: list[ModelResponse]):
    """A model factory whose runs consume `responses` in order, one per model request.
    The sweep also builds one model just to read the deployment label — that build
    performs no requests and consumes nothing."""
    remaining = list(responses)

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not remaining:
            raise AssertionError("a model request arrived but no response was scripted")
        return remaining.pop(0)

    return lambda: FunctionModel(respond)


def _read_then_complete(**per_key: dict[str, Any]) -> list[ModelResponse]:
    """Two model steps: a real tool call (read_file over the extracted tree), then the
    structured output — so requests, tool calls, and the FINAL step's output tokens are
    all distinguishable in the report."""
    read = ModelResponse(parts=[ToolCallPart("read_file", {"path": "app/page.tsx"})])
    read.usage = _usage(400, 20, cache_read=10, cache_write=5)
    output = ModelResponse(parts=[ToolCallPart(OUTPUT_TOOL_NAME, _complete_args(**per_key))])
    output.usage = _usage(1_000, 80, cache_read=300, cache_write=200)
    return [read, output]


def _rows(out_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lines = [json.loads(line) for line in out_path.read_text().splitlines()]
    runs = [line for line in lines if line["row_type"] == "run"]
    summaries = [line for line in lines if line["row_type"] == "summary"]
    assert len(summaries) == 1  # exactly one summary row, last
    return runs, summaries[0]


# ---------------------------------------------------------------------------------------
# Happy path: a fixture bundle, a scripted model, a fully populated row
# ---------------------------------------------------------------------------------------


def test_happy_path_emits_a_report_row_with_every_field_populated(tmp_path: Path) -> None:
    bundle_path, head_sha = _make_bundle(tmp_path, "visitor-log")
    seeded_path = tmp_path / "seeded.json"
    seeded_path.write_text(json.dumps({"visitor-log": ["financial_data"]}))
    out_path = tmp_path / "report.jsonl"
    factory = _factory_for(
        _read_then_complete(
            financial_data=_question(
                "financial_data",
                verdict="yes",
                evidence=[{"path": "app/page.tsx", "kind": "form-field"}],
                reason="The app records invoice amounts.",
            )
        )
    )

    code = eval_script.main(
        [
            "--bundle",
            str(bundle_path),
            "--seeded",
            str(seeded_path),
            "--out",
            str(out_path),
        ],
        model_factory=factory,
    )

    assert code == 0
    runs, summary = _rows(out_path)
    assert len(runs) == 1
    row = runs[0]
    assert set(row) == EXPECTED_ROW_KEYS

    # Identity + provenance.
    assert row["bundle_id"] == "visitor-log"
    assert row["source"] == "local"
    assert row["origin"] == str(bundle_path)
    assert row["head_sha"] == head_sha
    assert row["deployment"] == factory().model_name  # the run's model, on every row
    assert row["timestamp"]

    # The run itself.
    assert row["status"] == "complete"
    assert row["failure_kind"] is None
    assert row["failure_detail"] is None
    assert row["wall_clock_s"] > 0
    assert row["completeness"] == "complete"

    # Budgets: requests, tool calls, the four RAW token classes, and the FINAL step's
    # output tokens separately (the 8,000 cap is later re-set from that distribution).
    assert row["requests"] == 2
    assert row["tool_calls"] == 1  # read_file only — the output tool is not a step
    assert row["input_tokens"] == 1_400
    assert row["output_tokens"] == 100
    assert row["cache_read_tokens"] == 310
    assert row["cache_write_tokens"] == 205
    assert row["final_step_output_tokens"] == 80  # the LAST step alone, not the sum

    # Verdicts, the production R4 downgrade, catch/miss, and routing.
    assert set(row["verdicts"]) == set(CLASSIFICATION_KEYS)
    assert row["verdicts"]["financial_data"] == "yes"
    assert row["effective_verdicts"]["financial_data"] == "yes"  # evidence path is real
    assert row["downgraded"] == []
    assert row["evidence"]["financial_data"] == [
        {"path": "app/page.tsx", "kind": "form-field", "valid": True}
    ]
    assert row["scan"] == {
        "tier_a_paths": [],
        "tier_b_paths": [],
        "hits": [],
        "incomplete": False,
    }
    assert row["seeded"] == ["financial_data"]
    assert row["caught"] == {"financial_data": True}
    assert row["known_clean"] is False
    assert row["would_route"] is True  # a weighted Yes routes

    # The summary row carries the named figures.
    assert set(summary) == EXPECTED_SUMMARY_KEYS
    assert summary["runs"] == 1
    assert summary["complete"] == 1
    assert summary["seeded_findings_evaluated"] == 1
    assert summary["seeded_findings_missed"] == 0
    assert summary["miss_rate"] == 0.0
    assert summary["false_positive_routing_rate"] is None  # no known-clean set given
    assert summary["final_step_output_tokens"] == {"min": 80.0, "median": 80.0, "max": 80.0}


def test_a_seeded_finding_the_review_answers_no_is_a_miss(tmp_path: Path) -> None:
    bundle_path, _sha = _make_bundle(tmp_path, "seeded-health")
    seeded_path = tmp_path / "seeded.json"
    seeded_path.write_text(json.dumps({"seeded-health": ["health_data"]}))
    out_path = tmp_path / "report.jsonl"
    factory = _factory_for(_read_then_complete())  # six No verdicts

    code = eval_script.main(
        ["--bundle", str(bundle_path), "--seeded", str(seeded_path), "--out", str(out_path)],
        model_factory=factory,
    )

    assert code == 0
    runs, summary = _rows(out_path)
    assert runs[0]["caught"] == {"health_data": False}
    assert summary["seeded_findings_missed"] == 1
    assert summary["miss_rate"] == 1.0


# ---------------------------------------------------------------------------------------
# Edge: a bundle that fails to extract is a failure ROW, never an abort
# ---------------------------------------------------------------------------------------


def test_an_unextractable_bundle_is_a_failure_row_and_the_sweep_continues(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()
    (bundle_dir / "broken.bundle").write_bytes(b"definitely not a git bundle")
    good_path, good_sha = _make_bundle(tmp_path, "visitor-log")
    (bundle_dir / "visitor-log.bundle").write_bytes(good_path.read_bytes())
    out_path = tmp_path / "report.jsonl"
    factory = _factory_for(_read_then_complete())  # scripted for the ONE good run only

    code = eval_script.main(
        ["--bundle-dir", str(bundle_dir), "--out", str(out_path)], model_factory=factory
    )

    assert code == 0  # the sweep finished — the broken bundle did not abort it
    runs, summary = _rows(out_path)
    assert [row["bundle_id"] for row in runs] == ["broken", "visitor-log"]

    failure = runs[0]
    assert set(failure) == EXPECTED_ROW_KEYS  # the failure ROW keeps the full shape
    assert failure["status"] == "failed"
    assert failure["failure_kind"] == "extract_failed"
    assert failure["failure_detail"]
    assert failure["head_sha"] is None
    assert failure["requests"] is None  # the model was never touched
    assert failure["verdicts"] is None
    assert failure["would_route"] is True  # the ladder routes every run failure (R20)

    good = runs[1]
    assert good["status"] == "complete"
    assert good["head_sha"] == good_sha
    assert summary["runs"] == 2
    assert summary["failed"] == 1


def test_a_model_failure_is_a_failure_row_with_the_recorder_still_read(
    tmp_path: Path,
) -> None:
    from pydantic_ai.exceptions import ModelHTTPError

    bundle_path, head_sha = _make_bundle(tmp_path, "visitor-log")
    out_path = tmp_path / "report.jsonl"

    async def boom(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=500, model_name="opus", body="boom")

    code = eval_script.main(
        ["--bundle", str(bundle_path), "--out", str(out_path)],
        model_factory=lambda: FunctionModel(boom),
    )

    assert code == 0
    runs, _summary = _rows(out_path)
    row = runs[0]
    assert set(row) == EXPECTED_ROW_KEYS
    assert row["status"] == "failed"
    assert row["failure_kind"] == "model_error"
    assert row["head_sha"] == head_sha  # extraction succeeded; the model did not
    assert row["requests"] == 0  # the recorder was wired in and reports honestly
    assert row["would_route"] is True


# ---------------------------------------------------------------------------------------
# Known-clean routing and the scan-only lane
# ---------------------------------------------------------------------------------------


def test_a_known_clean_bundle_flagged_yes_drives_the_routing_rate(tmp_path: Path) -> None:
    bundle_path, _sha = _make_bundle(tmp_path, "visitor-log")
    clean_path = tmp_path / "clean.json"
    clean_path.write_text(json.dumps(["visitor-log"]))
    out_path = tmp_path / "report.jsonl"
    factory = _factory_for(
        _read_then_complete(
            personal_information=_question(
                "personal_information",
                verdict="yes",
                evidence=[{"path": "app/page.tsx", "kind": "form-field"}],
                reason="The app collects visitor names and phone numbers.",
            )
        )
    )

    code = eval_script.main(
        ["--bundle", str(bundle_path), "--known-clean", str(clean_path), "--out", str(out_path)],
        model_factory=factory,
    )

    assert code == 0
    runs, summary = _rows(out_path)
    assert runs[0]["known_clean"] is True
    assert runs[0]["would_route"] is True
    assert summary["known_clean_total"] == 1
    assert summary["known_clean_routed"] == 1
    assert summary["false_positive_routing_rate"] == 1.0  # the named ASM17 figure


def test_scan_only_runs_without_any_model_and_reports_tier_precision(tmp_path: Path) -> None:
    bundle_path, _sha = _make_bundle(
        tmp_path,
        "crm-app",
        files={
            "app/page.tsx": "export default () => null\n",
            "app/db.ts": _TIER_A_LINE,
            "app/login.tsx": _TIER_B_LINE,
        },
    )
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps({"crm-app": {"secrets": ["app/db.ts"], "credential_shaped": ["app/login.tsx"]}})
    )
    out_path = tmp_path / "report.jsonl"

    # model_factory=None AND no Foundry configured: --scan-only must not touch either.
    code = eval_script.main(
        [
            "--bundle",
            str(bundle_path),
            "--scan-labels",
            str(labels_path),
            "--scan-only",
            "--out",
            str(out_path),
        ]
    )

    assert code == 0
    runs, summary = _rows(out_path)
    row = runs[0]
    assert set(row) == EXPECTED_ROW_KEYS
    assert row["status"] == "scan_only"
    assert row["deployment"] is None
    assert row["requests"] is None
    assert row["scan"]["tier_a_paths"] == ["app/db.ts"]
    assert row["scan"]["tier_b_paths"] == ["app/login.tsx"]
    # Tier A and Tier B are reported SEPARATELY, and the gate is printed from Tier A:
    # the genuine secret is Tier A (precision 1.0), the login form reaches Tier B only.
    assert summary["tier_a"] == {
        "hits": 1,
        "true_positives": 1,
        "false_positives": 0,
        "precision": 1.0,
        "recall": 1.0,
    }
    assert summary["tier_b"]["false_positives"] == 1  # the lead is not a secret
    assert summary["tier_a_precision_gate"] == "pass"
    assert summary["tier_a_false_positive_paths"] == []


def test_a_tier_a_false_positive_fails_the_precision_gate(tmp_path: Path) -> None:
    bundle_path, _sha = _make_bundle(tmp_path, "demo-app", files={"app/db.ts": _TIER_A_LINE})
    labels_path = tmp_path / "labels.json"
    # The labeler says this bundle holds NO genuine secret — so the Tier A hit is false.
    labels_path.write_text(
        json.dumps({"demo-app": {"secrets": [], "credential_shaped": ["app/db.ts"]}})
    )
    out_path = tmp_path / "report.jsonl"

    code = eval_script.main(
        [
            "--bundle",
            str(bundle_path),
            "--scan-labels",
            str(labels_path),
            "--scan-only",
            "--out",
            str(out_path),
        ]
    )

    assert code == 0
    _runs, summary = _rows(out_path)
    assert summary["tier_a_precision_gate"] == "fail"
    assert summary["tier_a_false_positive_paths"] == ["demo-app:app/db.ts"]


# ---------------------------------------------------------------------------------------
# Argument handling: errors exit non-zero, with usage
# ---------------------------------------------------------------------------------------


def _expect_usage_exit(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as excinfo:
        eval_script.main(argv)
    assert excinfo.value.code == 2  # non-zero, argparse's usage-error code
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower()
    return captured.err


def test_no_input_source_exits_nonzero_with_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    err = _expect_usage_exit(["--out", str(tmp_path / "r.jsonl")], capsys)
    assert "no bundles" in err


def test_missing_out_flag_exits_nonzero_with_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_path, _sha = _make_bundle(tmp_path, "visitor-log")
    err = _expect_usage_exit(["--bundle", str(bundle_path)], capsys)
    assert "--out" in err


def test_an_unknown_seeded_category_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_path, _sha = _make_bundle(tmp_path, "visitor-log")
    seeded_path = tmp_path / "seeded.json"
    seeded_path.write_text(json.dumps({"visitor-log": ["totally_bogus_category"]}))
    err = _expect_usage_exit(
        [
            "--bundle",
            str(bundle_path),
            "--seeded",
            str(seeded_path),
            "--out",
            str(tmp_path / "r.jsonl"),
        ],
        capsys,
    )
    assert "totally_bogus_category" in err


def test_a_manifest_id_matching_no_bundle_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_path, _sha = _make_bundle(tmp_path, "visitor-log")
    seeded_path = tmp_path / "seeded.json"
    seeded_path.write_text(json.dumps({"ghost-app": ["health_data"]}))
    err = _expect_usage_exit(
        [
            "--bundle",
            str(bundle_path),
            "--seeded",
            str(seeded_path),
            "--out",
            str(tmp_path / "r.jsonl"),
        ],
        capsys,
    )
    assert "ghost-app" in err


def test_a_missing_manifest_file_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_path, _sha = _make_bundle(tmp_path, "visitor-log")
    _expect_usage_exit(
        [
            "--bundle",
            str(bundle_path),
            "--seeded",
            str(tmp_path / "nope.json"),
            "--out",
            str(tmp_path / "r.jsonl"),
        ],
        capsys,
    )


def test_a_bundle_both_seeded_and_known_clean_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_path, _sha = _make_bundle(tmp_path, "visitor-log")
    seeded_path = tmp_path / "seeded.json"
    seeded_path.write_text(json.dumps({"visitor-log": ["health_data"]}))
    clean_path = tmp_path / "clean.json"
    clean_path.write_text(json.dumps(["visitor-log"]))
    err = _expect_usage_exit(
        [
            "--bundle",
            str(bundle_path),
            "--seeded",
            str(seeded_path),
            "--known-clean",
            str(clean_path),
            "--out",
            str(tmp_path / "r.jsonl"),
        ],
        capsys,
    )
    assert "seeded and known-clean" in err
