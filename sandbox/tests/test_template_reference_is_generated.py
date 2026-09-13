"""The golden template's flight-data reference file must still match the source it came from.

WHY THIS EXISTS. `sandbox/template/lib/flight-data.reference.ts` is a rendering of
`sandbox/seed/flight-data.ts`: the head block, then every source line behind a `// `. The source
lives outside the template because the template deliberately pre-installs none of the four
packages the example imports, so a live import there would not compile and an app that reads no
flight data would stop being byte-identical to today. That split buys real verification — the
source is typechecked under the template's own `strict` settings and unit-tested next door — and
it costs exactly one thing: a generator whose output nobody regenerates drifts into a lie that
still passes. This file is what stops that.

The second failure it guards is sharper. The template's `lint` script IS `tsc --noEmit` across the
whole tree, so a reference file that failed to comment out cleanly would fail EVERY citizen's lint
at once, in apps that never asked for flight data. An earlier hand-written draft did exactly that:
it was wrapped in one `/* ... */` block and the JSDoc blocks inside closed the wrapper early.

WHICH IMPLEMENTATION OF THE COMMENTING RULE THIS TEST TRUSTS, AND WHY. `generate-reference.mjs` is
the only implementation. This test does NOT re-derive the rule in Python: two implementations of
one rule drift, and the drift would be invisible precisely because both sides would still agree
with themselves. It shells out to `node generate-reference.mjs --stdout`, which writes the exact
bytes it would write to the file and touches nothing, and compares. That needs `node` and nothing
else — no Docker, no `npm install`, no `node_modules` (the generator imports only `node:fs`,
`node:path` and `node:url`). A missing `node` is a clean skip, as with Docker elsewhere in this
harness.

The cost of that skip is covered rather than accepted: the structural checks below run
unconditionally on the shipped bytes alone and assert the property that actually breaks other
people's builds — every body line is a line comment, and there is no block-comment wrapper
anywhere. So a box with no Node still catches the failure that would ship; only the
currency comparison waits for a box that has one.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SANDBOX = Path(__file__).resolve().parent.parent
GENERATOR = _SANDBOX / "seed" / "generate-reference.mjs"
SOURCE = _SANDBOX / "seed" / "flight-data.ts"
#: The path the golden-template manifest in the agent's prompt blocks names.
REFERENCE = _SANDBOX / "template" / "lib" / "flight-data.reference.ts"

#: The seven defects the file exists to name. Each produces an app that builds green and is wrong.
MISTAKES = 7


def _shipped() -> str:
    """The reference file's bytes, decoded without newline translation.

    `newline=""` matters: Python's universal-newline decoding would turn a CRLF checkout into
    `\\n` and hide exactly the defect the root `.gitattributes` exists to prevent.
    """
    return REFERENCE.read_text(encoding="utf-8", newline="")


def test_the_reference_file_is_shipped_at_all() -> None:
    """It is unconditional — every workspace gets it, whether or not the connector is on. That is
    what makes the one manifest line in the agent's prompt blocks unconditionally true."""
    assert REFERENCE.is_file()
    assert SOURCE.is_file()
    assert GENERATOR.is_file()


def test_every_body_line_is_a_line_comment() -> None:
    """The property that fails every citizen's lint at once if it is ever false.

    Runs on the shipped bytes alone — no Node, no generator, no source. A blank line is `//` with
    no trailing space, so no line of the file ends in whitespace either.
    """
    offenders = [
        (number, line)
        for number, line in enumerate(_shipped().splitlines(), start=1)
        if line != "//" and not line.startswith("// ")
    ]
    assert offenders == [], (
        "sandbox/template/lib/flight-data.reference.ts must be entirely line comments; "
        "the template's lint is `tsc --noEmit` across the whole tree, so a line that parses as "
        f"code fails the lint of every generated app: {offenders[:5]}"
    )


def test_the_hazard_the_line_comments_exist_for_is_still_in_the_body() -> None:
    """Mutation-proofing for the test above, and the reason the generator exists at all.

    "Every line is a comment" is a cheap assertion to satisfy accidentally — a file of prose
    would pass it. What makes the rule load-bearing is that the body really does carry JSDoc
    blocks: the first draft of this file was hand-written as ONE `/* ... */` wrapper, those
    blocks closed it early, and everything after the first one parsed as code. So assert the
    hazard is present and that nothing opens or closes a block at the start of a line. If the
    body ever stops carrying `/**`, this test is the one that says the trap moved, rather than
    the guard quietly protecting nothing.
    """
    shipped = _shipped()
    assert "/**" in shipped and "*/" in shipped, (
        "the body no longer carries JSDoc blocks — check whether the line-comment rule is still "
        "guarding anything, and whether the head still explains why it exists"
    )
    starts = [line for line in shipped.splitlines() if line.lstrip().startswith(("/*", "*/", "*"))]
    assert starts == [], f"a block comment has crept back in: {starts[:5]}"


