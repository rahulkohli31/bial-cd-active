"""The remediation report has to be arithmetically self-checking, so its arithmetic is tested.

The report's whole claim to credibility is that a reviewer can ADD IT UP: every row on BIAL's
original list lands in exactly one sheet, the sheets sum back to the scanner's own totals, and
`Summary` is derived from the other sheets rather than typed. Those are properties, not
formatting, and each one has a test here that fails when the property breaks.

NO REAL CLIENT DATA. Every fixture below is synthetic — invented CVE ids, invented packages, a
made-up registry. The real exports are client vulnerability data about a live system and never
enter this repository (the generator's own module docstring explains the split). Synthetic data
is also strictly better for these tests: it lets a fixture encode the exact shape a scenario
needs, including shapes the real export does not currently contain.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import openpyxl
import pytest
from openpyxl.workbook import Workbook

from scripts.exception_register import (
    Disposition,
    Entry,
    Override,
    already_at_or_past_fix,
    apply_overrides,
    build_entries,
    build_workbook,
    check_integrity,
    compare_versions,
    disposition_for,
    is_prerelease,
    load_export,
    load_exports,
    load_overrides,
    reconcile,
)

REGISTRY = "testregistry.azurecr.io"
SANDBOX = "citizen-dev-sandbox"
FRONTEND = "vibe-coding-frontend"
BACKEND = "vibe-coding-backend"

DIGEST_OLD = "sha256:" + "a" * 64
DIGEST_NEW = "sha256:" + "b" * 64


# ─────────────────────────────────────────────────────────────────────────────
# Factories — the two export shapes BIAL actually sends
# ─────────────────────────────────────────────────────────────────────────────

_ASSET_HEADERS = [
    "CVE ID",
    "Description",
    "Severity",
    "Status",
    "Asset Name",
    "Software Name",
    "Software Version",
    "Fixed Version",
    "First Detected",
    "Software Package Manager",
    "Exploited in the Wild",
]

_SEVERITY_HEADERS = [
    "CVE ID",
    "Description",
    "Status",
    "Software Name",
    "Software Version",
    "Fixed Version",
    "First Detected",
    "CVE EPSS Score",
    "Software Type",
    "Software Package Manager",
    "Installation Path",
    "Exploited in the Wild",
]


def row(
    cve: str,
    software: str,
    version: str,
    *,
    fixed: str = "-",
    severity: str = "High",
    manager: str = "deb",
    kind: str = "OS",
    path: str = "/var/lib/dpkg/status",
    epss: str = "0.0010",
) -> dict[str, str]:
    """One scanner row, in the shape both writers below understand."""
    return {
        "cve": cve,
        "software": software,
        "version": version,
        "fixed": fixed,
        "severity": severity,
        "manager": manager,
        "kind": kind,
        "path": path,
        "epss": epss,
    }


def write_asset_shape(path: Path, sheets: dict[str, tuple[str, list[dict[str, str]]]]) -> Path:
    """The `vibe-coding_sheet.xlsx` shape: one sheet per ASSET, severity as a column."""
    wb = Workbook()
    if (default := wb.active) is not None:
        wb.remove(default)
    summary = wb.create_sheet("Summary")  # a pivot sheet, which the loader must skip
    summary["A1"] = "Count of CVE ID"
    summary["A2"] = "Row Labels"
    for sheet_name, (asset, rows) in sheets.items():
        ws = wb.create_sheet(sheet_name)
        ws.append(_ASSET_HEADERS)
        for r in rows:
            ws.append(
                [
                    r["cve"],
                    f"synthetic description for {r['cve']}",
                    r["severity"],
                    "New",
                    asset,
                    r["software"],
                    r["version"],
                    r["fixed"],
                    "2026-07-14",
                    r["manager"],
                    "False",
                ]
            )
    wb.save(path)
    return path


def write_severity_shape(path: Path, asset: str, rows: list[dict[str, str]]) -> Path:
    """The `citizen-dev-sandbox` shape: one sheet per SEVERITY, asset only on the pivot."""
    wb = Workbook()
    if (default := wb.active) is not None:
        wb.remove(default)
    summary = wb.create_sheet("Summary")
    summary["A2"] = "CVE COUNT"
    summary["A4"] = asset  # the ONLY place the asset (and its digest) appears
    for severity in ("Critical", "High", "Medium", "Low"):
        ws = wb.create_sheet(severity)
        ws.append(_SEVERITY_HEADERS)
        for r in (x for x in rows if x["severity"] == severity):
            ws.append(
                [
                    r["cve"],
                    f"synthetic description for {r['cve']}",
                    "New",
                    r["software"],
                    r["version"],
                    r["fixed"],
                    "2026-07-23",
                    r["epss"],
                    r["kind"],
                    r["manager"],
                    r["path"],
                    "False",
                ]
            )
    wb.save(path)
    return path


def asset_ref(repo: str, digest: str = DIGEST_OLD) -> str:
    return f"{REGISTRY}/{repo}@{digest}"


# ─────────────────────────────────────────────────────────────────────────────
# Version comparison — the logic a wrong answer here quietly corrupts everything
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        # dpkg basics
        ("1.0", "1.0", 0),
        ("1.0", "1.1", -1),
        ("1.10", "1.9", 1),  # numeric, not lexical — the classic bug
        ("2.5.0-1+deb12u2", "2.5.0-1+deb12u3", -1),
        ("7.88.1-10+deb12u15", "7.88.1-10+deb12u9", 1),  # 15 > 9, not "1" < "9"
        # epochs win outright
        ("1:1.0", "2.0", 1),
        # alpine revisions
        ("3.3.3-r0", "3.3.7-r0", -1),
        ("2.13.4-r5", "2.13.4-r10", -1),
        ("1.2.5-r9", "1.2.5-r9", 0),
        # `~` sorts BEFORE the empty string — this is what makes a pre-release older
        ("1.0~rc1", "1.0", -1),
        ("252.39-1~deb12u2", "252.39-1", -1),
        # go/v prefixes are stripped so feeds can be compared with installed versions
        ("go1.22.3", "1.22.4", -1),
        ("v2.8.4", "2.11.4", -1),
        ("go1.24.13", "1.24.13", 0),
    ],
)
def test_compare_versions(a: str, b: str, expected: int) -> None:
    assert compare_versions(a, b) == expected
    assert compare_versions(b, a) == -expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("3.15.0a6", True),
        ("3.15.0b3", True),
        ("1.26.0-rc.3", True),
        ("2.0.0-beta", True),
        ("1.0~rc1", True),
        # NOT pre-releases — the two shapes a naive regex mangles
        ("2.13.4-r5", False),  # alpine revision
        ("7.88.1-10+deb12u15", False),  # debian point release
        ("3.14.7", False),
        ("go1.23.12", False),
        ("16.2.12", False),
    ],
)
def test_is_prerelease(version: str, expected: bool) -> None:
    assert is_prerelease(version) is expected


@pytest.mark.parametrize(
    ("installed", "fixed", "expected"),
    [
        # THE RELEASE-LINE RULE. Getting this wrong marks nearly every genuine finding as a
        # dispute, which is the one claim a reviewer can disprove from their own console.
        ("go1.23.12", "1.24.9, 1.25.3", False),  # no fix on OUR line — still affected
        ("go1.22.3", "1.21.11, 1.22.4", False),  # same-line fix is 1.22.4 — still affected
        ("go1.22.5", "1.21.11, 1.22.4", True),  # past the same-line fix — dispute
        ("go1.24.13", "1.24.13, 1.25.7", True),  # exactly at the fix — dispute
        # A same-line fix must not be masked by a higher out-of-line candidate.
        ("1.22.9", "1.22.4, 9.9.9", True),
        # No same-line candidate at all: only "past ALL of them" counts.
        ("4.0.0", "2.9, 3.1", True),
        ("2.8", "2.9, 3.1", False),
        # No fix named at all is never a dispute.
        ("1.0", "-", False),
        ("1.0", "", False),
    ],
)
def test_already_at_or_past_fix(installed: str, fixed: str, expected: bool) -> None:
    assert already_at_or_past_fix(installed, fixed) is expected


# ─────────────────────────────────────────────────────────────────────────────
# Ingest
# ─────────────────────────────────────────────────────────────────────────────


def test_both_export_shapes_normalise_to_one_table(tmp_path: Path) -> None:
    """The two workbooks have different columns; the UNION is the schema, not the intersection.

    Dropping the columns only one shape carries would discard the installation path, which is
    the only thing separating the sandbox's vendored-build-binary findings from its OS ones.
    """
    a = write_asset_shape(
        tmp_path / "assets.xlsx",
        {
            FRONTEND: (
                asset_ref(FRONTEND),
                [row("CVE-9000-0001", "libssl3", "3.3.3-r0", manager="apk")],
            ),
            BACKEND: (asset_ref(BACKEND), [row("CVE-9000-0002", "libc6", "2.41-12")]),
        },
    )
    b = write_severity_shape(
        tmp_path / "severity.xlsx",
        asset_ref(SANDBOX),
        [
            row(
                "CVE-9000-0003",
                "stdlib",
                "go1.20.7",
                manager="go-module",
                path="/usr/local/bin/caddy",
            )
        ],
    )

    findings = load_exports([a, b])

    assert len(findings) == 3
    by_image = {f.image: f for f in findings}
    assert set(by_image) == {FRONTEND, BACKEND, SANDBOX}
    # Severity comes from the SHEET NAME in the second shape and from a COLUMN in the first.
    assert by_image[SANDBOX].severity == "High"
    assert by_image[FRONTEND].severity == "High"
    # The install path survives from the shape that has it, and is empty (never invented) in
    # the shape that does not.
    assert by_image[SANDBOX].install_path == "/usr/local/bin/caddy"
    assert by_image[FRONTEND].install_path == ""
    # Provenance is per-row, which is what makes the partition check possible at all.
    assert len({f.source for f in findings}) == 3


def test_the_asset_digest_is_recovered_from_the_pivot_only_shape(tmp_path: Path) -> None:
    """The sandbox export names its asset ONLY on the pivot sheet — losing it loses the digest.

    That digest is the rollback anchor for an artifact shipping under a mutable tag, so a
    loader that skipped the pivot sheet would throw away the one recoverable record of what
    was running.
    """
    path = write_severity_shape(
        tmp_path / "s.xlsx", asset_ref(SANDBOX), [row("CVE-9000-0004", "libexpat1", "2.5.0-1")]
    )
    findings = load_export(path)
    assert len(findings) == 1
    assert findings[0].image == SANDBOX
    assert findings[0].asset.endswith(DIGEST_OLD)


def test_pivot_sheets_are_skipped_not_counted_twice(tmp_path: Path) -> None:
    """A Summary sheet is a VIEW of the rows. Counting it would double every total."""
    path = write_asset_shape(
        tmp_path / "a.xlsx",
        {
            FRONTEND: (
                asset_ref(FRONTEND),
                [row("CVE-9000-0005", "nginx", "1.27.5-r1", manager="apk")],
            )
        },
    )
    assert len(load_export(path)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Dispositions
# ─────────────────────────────────────────────────────────────────────────────


def _one(tmp_path: Path, image: str, r: dict[str, str], *, name: str = "x.xlsx") -> Entry:
    if image == SANDBOX:
        path = write_severity_shape(tmp_path / name, asset_ref(SANDBOX), [r])
    else:
        path = write_asset_shape(tmp_path / name, {image: (asset_ref(image), [r])})
    entries = build_entries(load_export(path))
    assert len(entries) == 1
    return entries[0]


def test_a_finding_already_past_its_fix_is_a_dispute_not_an_exception(tmp_path: Path) -> None:
    """Plan AE2. Filing this as an exception concedes a vulnerability we do not have."""
    entry = _one(
        tmp_path,
        FRONTEND,
        row("CVE-9000-0010", "libssl3", "3.3.9-r0", fixed="3.3.7-r0", manager="apk"),
    )
    assert entry.verdict.disposition is Disposition.DISPUTE
    assert "already meets or exceeds" in entry.verdict.reason
    assert "3.3.7-r0" in entry.verdict.reason  # the reviewer can check it without asking us


def test_a_prerelease_only_fix_is_accepted_risk_with_the_prerelease_as_its_reason(
    tmp_path: Path,
) -> None:
    """Plan AE3. There is no released version to move to, so 'deferred' would be a lie —
    deferred means a fix exists and we chose not to take it."""
    entry = _one(
        tmp_path,
        BACKEND,
        row(
            "CVE-9000-0011",
            "python",
            "3.14.6",
            fixed="3.15.0a6",
            manager="binary",
            kind="Application",
        ),
    )
    assert entry.verdict.disposition is Disposition.EXCEPTION
    assert "pre-release" in entry.verdict.reason
    assert "3.15.0a6" in entry.verdict.reason


def test_a_fixable_finding_nobody_claimed_is_deferred_and_loudly_unowned(
    tmp_path: Path,
) -> None:
    """A fixable finding that no rule clears must never quietly become an exception.

    It lands in the third disposition with placeholder owner/date that the CLI treats as a
    blocker — an unowned deferral is indistinguishable from an oversight, so it is made noisy
    rather than tidy.
    """
    entry = _one(
        tmp_path,
        FRONTEND,
        # An ecosystem no frontend rule matches, WITH a released fix available.
        row(
            "CVE-9000-0012",
            "some-lib",
            "1.0.0",
            fixed="1.0.1",
            manager="cargo",
            kind="Application",
        ),
    )
    assert entry.verdict.disposition is Disposition.DEFERRED
    assert entry.verdict.owner == "UNASSIGNED"
    assert entry.verdict.target_date == "UNSET"


def test_debian_no_fix_rows_are_held_never_filed_as_exceptions(tmp_path: Path) -> None:
    """The Debian feed names no fixed version on ANY row, so an empty field there is a property
    of the feed rather than proof the package is unfixable. Filing it as an exception asserts
    something the data does not support."""
    entry = _one(tmp_path, SANDBOX, row("CVE-9000-0013", "libsystemd0", "252.39-1", fixed="-"))
    assert entry.verdict.disposition is Disposition.HELD


def test_the_kept_sandbox_tooling_is_excepted_only_when_it_is_genuinely_unfixable(
    tmp_path: Path,
) -> None:
    """curl and procps stay in the sandbox by decision — but a FIXABLE finding against them is
    still cleared by the base move. Filing a fixable one as an accepted risk while claiming
    zero-fixable is the single claim a reviewer can disprove from their own console."""
    unfixable = _one(
        tmp_path,
        SANDBOX,
        row("CVE-9000-0014", "curl", "7.88.1-10", fixed="-"),
        name="unfixable.xlsx",
    )
    fixable = _one(
        tmp_path,
        SANDBOX,
        row("CVE-9000-0015", "curl", "7.88.1-10", fixed="7.88.1-11"),
        name="fixable.xlsx",
    )
    assert unfixable.verdict.disposition is Disposition.EXCEPTION
    assert fixable.verdict.disposition is Disposition.FIXED


def test_a_mixed_group_takes_the_strictest_verdict_never_the_cleared_one(tmp_path: Path) -> None:
    """Rows sharing an entry can disagree when they sit at different install paths. Reporting
    such a group as Fixed would claim more than was done."""
    path = write_severity_shape(
        tmp_path / "mixed.xlsx",
        asset_ref(SANDBOX),
        [
            # Same CVE + package + version, two paths: one cleared by the esbuild override,
            # one an OS package with no named fix.
            row("CVE-9000-0016", "shared", "1.0", path="/workspace/app/node_modules/esbuild/x"),
            row("CVE-9000-0016", "shared", "1.0", path="/var/lib/dpkg/status"),
        ],
    )
    entries = build_entries(load_export(path))
    assert len(entries) == 1  # one entry, two rows
    assert entries[0].rows_accounted == 2
    assert entries[0].verdict.disposition is not Disposition.FIXED


# ─────────────────────────────────────────────────────────────────────────────
# Granularity and the partition invariant
# ─────────────────────────────────────────────────────────────────────────────


def test_one_cve_across_sibling_packages_and_paths_collapses_per_package(
    tmp_path: Path,
) -> None:
    """Plan AE6, with the real 9-row shape: one CVE, three sibling packages, three paths each.

    The report is per (image, CVE, package, version) and carries the ROW count, so the reviewer
    can reconcile against the row totals their console shows while reading a list of real
    findings rather than nine near-duplicates.
    """
    rows = [
        row("CVE-9000-0020", pkg, "2.41-12", path=p)
        for pkg in ("libc6", "libc-bin", "perl-base")
        for p in ("/var/lib/dpkg/status", "/usr/share/doc/a", "/usr/share/doc/b")
    ]
    path = write_severity_shape(tmp_path / "nine.xlsx", asset_ref(SANDBOX), rows)
    findings = load_export(path)
    entries = build_entries(findings)

    assert len(findings) == 9
    assert len(entries) == 3  # one entry per affected PACKAGE
    assert {e.software_name for e in entries} == {"libc6", "libc-bin", "perl-base"}
    assert all(e.rows_accounted == 3 for e in entries)
    assert sum(e.rows_accounted for e in entries) == 9
    # Every path is preserved on its entry — collapsing must not lose where it was found.
    assert all(len(e.install_paths) == 3 for e in entries)


def test_every_scanner_row_lands_in_exactly_one_entry(tmp_path: Path) -> None:
    """The partition invariant, checked at the row level via provenance."""
    rows = [row(f"CVE-9000-01{i:02d}", f"pkg{i % 4}", "1.0") for i in range(30)]
    path = write_severity_shape(tmp_path / "many.xlsx", asset_ref(SANDBOX), rows)
    findings = load_export(path)

    integrity = check_integrity(findings, build_entries(findings))

    assert integrity.ok, integrity.problems
    assert integrity.total_rows_in == 30
    assert integrity.total_rows_out == 30


def test_integrity_fails_loudly_when_a_row_is_dropped_or_duplicated(tmp_path: Path) -> None:
    """The check must actually be capable of failing — a partition test that cannot go red
    proves nothing. Both directions are exercised: a lost row and a double-counted one."""
    rows = [row(f"CVE-9000-02{i:02d}", "pkg", "1.0") for i in range(5)]
    path = write_severity_shape(tmp_path / "drop.xlsx", asset_ref(SANDBOX), rows)
    findings = load_export(path)
    entries = build_entries(findings)

    dropped = check_integrity(findings, entries[:-1])
    assert not dropped.ok
    assert any("landed in no entry" in p for p in dropped.problems)

    doubled = check_integrity(findings, [*entries, entries[0]])
    assert not doubled.ok
    assert any("more than once" in p for p in doubled.problems)


# ─────────────────────────────────────────────────────────────────────────────
# Reconciliation
# ─────────────────────────────────────────────────────────────────────────────


def _entries(
    tmp_path: Path,
    name: str,
    rows: list[dict[str, str]],
    digest: str,
    *,
    image: str = FRONTEND,
) -> list[Entry]:
    """Entries pinned to a specific asset DIGEST — which the normal factories cannot express.

    Defaults to the frontend image because these are reconciliation tests: they need rows the
    rule table genuinely clears, so that "predicted FIXED" is a real precondition rather than
    an accident. An apk row attributed to the sandbox image matches no rule and lands in the
    fallback, which would make the reconcile assertions vacuous.
    """
    wb_path = tmp_path / name
    wb = Workbook()
    if (default := wb.active) is not None:
        wb.remove(default)
    ws = wb.create_sheet("sheet")
    ws.append(_ASSET_HEADERS)
    for r in rows:
        ws.append(
            [
                r["cve"],
                "d",
                r["severity"],
                "New",
                asset_ref(image, digest),
                r["software"],
                r["version"],
                r["fixed"],
                "2026-08-01",
                r["manager"],
                "False",
            ]
        )
    wb.save(wb_path)
    return build_entries(load_export(wb_path))


def test_a_fix_that_did_not_land_is_reported_as_such_never_as_a_new_exception(
    tmp_path: Path,
) -> None:
    """Laundering a failed remediation into a defended one is the worst outcome available."""
    r = row("CVE-9000-0030", "libexpat1", "2.5.0-1", fixed="2.5.0-2", manager="apk")
    before = _entries(tmp_path, "b.xlsx", [r], DIGEST_OLD)
    after = _entries(tmp_path, "a.xlsx", [r], DIGEST_OLD)
    assert before[0].verdict.disposition is Disposition.FIXED  # predicted cleared

    rec = reconcile(before, after)

    assert len(rec.failed_to_clear) == 1
    assert rec.entries[0].verdict.disposition is Disposition.DEFERRED
    assert "STILL PRESENT" in rec.entries[0].verdict.reason


def test_a_residual_against_an_unshipped_digest_is_superseded_not_a_failed_fix(
    tmp_path: Path,
) -> None:
    """THE CASE THAT DECIDES THE REDUCTION CLAIM.

    A push untags the previous manifest without deleting it. If the scanner enumerates retained
    manifests, the old image keeps reporting its full finding set — and a reconcile that does
    not check the digest reads every one of those rows as 'predicted cleared, still present'
    and reports a successful remediation as a total failure.
    """
    r = row("CVE-9000-0031", "libexpat1", "2.5.0-1", fixed="2.5.0-2", manager="apk")
    before = _entries(tmp_path, "b2.xlsx", [r], DIGEST_OLD)
    after = _entries(tmp_path, "a2.xlsx", [r], DIGEST_OLD)  # scanner still on the OLD manifest

    assert before[0].verdict.disposition is Disposition.FIXED  # predicted cleared

    rec = reconcile(before, after, current_digests={FRONTEND: DIGEST_NEW})

    assert not rec.failed_to_clear, "an unshipped manifest must not read as a failed fix"
    assert len(rec.superseded) == 1
    entry = rec.entries[0]
    assert entry.verdict.disposition is Disposition.SUPERSEDED
    assert entry.replacing_digest == DIGEST_NEW
    assert DIGEST_OLD in entry.verdict.reason  # says WHICH artifact it measured


def test_without_current_digests_the_superseded_check_is_skipped_not_guessed(
    tmp_path: Path,
) -> None:
    """Absence of the digest must not silently change verdicts — it degrades to the honest
    reading (the fix did not land) rather than inventing a more flattering one."""
    r = row("CVE-9000-0032", "libexpat1", "2.5.0-1", fixed="2.5.0-2", manager="apk")
    before = _entries(tmp_path, "b3.xlsx", [r], DIGEST_OLD)
    after = _entries(tmp_path, "a3.xlsx", [r], DIGEST_OLD)

    rec = reconcile(before, after)

    assert not rec.superseded
    assert len(rec.failed_to_clear) == 1


def test_an_anticipated_addition_carries_its_reason_and_an_unanticipated_one_is_flagged(
    tmp_path: Path,
) -> None:
    """Installing git to fix the publish defect ADDS findings during a CVE remediation. Saying
    so plainly is the point; discovering it in the reviewer's console is not."""
    before: list[Entry] = []
    git_row = row("CVE-9000-0033", "git", "2.47.3", manager="deb")
    mystery_row = row("CVE-9000-0034", "who-put-this-here", "9.9", manager="deb")
    wb_path = tmp_path / "after4.xlsx"
    write_asset_shape(wb_path, {BACKEND: (asset_ref(BACKEND), [git_row, mystery_row])})
    after = build_entries(load_export(wb_path))

    rec = reconcile(before, after)

    by_pkg = {e.software_name: e for e in rec.entries}
    assert by_pkg["git"].verdict.disposition is Disposition.EXCEPTION
    assert "knowingly-accepted" in by_pkg["git"].verdict.reason.lower()
    assert len(rec.unanticipated) == 1
    assert "NOT ANTICIPATED" in by_pkg["who-put-this-here"].verdict.reason


