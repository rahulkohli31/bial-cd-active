"""What the template publishes to the BROWSER, and the names that must never join it.

WHY THIS EXISTS NOW. The platform injects more into a sandbox than it used to — a per-app Blob
SAS, a per-project database DSN, and now a data-lake URL, a managed identity's client id and
Azure's own token-endpoint pair. Every one of those is read SERVER-SIDE from `process.env`, and
the browser-facing object in `app/layout.tsx` has stayed at exactly two non-secret labels
throughout. That is a property nobody was checking.

It is the kind of boundary that erodes by accident rather than by decision: a citizen debugging a
"why can't my chart see the flight data" problem adds one line to `readBialConfig` to look at it
in the console, and the value is then in the page source of a running app. The two currently
published values are LABELS — an app id and the portal's own origin — and the rule is that the
list does not grow without someone changing this test on purpose.

TEXT, NOT EVALUATION, and deliberately so — unlike `test_template_next_config.py`, which
evaluates because it is asserting what a config RESOLVES TO. The question here is which names
appear in one function's source, which is exactly a text question; evaluating the layout would
need React, a build, and a running server for a fact that is visible in six lines.
"""

from __future__ import annotations

import re
from pathlib import Path

_TEMPLATE = Path(__file__).resolve().parent.parent / "template"
LAYOUT = _TEMPLATE / "app" / "layout.tsx"
CONFIG_TYPE = _TEMPLATE / "lib" / "bial-config.ts"

#: The names that are injected into a sandbox and MUST stay server-side. Two are outright
#: credentials; the rest are labels that still have no business in a page's source.
NEVER_IN_THE_BROWSER = (
    "BIAL_DATABASE_URL",
    "BIAL_BLOB_SAS",
    "BIAL_BLOB_CONTAINER_URL",
    "BIAL_DICE_URL",
    "BIAL_DICE_CLIENT_ID",
    "IDENTITY_ENDPOINT",
    "IDENTITY_HEADER",
)


def _read_bial_config_body() -> str:
    """The body of `readBialConfig()` — the one function whose result reaches `window`."""
    source = LAYOUT.read_text(encoding="utf-8")
    start = source.index("function readBialConfig()")
    end = source.index("\n}", start)
    return source[start:end]


def test_the_browser_config_reads_exactly_two_environment_variables() -> None:
    body = _read_bial_config_body()

    names = sorted(set(re.findall(r"process\.env\.([A-Z0-9_]+)", body)))

    assert names == ["BIAL_APP_ID", "BIAL_PORTAL_ORIGIN"], (
        "the browser-published set has changed. Every other injected value is server-only — if "
        "this is deliberate, change this test and say why in the commit."
    )


def test_no_server_only_name_appears_in_the_browser_config() -> None:
    """The same claim from the other side, so a rename or an indirection does not slip past the
    exact-list assertion above."""
    body = _read_bial_config_body()

    for name in NEVER_IN_THE_BROWSER:
        assert name not in body, f"{name} is server-only and must not reach window.__BIAL_CONFIG"


def test_the_published_type_declares_exactly_those_two_fields() -> None:
    """`BialConfig` is what a citizen reads to learn what is available in the browser. A field
    here that the layout does not populate would advertise a value that is never there; a field
    the layout DOES populate and this type omits is the shape a leak takes."""
    source = CONFIG_TYPE.read_text(encoding="utf-8")
    declaration = source[source.index("export type BialConfig") : source.index("declare global")]

    fields = sorted(re.findall(r"^\s{2}(\w+)\?:", declaration, flags=re.MULTILINE))

    assert fields == ["appId", "portalOrigin"]


def test_the_probe_can_actually_fail() -> None:
    """Mutation-proofing: prove the extractor really is reading the function's body and would
    see a new name, rather than silently matching nothing."""
    body = _read_bial_config_body()

    assert "process.env.BIAL_APP_ID" in body
    assert re.findall(r"process\.env\.([A-Z0-9_]+)", body + "process.env.BIAL_DICE_URL") != sorted(
        set(re.findall(r"process\.env\.([A-Z0-9_]+)", body))
    )
