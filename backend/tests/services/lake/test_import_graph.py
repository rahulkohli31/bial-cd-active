"""The settings module must import without reaching the ORM — checked by actually importing it.

WHY THIS EXISTS AS A TEST, AND WHY IT SPAWNS AN INTERPRETER. `src/settings/api.py` declares a
`LakeConfig` field, so importing settings imports `src/services/lake/__init__.py` — Python runs a
package's `__init__` before any submodule of it. Anything reachable from those re-export lines
therefore runs during settings construction, and `src/core/connectors.py` reaches
`src/db/models/`, which reaches `src/db/base.py`, which imports `src.config` at module scope. One
re-export of the wrong module closes that cycle and the process cannot boot AT ALL.

That failure has now been introduced twice while building this feature — once by nesting the lake
package under `services/connectors/`, and once by re-exporting `copy.py` — so it is a shape, not
an accident.

NONE OF THE FOUR STATIC GATES CATCHES IT. `ruff`, `ty`, `mypy` and `pyright` analyse the import
graph without executing it, and a cycle that is fine for a type checker is fatal for the
interpreter. CI runs those four and does not run pytest, so this test is not itself a CI guard —
it is what makes the failure loud and attributable the moment anyone runs the suite, instead of
surfacing as a container that will not start.

IN-PROCESS IT WOULD PROVE NOTHING. By the time pytest collects this file, conftest has already
imported half the tree, so every module is in `sys.modules` and the cycle cannot re-form. A fresh
interpreter is the only honest way to ask the question.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[3]


def _import_in_a_fresh_interpreter(module: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=_BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_settings_module_imports_on_its_own() -> None:
    """The exact import the process performs at startup, before anything else has run."""
    result = _import_in_a_fresh_interpreter("src.settings.api")

    assert result.returncode == 0, (
        "importing settings closed an import cycle — most likely a new re-export in "
        f"src/services/lake/__init__.py that reaches src/db/models/:\n{result.stderr}"
    )


def test_the_lake_package_imports_without_the_orm() -> None:
    """The narrower claim, so a failure points at the package rather than at settings."""
    result = _import_in_a_fresh_interpreter("src.services.lake")

    assert result.returncode == 0, result.stderr


def test_the_check_can_actually_fail() -> None:
    """Mutation-proofing: prove a fresh interpreter is really being spawned and its failure really
    is observable. Without this, a subprocess that silently succeeded on everything — a wrong
    `cwd`, a swallowed error — would make both assertions above pass forever."""
    result = _import_in_a_fresh_interpreter("src.no_such_module_exists")

    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr
