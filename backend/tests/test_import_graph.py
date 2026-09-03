"""The import graph must let a NON-FastAPI process import `src/`.

WHY THIS EXISTS. Reclamation moves out of the API process onto a Taskiq worker (ADR-0011,
ADR-0029). That worker imports `src.services.build_sessions.reaper` and
`src.services.deploy.reconcile` without ever building a FastAPI app. Before U3 those imports
FAILED — not because the services needed the API, but because
`src/api/v1/build_sessions/__init__.py` re-exported `deps` and `router` at package level, so
touching any C7 schema dragged the whole route tree in behind it. Those six lines were dead:
every real consumer already imported the submodule.

WHY A SUBPROCESS, AND WHY IT IS NOT CEREMONY. `tests/conftest.py` imports `src.main` before any
test runs, so by the time an in-process assertion executes, every module it could ask about is
already in `sys.modules` and the test passes no matter what the import graph does. Only a fresh
interpreter proves the standalone import. `-B` keeps the run from writing bytecode back.

WHAT WOULD BREAK THIS. Re-adding a package-level `deps`/`router` re-export to
`src/api/v1/build_sessions/__init__.py` — mutation-checked once by hand when U3 landed, and the
reason `test_the_package_does_not_drag_in_the_route_tree` asserts on loaded SUBMODULES rather
than on source text: a source-text check passes against a re-export spelled a new way.
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
    """The worker's reclamation task imports this. Before U3 it raised on a cold interpreter."""
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
    """U1 — `manager.py` AND `reaper.py` both import this module at module level, so it must not
    reach back into either of them, or into the orchestrator.

    LOADED BY FILE PATH, DELIBERATELY, and this is the honest version of the claim. Importing
    `src.services.build_sessions.integrity` by name runs the PACKAGE `__init__`, which imports
    `manager` -> `appdata` -> `services.projects` -> `agent.agent` and therefore `pydantic_ai` —
    that is already true of `reaper` today and is not this module's doing. What this test pins is
    the property that IS this module's doing: its own module-level imports stay cheap, so it can
    never become the reason a worker loads the agent stack, and it can never close the
    `build_sessions` <-> `orchestrator` cycle that forced `selfheal` and `harness` to defer
    theirs.

    NOT ASSERTED, because it would be a false claim: `fastapi` still arrives, pulled in by
    `src.services.sandbox` and `src.services.storage`, which every module in this area already
    loads. The three names below are the ones this module could plausibly grow and must not.

    Mutation check: add `from src.services.build_sessions.manager import SessionManager` (or any
    `src.services.orchestrator` import) at the top of `integrity.py` and this goes red."""
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
    """THE CYCLE IS THE WHOLE REASON `src/core/runtime_env.py` EXISTS.

    `src.config` reaches `src.settings.api`, which reaches both
    `src.services.redis.config` and the sandbox config. So the modules that need "which
    environment is this" — the Redis
    key prefix and the `bial-control-plane` tag — cannot ask at import time, and each had written
    its own function-scoped import with the same paragraph of explanation. One leaf accessor
    replaces both, and it is only safe while it imports NOTHING at module scope.

    Asserted on a cold interpreter for the reason this whole file exists: `conftest` imports
    `src.main` first, so in-process every module is already resolved and a cycle proves nothing.

    Mutation-check: hoist `from src.config import settings` to the top of `src/core/runtime_env.py`
    and this goes red."""
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
    """The first passenger on the scheduler (U6) — proven importable before it is scheduled."""
    result = _import_in_fresh_interpreter(
        "import importlib; importlib.import_module('src.services.deploy.reconcile'); print('ok')"
    )
    assert result.returncode == 0, (
        f"deploy.reconcile is not importable standalone.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ok" in result.stdout


def test_the_package_does_not_drag_in_the_route_tree() -> None:
    """THE REGRESSION GUARD. Importing the build-sessions package for a C7 schema must not load
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
        "the build-sessions package re-exports the route tree again — a worker importing a C7 "
        f"schema now pulls in {dragged_in}. Keep the schemas re-exports; drop deps/router."
    )


def test_the_c7_schema_re_exports_survive() -> None:
    """The other half of U3: C7 freezes these shapes AT THIS LOCATION, so the cleanup must not
    have taken them with it."""
    result = _import_in_fresh_interpreter(
        "from src.api.v1.build_sessions import ProgressEnvelope, RunBuild, StartBuildRequest;"
        " print('ok')"
    )
    assert result.returncode == 0, (
        f"a C7 schema re-export was lost.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_the_app_still_builds_with_its_full_route_surface() -> None:
    """The app is the other consumer of that package, and it reaches the router by SUBMODULE
    import (`src/api/v1/router.py`), which U3 did not touch. Pinned on the C3 build-session
    surface specifically: that is the contract the removed `router` re-export sat next to, so a
    regression would show up here first.

    `app.openapi()` rather than `app.routes` — this FastAPI defers router inclusion behind
    `_IncludedRouter`, so `app.routes` reports a handful of top-level entries and would pass
    while every real route was missing.
    """
    from src.main import app

    paths = list(app.openapi().get("paths", {}))
    build_session_paths = [p for p in paths if "build-session" in p]

    # 19 since U13 added `projects/{project_id}/client-error` (the app's own in-browser report),
    # U11 added `projects/{project_id}/compile-state` (the compile signal for a tab with no live
    # turn — the turn stream's producer stops at the terminal), and U4 added
    # `projects/{project_id}/workspace-check` (the idle-tab integrity probe, for the reversion
    # that happens while nobody is sending messages), and U25 added the two superadmin operator
    # routes for the trees U2 and U3 park (`internal/apps/{app_id}/parked` and `.../promote`) —
    # without a reader those objects would be write-only, and in a false-reversion they hold the
    # only copy of somebody's work. Each recorded in C3 §9 in the same change that added it.
    # 17 since U28 retired `lock/acquire` / `lock/renew` / `lock/release` / `heartbeat` (21 - 4):
    # nothing called them — the portal's keep-alive loop that was their only caller was itself
    # deleted back in U13. `lock/force-end` is the one lock op still reachable from the UI.
    # 18 since plan 002 U9 added `projects/{project_id}/stop-state`: the drain's ask
    # (`stop-active-build`) now RETURNS IMMEDIATELY and a detached task does the waiting, so the
    # outcome needs a reader. Holding a request open for the length of a stop was a dependency
    # nobody could satisfy — the budget had to sit under the request timeout of a gateway owned by
    # the client's network — and the outcome it was hiding is three states, not a boolean.
    assert len(build_session_paths) == 18, (
        f"the C3 build-session route surface changed: expected 18 paths, found "
        f"{len(build_session_paths)}. If a route was deliberately added or removed, amend C3 "
        f"and update this number in the same change.\n{sorted(build_session_paths)}"
    )