# ─────────────────────────────────────────────────────────────────────────────
# Human annotations
# ─────────────────────────────────────────────────────────────────────────────


def test_a_rejected_dispute_falls_back_to_accepted_risk_rather_than_disappearing(
    tmp_path: Path,
) -> None:
    """A dispute round-trip sits on the critical path to approval and can come back negative.
    The rejected row must remain in the report with a stated reason — the failure mode being
    guarded against is a rejected dispute quietly vanishing from the totals."""
    entry = _one(
        tmp_path,
        FRONTEND,
        row("CVE-9000-0040", "libssl3", "3.3.9-r0", fixed="3.3.7-r0", manager="apk"),
    )
    assert entry.verdict.disposition is Disposition.DISPUTE

    unmatched = apply_overrides(
        [entry],
        [
            Override(
                image=FRONTEND,
                cve_id="CVE-9000-0040",
                software_name="libssl3",
                disposition=Disposition.DISPUTE_REJECTED,
                reason="Scan owner rejected the dispute on 2026-09-01; accepted as risk.",
            )
        ],
    )

    assert unmatched == []
    # Re-read through a fresh local: `apply_overrides` REPLACES `entry.verdict`, which the type
    # checker cannot see, so asserting on the same attribute expression twice reads as a
    # contradiction rather than as a before/after.
    annotated = entry.verdict
    assert annotated.disposition is Disposition.DISPUTE_REJECTED
    assert "rejected" in annotated.reason.lower()


