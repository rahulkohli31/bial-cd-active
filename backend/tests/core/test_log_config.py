"""A logged exception leaves every process as its signature: named, placed, and never its text."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from src.core.log_config import configure_logging
from tests.subprocess_env import child_env

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_SECRET = "Bearer sk-live-0000 postgresql://bial:hunter2@db/app"


@pytest.fixture(autouse=True)
def _restores_the_suite_logging() -> Iterator[None]:
    saved = structlog.get_config()
    yield
    structlog.configure(**saved)


def _log_a_failure(form: str) -> None:
    log = structlog.get_logger()
    try:
        try:
            raise ValueError(_SECRET)
        except ValueError as inner:
            raise RuntimeError(_SECRET) from inner
    except RuntimeError as exc:
        exc_info: object = {
            "true": True,
            "instance": exc,
            "tuple": (type(exc), exc, exc.__traceback__),
        }[form]
        log.error("recovery_write_did_not_land", exc_info=exc_info)


@pytest.mark.parametrize("form", ["true", "instance", "tuple"])
def test_production_logs_the_signature_and_not_the_exception(
    form: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The production line an operator reads. Mutation check: drop the signature processor and
    the line carries `"exc_info": true` with no cause at all."""
    configure_logging(production=True)

    _log_a_failure(form)

    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "exc_info" not in line
    assert line["exc_signature"].startswith(
        "RuntimeError <- ValueError at=tests.core.test_log_config:_log_a_failure:"
    ), line
    assert "hunter2" not in json.dumps(line)
    assert "sk-live" not in json.dumps(line)


def test_development_console_names_the_failure_without_its_text_or_locals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The console renderer prints a traceback with frame locals when handed `exc_info`, and a
    local here is the secret."""
    configure_logging(production=False)

    _log_a_failure("true")

    out = capsys.readouterr().out
    assert "RuntimeError <- ValueError" in out
    assert "hunter2" not in out
    assert "sk-live" not in out


def test_the_worker_process_configures_its_logging() -> None:
    """The worker never imports `src.main`, so it gets this chain only if its own entry point
    installs it. Mutation check: remove the call from `worker_main.py` and this reads False."""
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-B",
            "-c",
            "import src.worker_main, structlog; print(structlog.is_configured())",
        ],
        cwd=_BACKEND_ROOT,
        env=child_env(ENV_FILE=".env.test"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert result.stdout.strip().splitlines()[-1] == "True", result.stdout
