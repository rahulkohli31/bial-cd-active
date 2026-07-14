"""Track SANDBOX integration-harness fixtures.

The integration lane (`-m integration`) runs the REAL pre-baked sandbox image in Docker and,
for the snapshot round-trip, a real Azurite. Every fixture here **skips cleanly** when its
service is absent, so the default (offline) lane always runs and the integration lane degrades
to a clear skip rather than a hang or an error.

Fixtures:
  * `docker_ready`   — session skip-gate: skips the whole integration lane if Docker is absent.
  * `sandbox_image`  — session: the image tag to run. `BIAL_SANDBOX_IMAGE` points at a pre-built
                       tag (fast dev-loop / CI cache); otherwise the current `Dockerfile.sandbox`
                       is built once per session so the test exercises the CURRENT image.
  * `sandbox_factory`— function: `make(env) -> Sandbox`, tracking + tearing down every container.

`sandbox/tests/` has no `__init__.py` on purpose: pytest (prepend import mode) puts this dir on
`sys.path`, so sibling modules (`_docker`, later `fake_storage` / `snapshot_ref_client`) import
by bare name. The backend-`src` bridge + the Azurite fixture are added by U15 / U16.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

# --- D6 read-only bridge to backend's `src`-layout -------------------------------------------
# The harness subclasses the FROZEN C2 ABC + the ObjectStorage port for a genuine conformance
# guarantee. Backend's pytest is rooted at backend/ (its conftest HARD-REQUIRES Postgres), so we
# CANNOT collect under it; instead we prepend the backend dir to sys.path so `src.services.*`
# resolves here. This imports ONLY the frozen contract modules (verified: no Settings/DB triggered)
# — never backend *test internals* (`tests/fakes.py`); FakeStorage is vendored (`fake_storage.py`).
# An accepted, tracked coupling to backend's src-layout — NOT a `backend/` edit (R8).
_BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402  (import after the sys.path bridge above)
from _docker import (  # noqa: E402
    DEFAULT_IMAGE,
    Sandbox,
    build_image,
    docker_available,
    image_exists,
    run_sandbox,
)


@pytest.fixture(scope="session")
def docker_ready() -> None:
    if not docker_available():
        pytest.skip("Docker is not available — integration lane skipped")


@pytest.fixture(scope="session")
def sandbox_image(docker_ready: None) -> str:
    """Resolve the sandbox image tag. `BIAL_SANDBOX_IMAGE` reuses a pre-built tag (skips the slow
    node_modules bake); unset builds the current Dockerfile once per session (CI-honest)."""
    override = os.environ.get("BIAL_SANDBOX_IMAGE")
    if override:
        if not image_exists(override):
            pytest.skip(f"BIAL_SANDBOX_IMAGE={override!r} not found — build the image first")
        return override
    return build_image(DEFAULT_IMAGE)


@pytest.fixture
def sandbox_factory(sandbox_image: str) -> Iterator[object]:
    """A per-test factory that launches sandbox containers and tears them ALL down at teardown,
    even if the test raises. `make(env, wait=...)` returns a ready `Sandbox`."""
    created: list[Sandbox] = []

    def make(env: dict[str, str] | None = None, *, wait: bool = True) -> Sandbox:
        sbx = run_sandbox(env or {}, image=sandbox_image, wait=wait)
        created.append(sbx)
        return sbx

    try:
        yield make
    finally:
        for sbx in created:
            sbx.stop()