def test_an_override_that_matches_nothing_is_reported_not_ignored(tmp_path: Path) -> None:
    """The realistic mistake is a typo in a CVE id, and the symptom of ignoring it is a report
    silently missing the exact row somebody went to the trouble of annotating."""
    entry = _one(tmp_path, FRONTEND, row("CVE-9000-0041", "libssl3", "3.3.3-r0", manager="apk"))

    unmatched = apply_overrides(
        [entry],
        [Override(image=FRONTEND, cve_id="CVE-9000-9999", software_name="libssl3")],
    )

    assert unmatched == [f"{FRONTEND}/CVE-9000-9999/libssl3"]


def test_an_override_supplies_the_owner_and_date_a_deferral_requires(tmp_path: Path) -> None:
    entry = _one(
        tmp_path,
        FRONTEND,
        row("CVE-9000-0042", "some-lib", "1.0.0", fixed="1.0.1", manager="cargo"),
    )
    assert entry.verdict.owner == "UNASSIGNED"

    apply_overrides(
        [entry],
        [
            Override(
                image=FRONTEND,
                cve_id="CVE-9000-0042",
                software_name="some-lib",
                owner="Platform team",
                target_date="2026-10-31",
                vendor_status="Under investigation",
                tracker_url="https://example.invalid/tracker/1",
            )
        ],
    )

    assert entry.verdict.disposition is Disposition.DEFERRED  # unchanged — only annotated
    assert entry.verdict.owner == "Platform team"
    assert entry.verdict.target_date == "2026-10-31"
    assert entry.tracker_url == "https://example.invalid/tracker/1"


