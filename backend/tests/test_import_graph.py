"""The import graph must let a NON-FastAPI process import `src/`.

WHY THIS EXISTS. The scheduled sandbox sweep runs on a Taskiq worker. That worker imports
`src.services.build_sessions.reaper` and `src.services.deploy.reconcile` without ever building a
FastAPI app. Those imports used to FAIL — not because the services needed the API,
but because `src/api/v1/build_sessions/__init__.py` re-exported `deps` and `router` at package
level, so touching any of its schemas dragged the whole route tree in behind it. Those six lines
were dead: every real consumer already imported the submodule.

WHY A SUBPROCESS, AND WHY IT IS NOT CEREMONY. `tests/conftest.py` imports `src.main` before any
test runs, so by the time an in-process assertion executes, every module it could ask about is
already in `sys.modules` and the test passes no matter what the import graph does. Only a fresh
interpreter proves the standalone import. `-B` keeps the run from writing bytecode back.

WHAT WOULD BREAK THIS. Re-adding a package-level `deps`/`router` re-export to
`src/api/v1/build_sessions/__init__.py` — mutation-checked once by hand when that re-export was
removed, and the reason `test_the_package_does_not_drag_in_the_route_tree` asserts on loaded
SUBMODULES rather than on source text: a source-text check passes against a re-export spelled a
new way.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.subprocess_env import child_env

# `backend/` — tests/ lives directly under it.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _import_in_fresh_interpreter(snippet: str) -> subprocess.CompletedProcess[str]:
    """Run `snippet` in a clean interpreter rooted at `backend/`, with only PATH and the test
    env file inherited — no ambient DATABASE_URL, no REDIS__*, and crucially no already-imported
    `src.main` from the suite's own conftest."""
    return subprocess.run(  # noqa: S603
        [sys.executable, "-B", "-c", snippet],
        cwd=_BACKEND_ROOT,
        env=child_env(ENV_FILE=".env.test"),
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_reaper_imports_without_the_fastapi_app() -> None:
    """The worker's scheduled sweep task imports this. It used to raise on a cold interpreter,
    before `build_sessions/__init__.py` stopped re-exporting `deps`/`router`."""
    result = _import_in_fresh_interpreter(
        "import importlib;"
        " importlib.import_module('src.services.build_sessions.reaper');"
        " print('ok')"
    )
    assert result.returncode == 0, (
        f"reaper is not importable standalone.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ok" in result.stdout


def test_the_integrity_verdict_carries_nothing_heavy_of_its_own() -> None:
    """`manager.py` AND `reaper.py` both import this module at module level, so it must not
    reach back into either of them, or into the orchestrator. LOADED BY FILE PATH, DELIBERATELY:
    importing by name runs the PACKAGE `__init__`, which already drags in `pydantic_ai` via
    `manager` — true of `reaper` today and not this module's doing. What IS this module's doing
    is staying cheap enough to never become the reason a worker loads the agent stack.
    NOT ASSERTED: `fastapi` still arrives via `sandbox`/`storage`, which every module here loads.
    Mutation check: add an `orchestrator` (or `manager`) import to `integrity.py` and this
    goes red."""
    result = _import_in_fresh_interpreter(
        "import importlib.util, sys;"
        " spec = importlib.util.spec_from_file_location("
        "'bial_integrity_probe', 'src/services/build_sessions/integrity.py');"
        " assert spec and spec.loader;"
        " module = importlib.util.module_from_spec(spec);"
        # Registered before exec: `from __future__ import annotations` makes dataclasses
        # resolve field types by looking the defining module up in `sys.modules`.
        " sys.modules['bial_integrity_probe'] = module;"
        " spec.loader.exec_module(module);"
        " heavy = [name for name in sys.modules"
        "   if name.startswith(('pydantic_ai', 'src.services.orchestrator'))"
        "   or name in ('src.main', 'src.services.build_sessions.manager')];"
        " assert not heavy, heavy;"
        " print('ok')"
    )
    assert result.returncode == 0, (
        f"the integrity verdict grew a heavy import.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ok" in result.stdout


def test_the_environment_accessor_stays_outside_the_settings_cycle() -> None:
    """THE CYCLE IS THE WHOLE REASON `src/core/runtime_env.py` EXISTS: `src.config` reaches
    `src.settings.api`, which reaches both `src.services.redis.config` and the sandbox config, so
    a module that needs "which environment is this" cannot ask `settings` at import time. One
    leaf accessor replaces the per-module workarounds, and it stays safe only while it imports
    nothing at module scope.

    Mutation-check: hoist `from src.config import settings` to the top of
    `src/core/runtime_env.py` and this goes red."""
    result = _import_in_fresh_interpreter(
        "import importlib, sys;"
        " importlib.import_module('src.core.runtime_env');"
        " assert 'src.config' not in sys.modules, 'the accessor dragged src.config in';"
        " importlib.import_module('src.services.redis.keys');"
        " importlib.import_module('src.config');"
        " print('ok')"
    )
    assert result.returncode == 0, (
        f"the environment accessor closed the settings cycle.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ok" in result.stdout


def test_deploy_reconcile_imports_without_the_fastapi_app() -> None:
    """The first passenger on the scheduler — proven importable before it is scheduled."""
    result = _import_in_fresh_interpreter(
        "import importlib; importlib.import_module('src.services.deploy.reconcile'); print('ok')"
    )
    assert result.returncode == 0, (
        f"deploy.reconcile is not importable standalone.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ok" in result.stdout


def test_the_package_does_not_drag_in_the_route_tree() -> None:
    """THE REGRESSION GUARD. Importing the build-sessions package for a schema must not load
    its `router` or `deps` submodules.

    Asserted on `sys.modules` rather than on the text of `__init__.py`, because the failure mode
    is "someone re-exports the router", not "someone writes a specific line" — a re-export
    spelled `from .router import router` or `from . import router` must fail this test too.
    """
    result = _import_in_fresh_interpreter(
        "import sys, importlib;"
        " importlib.import_module('src.api.v1.build_sessions');"
        " loaded = [m for m in ('src.api.v1.build_sessions.router',"
        " 'src.api.v1.build_sessions.deps') if m in sys.modules];"
        " print('LOADED:' + ','.join(loaded))"
    )
    assert result.returncode == 0, f"package import failed.\nstderr: {result.stderr}"
    assert "LOADED:" in result.stdout, result.stdout
    dragged_in = result.stdout.split("LOADED:")[1].strip()
    assert dragged_in == "", (
        "the build-sessions package re-exports the route tree again — a worker importing this "
        f"package now pulls in {dragged_in}. The package must not re-export deps/router."
    )


def test_the_app_still_builds_with_its_full_route_surface() -> None:
    """The app is the other consumer of that package, and it reaches the router by SUBMODULE
    import (`src/api/v1/router.py`), which the `deps`/`router` cleanup did not touch. Pinned on
    the build-session route surface specifically: that is the contract the removed `router`
    re-export sat next to, so a regression would show up here first.

    `app.openapi()` rather than `app.routes` — this FastAPI defers router inclusion behind
    `_IncludedRouter`, so `app.routes` reports a handful of top-level entries and would pass
    while every real route was missing.
    """
    from src.main import app

    paths = list(app.openapi().get("paths", {}))
    build_session_paths = [p for p in paths if "build-session" in p]

    # Counted so that a route added or removed is a decision rather than a side effect. None is
    # addressed by a session id: nothing can mint one for a client to name.
    assert len(build_session_paths) == 16, (
        f"the C3 build-session route surface changed: expected 16 paths, found "
        f"{len(build_session_paths)}. If a route was deliberately added or removed, amend C3 "
        f"and update this number in the same change.\n{sorted(build_session_paths)}"
    )


def test_the_error_signature_module_reaches_nothing_in_services() -> None:
    """The turn engine and the log configuration of every process import this, so it has to stay a
    leaf: loading it must not pull in a single `src.services` module. Mutation check: add an import
    from `src.services` to `core/error_signature.py` and this goes red."""
    result = _import_in_fresh_interpreter(
        "import importlib, sys;"
        " importlib.import_module('src.core.error_signature');"
        " print(sorted(m for m in sys.modules if m.startswith('src.services')))"
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert result.stdout.strip() == "[]", result.stdout
