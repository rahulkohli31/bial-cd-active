"""Approval is the only way a reviewed app goes live — as a test, not a sentence.

WHY THIS EXISTS. A manual go-live route once sat beside approval: an administrator ran a runbook,
recorded the deployment and pasted the live address by hand, and every submission carried which of
the two routes it had entered through. It was deleted whole, and a deletion that wide leaves its
vocabulary behind in docblocks, wire types and prose nobody re-reads, so the trees are searched
for the names that only that route used.

UNAMBIGUOUS NAMES ONLY. A bare `route` or `deployed` is live vocabulary elsewhere, and would make
this guard noise. Every entry below meant the deleted route and nothing else.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Code in both trees, and the published documentation. The generated API reference is covered by
# its own drift check against the routes, so it is not read here.
_TREES = (
    ("backend/src", {".py"}),
    ("portal/src", {".ts", ".tsx", ".js", ".jsx"}),
    ("documentation", {".md"}),
)

_RETIRED = re.compile(
    "|".join(
        (
            r"approval_?route",
            r"mark[_ -]?deployed",
            r"deployed_?submission_?id",
            r"deployed_?url",
            r"deployed_?at(?![a-z])",
            r"redeploy_?needed",
            r"approved-app-go-live",
            r"runbook[ -]lineage",
            r"self_publish",
        )
    ),
    re.IGNORECASE,
)


def _hits() -> list[str]:
    found: list[str] = []
    for tree, suffixes in _TREES:
        for path in sorted((_ROOT / tree).rglob("*")):
            if path.suffix not in suffixes or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for number, line in enumerate(text.splitlines(), start=1):
                match = _RETIRED.search(line)
                if match:
                    relative = path.relative_to(_ROOT).as_posix()
                    found.append(f"{relative}:{number}: {match.group(0)}")
    return found


def test_no_tree_names_the_manual_go_live_route() -> None:
    assert _hits() == []


def test_the_guard_can_actually_fail() -> None:
    """Without this, a wrong root or a regex that matches nothing would pass forever."""
    assert all((_ROOT / tree).is_dir() for tree, _suffixes in _TREES)
    for retired in (
        "ApprovalRoute",
        "approval_route",
        "markDeployed",
        "mark-deployed",
        "deployedSubmissionId",
        "deployedUrl",
        "MAX_DEPLOYED_URL",
        "deployed_at",
        "deployedAt",
        "redeployNeeded",
        "a runbook-lineage row",
        "SELF_PUBLISH",
    ):
        assert _RETIRED.search(retired), retired
    for live in (
        "the administrator approves the submitted copy",
        "the deployed app's address",
        "a self-published marketplace entry",
        "one live deployment per app",
        "the app deployed at its address",
        "Self Published Tool",
    ):
        assert not _RETIRED.search(live), live
