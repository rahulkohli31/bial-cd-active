"""The cross-language pin: every ending the server can store has copy on the other side.

WHY A TEST AND NOT A TYPE. The TypeScript half is genuinely compiled — `OUTCOME_COPY` is total
over `EndReason` and `outcomeSummary` ends in `assertNever`, so a member added there without a
sentence does not build. Nothing compels the reverse: a backend author adding a reason has no
compiler pointing at `portal/src/utils/messageTypes.ts`, and that is exactly how three endings
shipped with no copy and reached citizens as "The build failed." — the last of them over a
working dashboard.

TWO SOURCES, DELIBERATELY. The Python side is IMPORTED — `END_REASONS`, the collection every
producer's reason is spelled out of — and the TypeScript side is READ OFF THE FILE. Generating
both from one source would make a mutation agree with itself; reading the TS side is the same
shape `tests/services/build_sessions/test_integrity.py` uses to pin a Python constant against a
literal embedded in a shell script.

`build_sessions/outcome.py` KEEPS ITS OWN LITERALS and is pinned here rather than importing the
collection: `src/services/turns/__init__` imports the turn engine and the engine imports
`build_sessions.manager`, so a module-level import of anything under `turns` from that package is
a cycle. The session-end producer is therefore covered by assertion rather than by construction,
which is what this file is for.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from src.services.build_sessions import outcome as outcome_module
from src.services.turns.copy import (
    CHAT_TOO_LONG_CODE,
    DOCUMENT_TOO_LONG_CODE,
    END_REASONS,
    MODEL_UNAVAILABLE_CODE,
)

_MESSAGE_TYPES: Final = (
    Path(__file__).resolve().parents[4] / "portal" / "src" / "utils" / "messageTypes.ts"
)

_UNION_HEAD: Final = "export type EndReason ="
_MEMBER: Final = re.compile(r"^\s*\|\s*'([A-Za-z_]+)'\s*$")


def _union_members() -> set[str]:
    """The `EndReason` union, read off the portal's own source.

    Parsed line by line rather than with one regex over the whole file: the union is a run of
    `| 'member'` lines and stops at the first line that is not one, which is a rule a reader can
    check against the file. A regex spanning the declaration would quietly swallow whatever
    followed it if the formatting ever changed."""
    source = _MESSAGE_TYPES.read_text()
    assert _UNION_HEAD in source, f"{_MESSAGE_TYPES} no longer declares an `EndReason` union"
    members: set[str] = set()
    after = source.split(_UNION_HEAD, 1)[1]
    for line in after.splitlines():
        if not line.strip():
            continue
        found = _MEMBER.match(line)
        if found is None:
            break
        members.add(found.group(1))
    return members


def _copy_table_keys() -> set[str]:
    """The keys of `OUTCOME_COPY`, which the TypeScript compiler already holds equal to the
    union. Read anyway: this test is the only thing that would notice the annotation being
    widened back to `Record<string, string | undefined>`, which is what it was."""
    source = _MESSAGE_TYPES.read_text()
    head = "export const OUTCOME_COPY: Readonly<Record<EndReason, string>> = {"
    assert head in source, (
        "`OUTCOME_COPY` is no longer declared total over `EndReason` — the compile-time half of "
        "this guard has been removed"
    )
    body = source.split(head, 1)[1].split("\n}", 1)[0]
    return set(re.findall(r"^  ([A-Za-z_]+):", body, re.M))


def test_the_portal_names_every_ending_the_server_can_store() -> None:
    """★ THE PIN. Both directions, and the failure names which side is short.

    Mutation check: delete `context_hard_limit_exceeded` from the union in `messageTypes.ts` and
    this goes red naming it; the same for `model_unavailable`, whose producer is
    `_end_model_unavailable` rather than a `_WriteEndedError` raise; the same for
    `idle_teardown`, whose producer is the session end in `build_sessions`. Those three are the
    assertions an enumeration built from the exception class alone could not make."""
    union = _union_members()
    assert union, "no `EndReason` members parsed — the reader, not the union"
    missing = sorted(END_REASONS - union)
    assert missing == [], f"the server can store endings the portal has no sentence for: {missing}"
    invented = sorted(union - END_REASONS)
    assert invented == [], f"the portal names endings no producer can store: {invented}"


def test_the_copy_table_is_total_over_the_union() -> None:
    """The compile-time half, asserted from here as well because a widened annotation would take
    it away silently — and `Record<string, string | undefined>` is precisely what it was."""
    assert _copy_table_keys() == _union_members()


@pytest.mark.parametrize(
    "reason",
    [CHAT_TOO_LONG_CODE, DOCUMENT_TOO_LONG_CODE, MODEL_UNAVAILABLE_CODE],
    ids=["chat_too_long", "document_too_long", "model_unavailable"],
)
def test_the_direct_assignment_reasons_are_in_the_collection(reason: str) -> None:
    """The three reasons that never pass through `_WriteEndedError`: two named refusals and the
    model-unavailable ending assign `state.end_reason` directly. A union derived from the
    exception class would be green while every one of them still reproduced the bug on reload."""
    assert reason in END_REASONS


def test_the_session_end_producer_is_in_the_collection() -> None:
    """`build_sessions/outcome.py`'s four tokens — the stop, the force-end, the idle reap and the
    spent daily budget — reach `end_reason` through `manager.py`'s session end, which is a
    producer in a different package from every other one.

    Mutation check: change any one of those literals in `outcome.py` and this goes red. That is
    what stands in for the import the module cannot make."""
    assert {
        outcome_module.STOPPED_BY_USER,
        outcome_module.FORCE_ENDED,
        outcome_module.IDLE_TEARDOWN,
        outcome_module.QUOTA_EXCEEDED,
    } <= END_REASONS


def test_the_uppercase_reason_is_kept_as_it_is_stored() -> None:
    """★ AN ODD-LOOKING LITERAL THAT IS CORRECT. Every other reason is lowercase; this one is the
    provider's own token and rows already in the database carry it. Normalising it would make
    every one of those rows render the generic sentence — the exact defect this whole unit
    closes, introduced by tidying."""
    assert DOCUMENT_TOO_LONG_CODE == "DOCUMENT_TOO_MANY_PAGES"
    assert DOCUMENT_TOO_LONG_CODE in END_REASONS
