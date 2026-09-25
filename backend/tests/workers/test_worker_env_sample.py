"""The tracked worker environment sample, `.env.worker.example`.

THE WORKER ROLE HAD NO TEMPLATE, once. A fresh checkout could produce a working API from
`.env.example`; there was nothing at all for the worker, and `WorkerSettings` makes object
storage, Redis and ARM access REQUIRED in every environment — so a hand-assembled file fails at
construction, and the cheapest way out of that is to start trimming safety. This file pins the
sample that fixes that, and that it stays safe to publish."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from tests.subprocess_env import child_env

#: The `backend/` tree — `tests/workers/` lives two levels under it.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_WORKER_SAMPLE = ".env.worker.example"

#: The only strings a `SANDBOX__*` value in the tracked sample may be. Deliberately a closed set
#: of exact literals rather than a "looks fake enough" heuristic: this repo is PUBLIC and has no
#: secret scanning, the worker's `SandboxConfig` is REQUIRED and carries an ACR password, and the
#: failure mode is somebody pasting a working credential in to make the file parse.
_PLACEHOLDERS = frozenset({"REPLACE_ME", "00000000-0000-0000-0000-000000000000"})

_SANDBOX_KEY = re.compile(r"^SANDBOX__[A-Z0-9_]+$")


def test_the_worker_sample_boots_a_valid_worker_profile() -> None:
    """A SUBPROCESS WITH A SCRUBBED ENVIRONMENT, not an in-process construct: pydantic-settings
    merges the ambient environment on top of the file, so a developer's own `.env.worker` would
    quietly supply anything this sample forgot — the exact drift the test exists to catch."""
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-B",
            "-c",
            "from src.config import resolve_settings;"
            " s = resolve_settings();"
            " print(type(s).__name__)",
        ],
        cwd=_BACKEND_ROOT,
        env=child_env(ENV_FILE=_WORKER_SAMPLE, BIAL_ROLE="worker"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, f"{_WORKER_SAMPLE} does not boot a worker:\n{done.stderr}"
    # `BIAL_ROLE` is read from the real environment, never from the file, so a sample that
    # booted an ApiSettings would prove nothing about the worker.
    assert done.stdout.split() == ["WorkerSettings"], done.stdout


def test_no_sandbox_value_in_the_worker_sample_looks_real() -> None:
    """THE ONE THAT STOPS A CREDENTIAL REACHING A PUBLIC REPO.

    `SandboxConfig` is REQUIRED for this role and carries `acr_password`, `acr_username` and a
    subscription id, and `.env.example` never had to demonstrate safe placeholders for that block
    because the API's sandbox is optional. So there is nothing here to copy the convention from,
    and the natural move when a sample will not parse is to paste a value that works. This
    repository is public and has nothing scanning it.

    COMMENTED LINES COUNT TOO: a commented-out real credential is a committed credential."""
    lines = (_BACKEND_ROOT / _WORKER_SAMPLE).read_text(encoding="utf-8").splitlines()
    values = {
        key: value
        for key, _, value in (line.lstrip("# ").partition("=") for line in lines)
        if _SANDBOX_KEY.match(key)
    }

    assert values, f"{_WORKER_SAMPLE} documents no SANDBOX__* key at all"
    not_placeholders = {k: v for k, v in values.items() if v not in _PLACEHOLDERS}
    assert not not_placeholders, (
        f"{_WORKER_SAMPLE} carries SANDBOX__ values that are not placeholders — this repo is "
        f"public and unscanned, so replace each with one of {sorted(_PLACEHOLDERS)}: "
        f"{sorted(not_placeholders)}"
    )


def test_the_worker_sample_is_not_excluded_by_gitignore() -> None:
    """A TEMPLATE NOBODY CAN COMMIT IS THE ABSENCE IT WAS WRITTEN TO FIX. `backend/.gitignore` is
    `.env.*` — which matches this filename — rescued only by the `!.env*.example` negation on the
    next line. Reorder those two, or narrow the negation to `!.env.example`, and the file silently
    stops being trackable while every other test here stays green.

    ASKED OF GIT ITSELF rather than by re-implementing pattern precedence: last-match-wins across
    nested `.gitignore` files is not a rule worth reproducing in a test. `--no-index` keeps the
    answer the same before and after the file is committed."""
    probe = subprocess.run(  # noqa: S603
        ["git", "check-ignore", "--no-index", "-v", "--", f"backend/{_WORKER_SAMPLE}"],
        cwd=_BACKEND_ROOT.parent,
        capture_output=True,
        text=True,
        check=False,
    )

    if probe.returncode == 1:  # no pattern matched at all — trivially trackable
        return
    assert probe.returncode == 0, f"git could not answer: {probe.stderr}"
    # `<source>:<line>:<pattern>\t<path>`. A leading `!` is git saying "explicitly NOT ignored".
    pattern = probe.stdout.split("\t", 1)[0].split(":", 2)[2]
    assert pattern.startswith("!"), (
        f"backend/{_WORKER_SAMPLE} is ignored by {pattern!r} — the template cannot be committed"
    )