def test_an_unknown_disposition_in_the_annotations_file_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "overrides.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "image": FRONTEND,
                        "cve_id": "CVE-9000-0043",
                        "software_name": "libssl3",
                        "disposition": "not-affected",  # the one word this report may never use
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown disposition"):
        load_overrides(path)


def test_a_missing_required_field_in_the_annotations_file_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"entries": [{"image": FRONTEND, "cve_id": "CVE-9000-0044"}]}))
    with pytest.raises(KeyError):
        load_overrides(path)


# ─────────────────────────────────────────────────────────────────────────────
# The workbook
# ─────────────────────────────────────────────────────────────────────────────

_SUMIFS = re.compile(r"SUMIFS\(([^()]*)\)")
_REF = re.compile(r"'([^']+)'!\$([A-Z]+):\$[A-Z]+")


def _eval_sumifs(wb: Workbook, formula: str) -> int:
    """Evaluate the SUMIFS terms this generator writes, so 'derived' can actually be proven.

    openpyxl stores formulas, it does not compute them — so a test that only asserted the cell
    holds a string starting with `=` would prove the cell is A formula, not that it is the RIGHT
    one. This computes the formula against the written sheets, which is what makes
    'Summary moves when a sheet moves' a real assertion rather than a shape check.
    """
    total = 0
    for args_text in _SUMIFS.findall(formula):
        args = [a.strip() for a in args_text.split(",")]
        sum_match = _REF.match(args[0])
        assert sum_match, args[0]
        sum_sheet, sum_col = sum_match.group(1), sum_match.group(2)
        criteria: list[tuple[str, str]] = []
        for i in range(1, len(args) - 1, 2):
            ref = _REF.match(args[i])
            assert ref, args[i]
            criteria.append((ref.group(2), args[i + 1].strip('"')))
        ws = wb[sum_sheet]
        for r in range(2, (ws.max_row or 1) + 1):
            if all(str(ws[f"{col}{r}"].value or "") == want for col, want in criteria):
                total += int(ws[f"{sum_col}{r}"].value or 0)
    return total


