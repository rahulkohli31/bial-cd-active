"""The server never saves at send time, and no approved state waits for a second click.

Saving before the review moved to the portal, and approving publishes on its own, so the
server-side save, the re-check that followed it and the two approved states were deleted whole.
Their vocabulary is searched for here so a sentence or a field naming them cannot come back
unnoticed. The portal's half of this guard is `portal/src/__tests__/jsx-deploy-retirement.test.ts`.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[5]

# The generated API reference is covered by its own drift check against the routes.
_TREES = (("backend/src", {".py"}), ("documentation", {".md"}))

_RETIRED = re.compile(
    "|".join(
        (
            r"\bsave_?first\b",
            r"\bunsaved_changes\b",
            r"save and deploy",
            r"save and publish",
            r"VersionRecheck",
            r"VersionReviewer",
            r"\bSTEP_CHECKING\b",
            r"\broute_refused\b",
            r"approved_ready_to_publish",
            r"approved_needs_review_again",
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
                    found.append(f"{path.relative_to(_ROOT).as_posix()}:{number}: {match[0]}")
    return found


def test_no_tree_names_the_save_at_send_path_or_the_approved_states() -> None:
    assert _hits() == []


def test_the_guard_can_actually_fail() -> None:
    """Without this, a wrong root or a pattern that matches nothing would pass forever."""
    assert all((_ROOT / tree).is_dir() for tree, _suffixes in _TREES)
    for retired in (
        "saveFirst",
        "unsaved_changes",
        "Save and deploy",
        "approved_ready_to_publish",
    ):
        assert _RETIRED.search(retired), retired
    assert not _RETIRED.search("discard_unsaved_changes")
