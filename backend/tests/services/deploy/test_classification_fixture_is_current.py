"""The canonical classification policy, exported as a fixture the portal pins against.

`deploy/classification.py` calls its table "ONE POLICY UNIT" that changes "together, in
review, in this file" — but the portal re-declares the same six `(key, label, weight)` rows
plus the threshold BY HAND (`portal/src/utils/deployApi.ts`, whose own docstring admits
"there is no codegen across the two languages"). They match today by careful editing and
nothing else: a reweight or a rewording on either side has no failing test on the other.

The damage is user-visible rather than a gate hole — the server is authoritative, so a
stale portal table cannot mis-publish anything — but the modal's running total, its
"Send for review" vs "Publish" label, and its score line would all tell the citizen
something the server then contradicts.

This test keeps `classification-policy.json` current; its twin on the portal side
(`deployApi.classification-parity.test.ts`) asserts the TypeScript mirror matches the same
file field-for-field. Neither language imports the other and no build step is added — the
fixture is the contract, and drift becomes a red test in whichever PR causes it.

IF THIS TEST FAILS: the policy changed and the fixture did not. Regenerate with

    uv run python -c "from tests.services.deploy.test_classification_fixture_is_current \
        import write_fixture; write_fixture()"

then run the portal suite — its parity test is what will tell you the TypeScript mirror
still needs the same edit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.services.deploy.classification import (
    AUTO_DEPLOY_MAX_SCORE,
    DATA_CLASSIFICATION_QUESTIONS,
)

# Beside the portal's own source so a reader finds it from either side; the portal test
# reads this exact path. Kept in the repo (not a build artifact) — it IS the contract.
FIXTURE = (
    Path(__file__).resolve().parents[3].parent
    / "portal"
    / "src"
    / "utils"
    / "__fixtures__"
    / "classification-policy.json"
)


def _policy() -> dict[str, Any]:
    """The canonical table in the shape both languages can compare field-for-field."""
    return {
        "_comment": (
            "GENERATED from backend/src/services/deploy/classification.py. Do not hand-edit: "
            "change the Python table, re-run write_fixture(), and update "
            "portal/src/utils/deployApi.ts to match."
        ),
        "autoDeployMaxScore": AUTO_DEPLOY_MAX_SCORE,
        "questions": [
            {"storedKey": key, "label": label, "weight": weight}
            for key, label, weight in DATA_CLASSIFICATION_QUESTIONS
        ],
    }


def write_fixture() -> None:
    """Rewrite the exported fixture from the Python table. The regeneration step, kept as a
    named function so the docstring can point at something runnable rather than describing
    a hand edit of generated JSON."""
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(_policy(), indent=2) + "\n", encoding="utf-8")


def test_the_exported_policy_fixture_matches_the_python_table() -> None:
    current = _policy()
    assert FIXTURE.is_file(), (
        f"{FIXTURE} is missing — regenerate it with `write_fixture()`. The portal's parity "
        "test reads it, so a missing fixture unpins the TypeScript mirror entirely."
    )
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == current, (
        "The classification policy changed and the exported fixture did not. Regenerate "
        "with `write_fixture()`, then update portal/src/utils/deployApi.ts to match — its "
        "own test will fail until you do."
    )