def _build(tmp_path: Path, entries: list[Entry], before_rows: dict[str, int]) -> Path:
    wb = build_workbook(
        entries,
        before_rows=before_rows,
        after_rows=dict.fromkeys(before_rows),
        digests={SANDBOX: DIGEST_OLD},
        generated_at="2026-08-13 00:00 UTC",
    )
    out = tmp_path / "register.xlsx"
    wb.save(out)
    return out


def test_the_workbook_partitions_every_row_across_the_sheets(tmp_path: Path) -> None:
    """Every row from the original list appears in exactly ONE sheet, and the accounted counts
    sum back to the scanner's own total. This is the report's self-check."""
    rows = [
        row("CVE-9000-0050", "libexpat1", "2.5.0-1", fixed="2.5.0-2"),  # fixed by the base move
        row("CVE-9000-0051", "libsystemd0", "252.39-1", fixed="-"),  # held (debian, no fix)
        row("CVE-9000-0052", "curl", "7.88.1-10", fixed="-"),  # excepted (kept tooling)
        row(
            "CVE-9000-0053", "stdlib", "go1.20.7", manager="go-module", path="/usr/local/bin/caddy"
        ),
    ]
    src = write_severity_shape(tmp_path / "src.xlsx", asset_ref(SANDBOX), rows)
    findings = load_export(src)
    entries = build_entries(findings)
    out = _build(tmp_path, entries, {SANDBOX: len(findings)})

    wb = openpyxl.load_workbook(out)
    content = ["Fixed", "Exceptions", "Deferred", "Disputes", "Out of scope", "Held"]
    assert wb.sheetnames[0] == "Summary"  # the sheet most readers will open
    assert set(content).issubset(set(wb.sheetnames))

    seen: list[tuple[str, str]] = []
    accounted = 0
    for name in content:
        ws = wb[name]
        for r in range(2, (ws.max_row or 1) + 1):
            if ws[f"A{r}"].value is None:
                continue
            seen.append((str(ws[f"B{r}"].value), str(ws[f"D{r}"].value)))
            accounted += int(ws[f"J{r}"].value or 0)

    assert len(seen) == len(set(seen)), "a finding appears in more than one sheet"
    assert accounted == len(findings), "the sheets do not sum back to the scanner's row total"


