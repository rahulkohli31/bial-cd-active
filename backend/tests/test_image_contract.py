"""The BUILT backend image must carry the binaries the control plane shells out to.

WHY THIS EXISTS. `services/storage/snapshot_read.py` runs `git clone` to restore an app's
snapshot, and that path sits under BOTH the Plan-mode read and the entire publish pipeline
(`services/deploy/service.py`). The base image shipped none — `python:3.14-slim` has no
`/usr/bin/git` and no `/usr/lib/git-core` — so publish was broken in every environment running
that image, and nothing in the suite noticed, because every other test runs on a developer
machine where git is on the PATH by accident.

WHY IT ASSERTS AGAINST THE PINNED PATH, NOT THE DEFAULT ONE. This is the part a naive version
of this test gets wrong. `_git_env` hands the clone subprocess an explicit, minimal environment
whose `PATH` is `/usr/local/bin:/usr/bin:/bin` and nothing else — no inherited PATH, deliberately,
so an untrusted bundle's checkout cannot reach a planted binary. A test that runs a bare
`git --version` inside the image would therefore pass on a base that installs git to, say,
`/opt/git/bin` while production still fails. So the check runs git under `env -i` with EXACTLY
the PATH the source pins, imported from the source module rather than retyped here — the two
cannot drift apart, because there is only one copy of the string.

WHAT WOULD BREAK THIS. Any base-image change that drops git (the Alpine rebase is the immediate
one, but so is any future move), or a change to `_git_env`'s PATH that no longer covers wherever
the new base puts git. That is precisely the pair of mistakes this is here to catch, and it is
why the guard is an IMAGE test rather than another unit test with a mocked subprocess.

Marked `integration` because it needs a Docker daemon: the default lane deselects it
(`pyproject.toml` `addopts`), and it skips cleanly rather than erroring when Docker is absent.

    uv run pytest tests/test_image_contract.py -m integration
    BIAL_BACKEND_IMAGE=bial-backend:local uv run pytest tests/test_image_contract.py -m integration
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from src.services.storage.snapshot_read import _git_env

pytestmark = pytest.mark.integration

# `backend/` — the Docker build context (this file lives at backend/tests/test_image_contract.py).
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_TAG = "bial-backend-imagetest"

# The architecture the artifact actually ships as, whatever the host. An arm64 developer machine
# building the default platform would test an image nobody deploys.
_PLATFORM = "linux/amd64"


def _docker_available() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):  # fmt: skip  # ruff py314 strips parens
        return False
    return proc.returncode == 0


@pytest.fixture(scope="module")
def backend_image() -> str:
    """The image tag to probe. `BIAL_BACKEND_IMAGE` reuses a pre-built tag; unset builds the
    CURRENT Dockerfile once per module, so the test exercises the Dockerfile in the tree."""
    if not _docker_available():
        pytest.skip("Docker is not available — image contract lane skipped")
    if override := os.environ.get("BIAL_BACKEND_IMAGE"):
        probe = subprocess.run(["docker", "image", "inspect", override], capture_output=True)
        if probe.returncode != 0:
            pytest.skip(f"BIAL_BACKEND_IMAGE={override!r} not found — build the image first")
        return override
    build = subprocess.run(
        ["docker", "build", "--platform", _PLATFORM, "-f", "Dockerfile", "-t", _DEFAULT_TAG, "."],
        cwd=_BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if build.returncode != 0:
        pytest.fail(f"backend image build failed:\n{build.stderr[-4000:]}")
    return _DEFAULT_TAG


def _run_in_image(image: str, script: str) -> subprocess.CompletedProcess[str]:
    """Run a shell snippet inside the image, bypassing its CMD."""
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            _PLATFORM,
            "--entrypoint",
            "sh",
            image,
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_git_resolves_on_the_exact_path_the_snapshot_clone_pins(backend_image: str) -> None:
    """git must be reachable from the minimal env `_git_env` builds — not merely installed."""
    pinned_path = _git_env(Path("/tmp"))["PATH"]
    # `env -i` wipes the environment so ONLY the pinned PATH is in play. If git resolves here it
    # resolves for `snapshot_read`, because this is the same environment that call constructs.
    result = _run_in_image(backend_image, f"env -i PATH={pinned_path} git --version")

    assert result.returncode == 0, (
        f"`git` did not resolve on the PATH the snapshot clone pins ({pinned_path}).\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}\n"
        "The control plane shells out to git for snapshot restore and publish; an image "
        "without it breaks both."
    )
    assert result.stdout.startswith("git version"), result.stdout


def test_tls_trust_store_is_present(backend_image: str) -> None:
    """ca-certificates: the control plane talks TLS to Postgres, Blob storage and Entra.

    Named explicitly in the Dockerfile even though the slim base already carries it, so a base
    change cannot drop it silently alongside git — which is exactly the failure this pins.
    """
    result = _run_in_image(backend_image, "ls /etc/ssl/certs/ca-certificates.crt")
    assert result.returncode == 0, f"no CA bundle in the image: {result.stderr!r}"


def test_the_application_user_is_unprivileged(backend_image: str) -> None:
    """The image must not run as root. Pinned here because the Alpine rebase rewrites the
    user-creation syntax (`useradd` does not exist on Alpine), and a botched rewrite that
    silently leaves the image running as root is a change no unit test would see.

    ASSERTS THE EFFECTIVE USER, NOT A PASSWD LOOKUP. `id -u app` answers "what uid is the
    account named app?" — which /etc/passwd will happily report on an image that then runs
    every process as root. Deleting `USER app` from the Dockerfile left that form green, so it
    passed on precisely the regression it exists to catch. `id -u` with no operand asks the
    only question that matters: who is this container actually running as.
    """
    result = _run_in_image(backend_image, "id -u; id -un")
    assert result.returncode == 0, f"could not read the container's user: {result.stderr!r}"
    uid, _, name = result.stdout.strip().partition("\n")
    assert uid == "10001", (
        f"the image runs as uid {uid!r}, not the unprivileged 10001 — check that `USER app` "
        "survived, and that the addgroup/adduser pair still creates it"
    )
    assert name == "app", f"expected the app user, got {name!r}"
