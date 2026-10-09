"""The flight-data install command: what it writes, what it refuses, and where the image puts it.

`sandbox/scripts/adopt-flight-data.mjs` is generated (its currency is checked beside the reference
file's, in `test_template_reference_is_generated.py`). These tests run the COMMITTED script the
way the build agent does — `node <script>` from the app's folder — with `npm` replaced on `PATH`
by a stub that records its arguments. The script is copied out of the repository first, so a
script that read the seed workspace at run time would fail here, as it would in the image.

Needs `node` and a POSIX shell for the stub; a box without either skips, as the other
Node-dependent checks in this harness do.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_SANDBOX = Path(__file__).resolve().parent.parent
SCRIPT = _SANDBOX / "scripts" / "adopt-flight-data.mjs"
SEED = _SANDBOX / "seed"
DOCKERFILE = _SANDBOX / "Dockerfile.sandbox"
PROMPT_BLOCKS = _SANDBOX.parent / "backend" / "src" / "core" / "prompt_blocks.py"
REFERENCE = _SANDBOX / "template" / "lib" / "flight-data.reference.ts"

_NPM_STUB = """\
#!/bin/sh
printf '%s\\n' "$@" > "$NPM_ARGS"
exit "$NPM_EXIT"
"""

needs_node_and_sh = pytest.mark.skipif(
    shutil.which("node") is None or sys.platform == "win32",
    reason="needs node and a POSIX shell for the npm stub",
)


@dataclass
class Run:
    app: Path
    npm_args: Path
    proc: subprocess.CompletedProcess[str]

    def npm_was_asked_for(self) -> list[str] | None:
        if not self.npm_args.exists():
            return None
        return self.npm_args.read_text(encoding="utf-8").splitlines()


def _adopt(tmp_path: Path, *, npm_exit: int = 0, existing: str | None = None) -> Run:
    """Run a copy of the committed script from a fresh app folder, with npm stubbed."""
    copy = tmp_path / "image" / SCRIPT.name
    copy.parent.mkdir()
    shutil.copyfile(SCRIPT, copy)
    stub = tmp_path / "bin" / "npm"
    stub.parent.mkdir()
    stub.write_text(_NPM_STUB, encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    app = tmp_path / "app"
    app.mkdir()
    if existing is not None:
        (app / "lib").mkdir()
        (app / "lib" / "flight-data.ts").write_text(existing, encoding="utf-8")
    npm_args = tmp_path / "npm-args"
    env = {
        **os.environ,
        "PATH": f"{stub.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "NPM_ARGS": str(npm_args),
        "NPM_EXIT": str(npm_exit),
    }
    proc = subprocess.run(
        ["node", str(copy)],
        cwd=app,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return Run(app=app, npm_args=npm_args, proc=proc)


def _seed_versions() -> list[str]:
    dependencies = json.loads((SEED / "package.json").read_text(encoding="utf-8"))["dependencies"]
    return [f"{name}@{version}" for name, version in dependencies.items()]


def _files_under(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@needs_node_and_sh
def test_it_installs_the_tested_versions_then_writes_the_seed_module(tmp_path: Path) -> None:
    run = _adopt(tmp_path)

    assert run.proc.returncode == 0, run.proc.stderr
    asked = run.npm_was_asked_for()
    assert asked is not None
    assert asked[:2] == ["install", "--save-exact"]
    assert [arg for arg in asked if not arg.startswith("-")][1:] == _seed_versions()
    assert (run.app / "lib" / "flight-data.ts").read_bytes() == (
        SEED / "flight-data.ts"
    ).read_bytes()


@needs_node_and_sh
def test_an_existing_module_is_left_alone_and_nothing_is_installed(tmp_path: Path) -> None:
    """The app's own `lib/flight-data.ts` may be an older copy or one the agent changed. Either
    way it is what the app runs, so the command refuses rather than overwrite it, and says to
    read that file rather than trust a summary of the current version."""
    own = "export const mine = true\n"
    run = _adopt(tmp_path, existing=own)

    assert run.proc.returncode != 0
    assert "this app's own copy" in run.proc.stderr
    assert "read its exports" in run.proc.stderr
    assert run.npm_was_asked_for() is None
    assert _files_under(run.app) == {"lib/flight-data.ts": own.encode()}


@needs_node_and_sh
def test_a_failed_install_writes_no_module(tmp_path: Path) -> None:
    """A module whose packages are missing fails the app's whole type-check, so the module is
    written only once the install has succeeded."""
    run = _adopt(tmp_path, npm_exit=1)

    assert run.proc.returncode != 0
    assert run.npm_was_asked_for() is not None
    assert not (run.app / "lib" / "flight-data.ts").exists()
    assert "was not written" in run.proc.stderr


def _instruction_containing(dockerfile: str, needle: str) -> str:
    """The one instruction holding `needle`, with its continuation lines joined."""
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    matches = [line for line in joined.splitlines() if needle in line]
    assert len(matches) == 1, f"expected one instruction containing {needle!r}, found {matches}"
    return matches[0]


def test_the_image_puts_it_where_the_backend_and_the_reference_say() -> None:
    """Three spellings of one path: the image's COPY, the backend constant the schema answer and
    the command classifier read, and the reference file's head. The image is also where the CRs
    a Windows checkout might add are stripped and the script made executable."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    copy = _instruction_containing(dockerfile, "COPY scripts/adopt-flight-data.mjs")
    destination = copy.split()[-1]
    assert destination == "/usr/local/lib/bial/adopt-flight-data.mjs"

    strip_then_chmod = _instruction_containing(dockerfile, f"sed -i 's/\\r$//' {destination}")
    strip, chmod = strip_then_chmod.split("&&")
    assert destination in strip
    assert "chmod +x" in chmod and destination in chmod

    assert f'FLIGHT_DATA_ADOPT_PATH = "{destination}"' in PROMPT_BLOCKS.read_text(encoding="utf-8")
    assert f"node {destination}" in REFERENCE.read_text(encoding="utf-8")