def test_summary_is_derived_from_the_sheets_and_moves_when_they_do(tmp_path: Path) -> None:
    """`Summary` must be a formula over the other sheets, never typed.

    Proven in two steps, because either alone is weak: the cell must BE a formula (a typed
    literal fails here), and evaluating that formula against a MUTATED sheet must produce the
    mutated number (a formula pointing at the wrong range fails here).
    """
    rows = [
        row(f"CVE-9000-006{i}", "libexpat1", f"2.5.0-{i}", fixed=f"2.5.0-{i + 1}")
        for i in range(4)
    ]
    src = write_severity_shape(tmp_path / "s.xlsx", asset_ref(SANDBOX), rows)
    findings = load_export(src)
    out = _build(tmp_path, build_entries(findings), {SANDBOX: len(findings)})

    wb = openpyxl.load_workbook(out)
    summary = wb["Summary"]
    header_row = next(
        r for r in range(1, 30) if str(summary[f"A{r}"].value or "").strip() == "Image"
    )
    fixed_cell = summary.cell(row=header_row + 1, column=6)

    # (1) It is a formula, not a number somebody typed.
    assert isinstance(fixed_cell.value, str) and fixed_cell.value.startswith("=SUMIFS("), (
        f"Summary!F{header_row + 1} is typed, not derived: {fixed_cell.value!r}"
    )

    # (2) Evaluating it reproduces the sheet, and TRACKS the sheet when the sheet changes.
    before = _eval_sumifs(wb, fixed_cell.value)
    assert before == len(findings)

    wb["Fixed"]["J2"] = int(wb["Fixed"]["J2"].value or 0) + 7  # hand-edit one row
    after = _eval_sumifs(wb, fixed_cell.value)
    assert after == before + 7, "Summary did not move with the sheet it claims to summarise"


