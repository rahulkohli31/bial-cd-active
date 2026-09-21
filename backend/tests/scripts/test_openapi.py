"""The committed API reference must describe the surface this tree actually serves.

WHY THIS EXISTS. `documentation/reference/openapi.json` is published because production disables
the served schema route, so a reader holding the clone has no endpoint to ask — and a committed
artefact nobody regenerates is worse than none, because it reads as authoritative while describing
a surface that has moved. This is the only executable guard the published documentation has;
everything else in it is prose that nothing checks.

The guard itself is a script rather than a test because `tests/conftest.py` raises at collection
when the configured database is not a test database, and the guard has to run on a fresh clone with
nothing provisioned. Running the guard and testing the guard are different jobs; this file is the
second one, so anything here that touches the committed bytes spawns the script in a child process
with a stripped environment — which is also what proves it needs no database, no cache and no
configuration.

The failure message is asserted, not just the exit code: a check that fails with "files differ"
teaches nothing and trains people to regenerate blindly, which is the opposite of noticing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.openapi import (
    REGENERATE_COMMAND,
    TARGET,
    describe_drift,
    operations,
    serialise,
)
from tests.subprocess_env import child_env

_BACKEND = Path(__file__).resolve().parents[2]
_SCRIPT = "scripts/openapi.py"


def _run(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    """The child inherits nothing but `PATH` — which is the point of most of these tests."""
    return subprocess.run(  # noqa: S603
        [sys.executable, _SCRIPT, *args],
        cwd=_BACKEND,
        env=child_env(**env),
        capture_output=True,
        text=True,
        check=False,
    )


def shipped_text() -> str:
    """The committed bytes, decoded WITHOUT newline translation.

    `newline=""` matters here for the same reason it does in the generated connector catalogue's
    test: Python's universal-newline decoding would turn a CRLF checkout into `\\n` and hide the
    exact corruption the line-ending rule exists to prevent.
    """
    return TARGET.read_text(encoding="utf-8", newline="")


def shipped_spec() -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(shipped_text())
    return spec


# --- currency: the committed file is what the generator produces today ----------------------


def test_the_shipped_spec_is_what_the_generator_produces_today() -> None:
    """Fails when somebody edits the artefact by hand, or changes a route without regenerating."""
    result = _run("--check")
    assert result.returncode == 0, (
        f"the committed spec has drifted from this tree:\n{result.stdout}\n{result.stderr}"
    )


def test_a_clean_check_says_nothing_at_all() -> None:
    """A check that chatters on success gets filtered out of the ritual and stops being read.

    The application logs to stdout as it is constructed, which would otherwise put two lines of
    unrelated noise into the gate output on every run. The generator holds stdout for its own
    use, so both streams stay empty when there is nothing to report.
    """
    result = _run("--check")
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert result.stderr.strip() == ""


def test_the_generator_needs_no_database_no_redis_and_no_configuration() -> None:
    """The child is handed `PATH` and nothing else, and neither service is running here.

    This is the property that lets the check sit in the static gate list rather than behind a
    provisioned database — and it is the fresh-clone case, where no configuration file has been
    written yet.
    """
    result = _run("--check")
    assert result.returncode == 0, result.stderr


def test_the_generator_ignores_the_callers_environment_file() -> None:
    """The committed document must be reproducible by anyone, not a picture of one developer's box.

    A generator that honoured the caller's configuration would let two people produce two
    different files, and the byte comparison would then report each other's settings as drift.
    """
    result = _run("--check", ENV_FILE=".env.test")
    assert result.returncode == 0, (
        f"a caller's environment file must not change the generated document:\n{result.stderr}"
    )


# --- determinism: the bytes must not depend on how the interpreter was started ---------------


def test_two_generations_under_different_hash_seeds_agree() -> None:
    """Set iteration order must not reach the file, or every run would produce a fresh diff."""
    first = _run("--check", PYTHONHASHSEED="0")
    second = _run("--check", PYTHONHASHSEED="12345")
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr


def test_the_shipped_spec_carries_no_carriage_return() -> None:
    """Read as BYTES on purpose.

    A Windows checkout rewriting these line endings would fail the drift check on the build host
    while passing locally, which is the worst shape a guard can have. The `.gitattributes` entry
    and the generator's newline handling are what keep this true; this asserts the result.
    """
    assert b"\r" not in TARGET.read_bytes()


def test_the_shipped_spec_ends_in_exactly_one_newline() -> None:
    text = shipped_text()
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


# --- the failure message: it must name what moved and how to repair it -----------------------


def test_an_added_operation_is_named_in_the_drift_message() -> None:
    committed = shipped_spec()
    current = json.loads(json.dumps(committed))
    current["paths"]["/v1/a-route-that-was-just-added"] = {
        "get": {"summary": "added", "responses": {"200": {"description": "ok"}}}
    }

    message = describe_drift(committed, current)

    assert message, "an added operation must be reported as drift"
    assert "GET /v1/a-route-that-was-just-added" in message
    assert REGENERATE_COMMAND in message


def test_a_removed_operation_is_named_in_the_drift_message() -> None:
    committed = shipped_spec()
    current = json.loads(json.dumps(committed))
    victim = sorted(operations(committed))[0]
    method, path = victim.split(" ", 1)
    del current["paths"][path][method.lower()]

    message = describe_drift(committed, current)

    assert message
    assert victim in message
    assert REGENERATE_COMMAND in message


def test_a_changed_description_is_caught_even_though_the_operation_set_matches() -> None:
    """Route and model docstrings publish as OpenAPI descriptions.

    This is why a comment-only change in this tree can move the committed spec, and why the check
    cannot compare operation names alone.
    """
    committed = shipped_spec()
    current = json.loads(json.dumps(committed))
    target = sorted(operations(committed))[0]
    method, path = target.split(" ", 1)
    current["paths"][path][method.lower()]["description"] = "a docstring somebody reworded"

    assert operations(committed) == operations(current)

    message = describe_drift(committed, current)

    assert message, "a changed description must be reported even when no operation was added"
    assert target in message
    assert REGENERATE_COMMAND in message


def test_a_changed_component_schema_is_caught() -> None:
    """Pydantic model docstrings land under `components`, not under any one operation."""
    committed = shipped_spec()
    current = json.loads(json.dumps(committed))
    current.setdefault("components", {}).setdefault("schemas", {})["AFreshlyAddedModel"] = {
        "type": "object"
    }

    message = describe_drift(committed, current)

    assert message
    assert REGENERATE_COMMAND in message


def test_an_identical_spec_reports_no_drift() -> None:
    """The checker must be capable of returning clean, or the tests above prove nothing."""
    committed = shipped_spec()
    assert describe_drift(committed, json.loads(json.dumps(committed))) == ""


# --- the production gate suppresses the served route, not the schema object ------------------


def test_the_served_surface_does_not_depend_on_configuration() -> None:
    """The assumption that makes one committed document honest for every deployment.

    The document is generated from the sample environment. If any route were registered
    conditionally on settings, that file would describe one configuration while claiming to be
    the contract. This suite runs under a different environment from the one that generated it,
    so comparing the two operation sets is a real cross-configuration check.
    """
    from src.main import create_app

    assert operations(create_app().openapi()) == operations(shipped_spec())


def test_the_production_gate_removes_the_route_and_not_the_schema() -> None:
    """Production sets `openapi_url=None`, which is why the committed copy is the only reference a
    reader has. The generator asks the application object for its schema rather than fetching that
    route, so the gate cannot change what gets published."""
    from src.main import create_app

    app = create_app()
    assert app.openapi_url is not None, "the test environment serves the schema route"
    assert operations(app.openapi()), "the schema object is built regardless of the served route"


def test_any_byte_difference_is_reported_as_drift() -> None:
    """The invariant the checker exists to hold, stated directly.

    Python equality is not byte equality — `0 == False` and `1 == 1.0` — so a document that
    serialises differently can compare equal and report nothing. Each mutation below changes the
    bytes, so each one must produce a message.
    """
    committed = shipped_spec()
    mutations: list[tuple[str, dict[str, Any]]] = []

    zero_to_false = json.loads(shipped_text())
    for schema in (zero_to_false.get("components", {}).get("schemas") or {}).values():
        for prop in (schema.get("properties") or {}).values():
            if prop.get("default") == 0 and not isinstance(prop.get("default"), bool):
                prop["default"] = False
                mutations.append(("an integer default becomes a boolean", zero_to_false))
                break
        if mutations:
            break

    reshaped = json.loads(shipped_text())
    reshaped["openapi"] = [reshaped["openapi"]]
    mutations.append(("a top-level value changes shape", reshaped))

    assert mutations, "the shipped spec no longer carries the shape this test mutates"
    for description, mutated in mutations:
        assert serialise(committed) != serialise(mutated), description
        assert describe_drift(committed, mutated), (
            f"bytes differ but no drift was reported: {description}"
        )


# --- disclosure: nothing from the sample environment may reach the published file -------------


@pytest.mark.parametrize(
    "variable",
    [
        "DATABASE_URL",
        "REDIS__URL",
        "AUTH__CLIENT_SECRET",
        "AUTH__SESSION_SECRET",
        "SUPERADMIN_EMAILS",
        "APPS_BASE_URL",
    ],
)
def test_no_configured_value_reaches_the_published_spec(variable: str) -> None:
    """The generator reads a configured application; it must publish none of that configuration.

    These six carry a credential, a host or an address. The file is published in a repository the
    client holds, so a settings value reaching the schema is a disclosure rather than a cosmetic
    fault.
    """
    sample = dict(
        line.split("=", 1)
        for line in (_BACKEND / ".env.example").read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    value = sample[variable].strip()
    assert value, f"{variable} is not set in the sample environment"
    assert value not in shipped_text(), (
        f"the value configured for {variable} appears in the published spec"
    )


# --- the guard's own failure surface ------------------------------------------------------


def test_a_missing_reference_is_reported_with_the_command_that_creates_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fresh clone that has not generated the file yet is told how, not shown a traceback."""
    import scripts.openapi as script

    monkeypatch.setattr(script, "TARGET", tmp_path / "absent.json")

    assert script.main(["--check"]) == 1
    assert script.REGENERATE_COMMAND in capsys.readouterr().err


def test_an_unparseable_reference_is_reported_rather_than_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A conflict marker in a large generated document reads exactly this way, and this guard runs
    in a gate sequence — so it has to fail with something actionable rather than a decoder
    traceback."""
    import scripts.openapi as script

    broken = tmp_path / "openapi.json"
    broken.write_text('{"openapi": "3.1.0", <<<<<<< HEAD\n', encoding="utf-8")
    monkeypatch.setattr(script, "TARGET", broken)

    assert script.main(["--check"]) == 1
    assert script.REGENERATE_COMMAND in capsys.readouterr().err


def test_write_produces_a_file_that_check_then_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repair path — what people run when the gate fails, so not the one to leave untested."""
    import scripts.openapi as script

    target = tmp_path / "nested" / "openapi.json"
    monkeypatch.setattr(script, "TARGET", target)

    assert script.main(["--write"]) == 0
    assert target.exists()
    assert b"\r" not in target.read_bytes()
    assert script.main(["--check"]) == 0
