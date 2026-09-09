"""R18's checkable form, as a test rather than a sentence in a docblock.

THE RULE. The platform is generic by name and specific only by value: connectors are the first of
several integrations, so no table, route, enum, audit action, schema field or component carries
the word DICE. It appears only as a value of `connector_key` and as the literals on one registry
entry, which is what makes "add a second connector" a registry entry plus its board copy rather
than a migration, a route and a component change.

WHY THIS EXISTS AS A TEST. `src/core/connectors.py` states the rule and even spells out the
command that checks it — and nothing ran it. A rule whose enforcement is a sentence asking a
reviewer to remember a grep is enforced until the first tired evening. It broke twice while this
feature was being built: once when a docblock quoted a consent line verbatim, once when a comment
explained whose noun `data_noun` holds. Both were caught by hand, which is the point: by hand is
not a guard.

WORD BOUNDARY, NOT SUBSTRING. A bare `dice` also matches `indices`, which appears in
`portal/src/components/chat/ActivityGroup.tsx` and has nothing to do with connectors.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WORD = re.compile(r"\bdice\b", re.IGNORECASE)
#: The one module allowed to name the connector, relative to the repo root.
_THE_CATALOGUE = "backend/src/core/connectors.py"
_TREES = ("backend/src", "portal/src")
_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}


def _offenders() -> list[str]:
    hits: list[str] = []
    for tree in _TREES:
        for path in sorted((_ROOT / tree).rglob("*")):
            if path.suffix not in _SUFFIXES or not path.is_file():
                continue
            relative = path.relative_to(_ROOT).as_posix()
            if relative == _THE_CATALOGUE:
                continue
            if _WORD.search(path.read_text(encoding="utf-8", errors="ignore")):
                hits.append(relative)
    return hits


def test_the_connector_is_named_in_exactly_one_module() -> None:
    """Everything but the catalogue is generic. A new hit here is not a style nit: it is the
    moment a second connector stops being a registry entry and starts being a code change."""
    assert _offenders() == []


def test_the_guard_can_actually_fail() -> None:
    """Mutation-proofing the assertion above. If the search were broken — a wrong root, a regex
    that matches nothing — the check would pass forever while the rule rotted. So prove the
    catalogue itself WOULD be reported if it were not the exemption, and that the word-boundary
    rule really does spare `indices`."""
    catalogue = (_ROOT / _THE_CATALOGUE).read_text(encoding="utf-8")
    assert _WORD.search(catalogue), (
        "the catalogue must name the connector, or the search is broken"
    )
    assert not _WORD.search("group.indices.map(...)"), "a substring search would match `indices`"