def test_held_rows_never_leak_into_a_residual_sheet_they_do_not_belong_to(
    tmp_path: Path,
) -> None:
    """Held rows are counted and visible, but they must not be laundered into Exceptions —
    that would assert a defence we have not established."""
    rows = [row(f"CVE-9000-007{i}", f"libpkg{i}", "1.0", fixed="-") for i in range(3)]
    src = write_severity_shape(tmp_path / "h.xlsx", asset_ref(SANDBOX), rows)
    findings = load_export(src)
    out = _build(tmp_path, build_entries(findings), {SANDBOX: len(findings)})

    wb = openpyxl.load_workbook(out)
    held = wb["Held"]
    assert (held.max_row or 1) - 1 == 3
    for name in ("Exceptions", "Deferred", "Disputes", "Out of scope", "Fixed"):
        ws = wb[name]
        values = [ws[f"K{r}"].value for r in range(2, (ws.max_row or 1) + 1)]
        assert str(Disposition.HELD) not in [str(v) for v in values]


def test_an_image_that_was_never_scanned_carries_an_after_only(tmp_path: Path) -> None:
    """The deployed-app image has no 'before' — its Summary row must SAY so rather than show a
    blank that reads as zero findings."""
    rows = [row("CVE-9000-0080", "libexpat1", "2.5.0-1", fixed="2.5.0-2")]
    src = write_severity_shape(tmp_path / "n.xlsx", asset_ref(SANDBOX), rows)
    entries = build_entries(load_export(src))
    wb = build_workbook(
        entries,
        before_rows={SANDBOX: 1},
        after_rows={SANDBOX: None},
        digests={},
        generated_at="2026-08-13 00:00 UTC",
    )
    out = tmp_path / "never.xlsx"
    wb.save(out)

    loaded = openpyxl.load_workbook(out)["Summary"]
    header_row = next(
        r for r in range(1, 30) if str(loaded[f"A{r}"].value or "").strip() == "Image"
    )
    assert loaded.cell(row=header_row + 1, column=4).value == "never scanned"
    assert loaded.cell(row=header_row + 1, column=2).value == "not scanned"


