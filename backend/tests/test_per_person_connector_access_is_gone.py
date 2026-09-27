"""Connector access is the project's switch and nothing else — as a test, not a sentence.

WHY THIS EXISTS. A per-person access flow once sat in front of the switch: a request, an
administrator's decision, a queue and a page of its own. It was deleted whole, and a deletion that
wide leaves its vocabulary behind in docblocks, prose and wire comments nobody re-reads. A stale
sentence describing an approval step that no longer exists misleads a reader exactly as a stale
branch would, so the trees are searched for the names that only that flow used.

UNAMBIGUOUS NAMES ONLY. A bare `approver` or `declined` is live vocabulary elsewhere — the app
review has approvers — and would make this guard noise. Every entry below meant the deleted flow
and nothing else.
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
            r"access[ _-]?requests?",
            r"never_?asked",
            r"pending approval",
            r"requester consent",
            r"consent[_ ]?lines",
            r"ask[_ ]?subtitle",
            r"access_not_approved",
            r"connector_request_status",
            r"ConnectorRequestStatus",
            r"connector-requests",
            r"ConnectorAccess",
            r"ConnectorPersonState",
            r"PersonAccess",
            r"current_access",
            r"owner_access_state",
            r"AskAccess",
            r"IntegrationsPanel",
            r"IntegrationsPage",
            r"adminConnectorApi",
            r"ConnectorReviewDialog",
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


def test_no_tree_names_the_per_person_access_flow() -> None:
    assert _hits() == []


def test_the_guard_can_actually_fail() -> None:
    """Without this, a wrong root or a regex that matches nothing would pass forever."""
    assert all((_ROOT / tree).is_dir() for tree, _suffixes in _TREES)
    for retired in ("an access request", "neverAsked", "NEVER_ASKED", "IntegrationsPage"):
        assert _RETIRED.search(retired), retired
    for live in ("the administrator approves the submitted copy", "a step nobody ever asked for"):
        assert not _RETIRED.search(live), live
