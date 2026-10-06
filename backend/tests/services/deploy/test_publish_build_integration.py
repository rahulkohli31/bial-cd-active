"""The publish build, run through the platform Dockerfile for real.

The unit tests prove the build copy's `package.json` changes. Whether an old project then
actually builds on the Next floor depends on the Dockerfile's drift fallback recognising
npm's refusal, and whether a new project still publishes depends on the template's lockfile
installing under the deploy image's own npm. Neither is visible without a real build.

Marked `integration`: needs a Docker daemon and the npm registry.

    uv run pytest tests/services/deploy/test_publish_build_integration.py -m integration
"""

from __future__ import annotations

import gzip
import io
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

from src.services.deploy.context import NEXT_FLOOR, build_context

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[4]
_DOCKERFILE = _REPO / "backend" / "src" / "services" / "deploy" / "assets" / "Dockerfile"
# The notice as the step PRINTED it (`#<step> <seconds> <text>`). BuildKit also echoes each
# RUN command, which contains the same words, so a bare substring match proves nothing.
_FALLBACK_RAN = re.compile(
    r"^#\d+ [\d.]+ lockfile drifted from package.json; falling back to npm install$", re.MULTILINE
)


def _node_image() -> str:
    match = re.search(r"^ARG NODE_IMAGE=(\S+)$", _DOCKERFILE.read_text(), re.MULTILINE)
    assert match is not None
    return match.group(1)


@pytest.fixture(scope="module", autouse=True)
def _docker() -> None:
    try:
        ok = subprocess.run(["docker", "version"], capture_output=True, timeout=15).returncode == 0
    except FileNotFoundError, subprocess.TimeoutExpired:
        ok = False
    if not ok:
        pytest.skip("Docker is not available")


def _unpack_to(packed: bytes, dest: Path) -> Path:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(packed)), mode="r") as archive:
        archive.extractall(dest, filter="data")
    return dest


def _build(context: Path, tag: str, *, target: str | None = None) -> str:
    # `--no-cache`: a cached step replays no output, and the fallback is read from output.
    command = ["docker", "build", "--no-cache", "--progress=plain", "-t", tag]
    command += ["--build-arg", "BIAL_BASE_PATH=/a/pub-0000000000000000000000000000"]
    command += ["--build-arg", "BIAL_APPS_HOSTNAME=apps.example.test"]
    if target is not None:
        command += ["--target", target]
    proc = subprocess.run([*command, str(context)], capture_output=True, text=True, timeout=1800)
    assert proc.returncode == 0, proc.stderr[-4000:]
    return proc.stderr


def _installed_next(tag: str) -> str:
    proc = subprocess.run(
        ["docker", "run", "--rm", tag, "node", "-p", "require('next/package.json').version"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_an_old_project_publishes_on_the_floor_through_the_drift_fallback(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "package.json").write_text(
        '{"name":"old-app","private":true,'
        '"dependencies":{"next":"16.3.3","react":"19.2.7","react-dom":"19.2.7"}}'
    )
    locked = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{app}:/app", "-w", "/app", _node_image(),
         "npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"],
        capture_output=True, text=True, timeout=600,
    )  # fmt: skip
    assert locked.returncode == 0, locked.stderr

    context = _unpack_to(build_context(app), tmp_path / "context")
    log = _build(context, "bial-publish-floor-test", target="deps")

    assert _FALLBACK_RAN.search(log)
    assert _installed_next("bial-publish-floor-test") == NEXT_FLOOR


def test_a_new_project_installs_its_lock_as_is_and_builds_standalone(tmp_path: Path) -> None:
    context = _unpack_to(build_context(_REPO / "sandbox" / "template"), tmp_path / "context")
    log = _build(context, "bial-publish-template-test", target="builder")

    assert not _FALLBACK_RAN.search(log)
    assert _installed_next("bial-publish-template-test") == NEXT_FLOOR