def test_the_head_says_where_it_came_from_and_what_to_install() -> None:
    """A generated file that does not say it is generated gets hand-edited, once, by someone who
    had no way to know. The install line is the other half: none of the four packages is in the
    golden image, so the example does not compile until the reader runs it."""
    # The head is everything down to the second box rule; the body starts after it.
    lines = _shipped().splitlines()
    rules = [n for n, line in enumerate(lines) if line.startswith("// \u2500")]
    assert len(rules) >= 2, "the head block's box rules are gone"
    head = "\n".join(lines[: rules[1] + 1])
    assert "GENERATED FILE" in head
    assert "sandbox/seed/flight-data.ts" in head
    assert "node sandbox/seed/generate-reference.mjs" in head
    assert (
        "npm install hyparquet hyparquet-compressors @azure/identity @azure/storage-blob" in head
    )


def test_the_seven_mistakes_all_survive_into_the_shipped_file() -> None:
    """Mutation-proofing for the comparison below. A generator that emitted an empty body, or a
    head with nothing after it, would still be "current" — this is what makes that fail. The
    specificity is the whole value of the file: a reader who is told to trim their text changes
    nothing, and a reader who is told this airline is stored twice, once padded, changes their
    chart."""
    shipped = _shipped()
    numbered = sorted(int(n) for n in re.findall(r"MISTAKE (\d+)", shipped))
    assert numbered == list(range(1, MISTAKES + 1)), (
        f"expected MISTAKE 1..{MISTAKES} named exactly once each, found {numbered}"
    )
    for landmark in (
        "SIBT_SOBT_TIME",  # the merged scheduled time, the only safe date column
        "SCHEDULED_OFF_BLOCK_TIME_SOBT",  # null on every arrival row
        "AODB_AFTTAB_PK_URNO",  # the flight key the amendment rows collapse on
        "AKASA AIR",  # stored twice, once padded
        "tb_flight_fact_report_",  # the real object-name pattern
        "revalidate: 3600",  # an hour, not a week
        "ManagedIdentityCredential",  # never DefaultAzureCredential
    ):
        assert landmark in shipped, f"the shipped example no longer names {landmark!r}"


def test_the_memory_warning_names_both_container_limits() -> None:
    """MISTAKE 4 is a memory bug wearing a performance costume: a wide read builds green in the
    2 GiB sandbox and is OOM-killed in the 1 GiB published container, with no exception and
    nothing in the logs. "Slower" is not a warning anyone acts on, so the numbers are the
    control — and a rewrite that drops one of them is the failure this catches."""
    shipped = _shipped()
    assert "2 GiB" in shipped and "1 GiB" in shipped
    assert "KILLED" in shipped.upper()


def test_the_file_carries_no_carriage_returns() -> None:
    """The root `.gitattributes` carries `sandbox/** text eol=lf` and is the control; this is the
    assertion that the control held. The image is built on a Windows VM, and a CRLF checkout of a
    shipped file has already broken container start on this project once."""
    assert "\r" not in _shipped()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not available")
def test_the_shipped_file_is_a_current_generation_of_its_source() -> None:
    """Byte for byte, against the generator's own output — the only implementation of the rule.

    If this fails, the fix is `node sandbox/seed/generate-reference.mjs`, and then a look at what
    the source changed: the file is verified only because its source is typechecked and tested,
    so a regeneration is the end of the job and not the whole of it.
    """
    proc = subprocess.run(
        ["node", str(GENERATOR), "--stdout"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(GENERATOR.parent),
    )
    assert proc.returncode == 0, f"the generator did not run:\n{proc.stderr[-3000:]}"
    assert proc.stdout == _shipped(), (
        "sandbox/template/lib/flight-data.reference.ts is stale — regenerate it with "
        "`node sandbox/seed/generate-reference.mjs`. It is generated from "
        "sandbox/seed/flight-data.ts and must never be hand-edited."
    )


def test_the_agents_template_manifest_names_the_file() -> None:
    """Without this line the agent is never told the file exists and every other cost in this
    unit buys nothing. The manifest is hard-coded to mirror `sandbox/template/`, so a file added
    here and not there is a file nobody reads. Imported through the read-only `sys.path` bridge
    `conftest.py` sets up; `prompt_blocks` is a leaf module and pulls in no Settings or DB."""
    from src.core.prompt_blocks import _GOLDEN_TEMPLATE_MANIFEST

    assert REFERENCE.name in _GOLDEN_TEMPLATE_MANIFEST