def test_no_finding_is_ever_labelled_not_affected(tmp_path: Path) -> None:
    """A guard on the vocabulary itself. 'Not affected' is the one claim this report may never
    make about a fixable finding, and it is the easiest one to reach for by accident."""
    assert "not-affected" not in {d.value for d in Disposition}
    rows = [
        row("CVE-9000-0090", "a", "1.0", fixed="2.0"),
        row("CVE-9000-0091", "b", "1.0", fixed="-"),
        row("CVE-9000-0092", "c", "1.0", fixed="9.9.9a1"),
    ]
    src = write_severity_shape(tmp_path / "v.xlsx", asset_ref(SANDBOX), rows)
    for entry in build_entries(load_export(src)):
        assert "not affected" not in entry.verdict.reason.lower()
        assert "not-affected" not in str(entry.verdict.disposition)


def test_disposition_for_is_total_every_row_gets_exactly_one(tmp_path: Path) -> None:
    """No row may fall through the rule table unmapped — 'every row carries a disposition' is
    the definition of done the whole engagement is measured against."""
    shapes = [
        row("CVE-9000-0100", "weird", "1.0", manager="conda", kind="Application", path="/opt/x"),
        row("CVE-9000-0101", "", "", fixed="", manager="", kind="", path=""),
        row("CVE-9000-0102", "nginx", "1.27.5-r1", manager="apk"),
    ]
    src = write_severity_shape(tmp_path / "t.xlsx", asset_ref(SANDBOX), shapes)
    for finding in load_export(src):
        verdict = disposition_for(finding)
        assert isinstance(verdict.disposition, Disposition)
        assert verdict.reason
        assert verdict.rule
