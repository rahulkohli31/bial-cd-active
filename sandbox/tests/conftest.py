"""Sandbox integration-harness fixtures.

The integration lane (`-m integration`) runs the REAL pre-baked sandbox image in Docker. Fixtures
skip cleanly when Docker is absent, so the default (offline) lane always runs and the integration
lane degrades to a skip, not a hang.

Fixtures: `docker_ready` (skip-gate), `sandbox_image` (image tag), `sandbox_factory` (launches +
tears down containers) — each documents itself below.

No `__init__.py` here on purpose: pytest's prepend import mode puts this dir on `sys.path`, so
the sibling `_docker` module imports by bare name.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from _docker import (
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

    def make(
        env: dict[str, str] | None = None,
        *,
        wait: bool = True,
        extra_run_args: list[str] | None = None,
    ) -> Sandbox:
        sbx = run_sandbox(env or {}, image=sandbox_image, wait=wait, extra_run_args=extra_run_args)
        created.append(sbx)
        return sbx

    try:
        yield make
    finally:
        for sbx in created:
            sbx.stop()
