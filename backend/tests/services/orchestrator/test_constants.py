"""The fail-closed write allowlist + read ignore set (U1, KD-9/KD-10) — the security boundary
that keeps the model out of `.git/**`, the never-edit infra, and any `..` escape."""

from __future__ import annotations

import pytest

from src.services.orchestrator import constants


def test_frozen_budgets_are_in_module_not_config() -> None:
    # The self-heal budget + per-run ceiling are in-module constants (KD-7); none reads config.py.
    assert constants.SELF_HEAL_MAX_RETRIES == 3
    assert constants.MODEL_TURN_CEILING > 0
    assert constants.TYPECHECK_CMD == ("npx", "tsc", "--noEmit")
    assert constants.EXEC_TIMEOUT_S < 900  # within C1's hard exec cap
    assert constants.TEMPERATURE == 0.0


@pytest.mark.parametrize(
    "path",
    [
        "app/records/page.tsx",
        "app/page.tsx",
        "components/widgets/data-table.tsx",
        "lib/format.ts",
        "./app/records/page.tsx",  # normalizes to a writable path
    ],
)
def test_write_allowed_inside_the_surface(path: str) -> None:
    assert constants.is_write_allowed(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "lib/bial-data.ts",  # the frozen data module
        "components/ui/button.tsx",  # shadcn primitive — compose only
        "components/bial/error-capture.tsx",  # platform shim
        "package.json",
        "package-lock.json",
        "next.config.ts",
        "tsconfig.json",
        ".env",
        ".git/config",  # the exfiltration path a denylist would miss
        ".git/hooks/pre-push",
        "app/../lib/bial-data.ts",  # `..` escape landing on a never-edit file
        "app/../../etc/passwd",  # `..` escape out of the workspace
        "/etc/passwd",  # absolute
        "/workspace/app/app/page.tsx",  # absolute even into the surface → denied (fail-closed)
        "README.md",  # a root file, outside the surface
        "",  # empty
    ],
)
def test_write_denied_by_default(path: str) -> None:
    assert constants.is_write_allowed(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/react/index.js",
        ".next/build-manifest.json",
        "dist/x.js",
        ".git/config",
        "package-lock.json",
        "app/node_modules/dep/x.ts",
        "pnpm-lock.yaml",
    ],
)
def test_read_ignored(path: str) -> None:
    assert constants.is_read_ignored(path) is True


@pytest.mark.parametrize(
    "path",
    ["app/page.tsx", "lib/bial-data.ts", "components/ui/button.tsx", "README.md"],
)
def test_read_allowed(path: str) -> None:
    # A read can't mutate (KD-10) — even the never-edit files are readable so the model can learn
    # the data API before composing against it.
    assert constants.is_read_ignored(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",  # absolute — must be denied, not read as a relative path
        "/proc/self/environ",  # the supervisor token lives here (KD-9); never readable
        "/workspace/.env",  # absolute into the workspace root
        "app/../../etc/shadow",  # `..` escape out of the workspace
        "..",  # bare parent
        "",  # empty
    ],
)
def test_read_ignored_denies_absolute_and_traversal_paths(path: str) -> None:
    # The read guard normalizes through the SAME fail-closed `_normalize_rel` as the write guard,
    # so an absolute or `..`-escaping path is denied — it is no longer silently stripped to a
    # readable relative path (the fixed asymmetry with is_write_allowed).
    assert constants.is_read_ignored(path) is True
