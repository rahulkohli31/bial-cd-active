"""Build-sessions domain package.

Deliberately empty of re-exports: every consumer imports the submodule it needs directly.

WHY THIS EXISTS
This package MUST NOT re-export `deps` or `router` at package level. Doing so drags
FastAPI, the session manager and the whole route tree into any process that merely wants
a schema from here, which is what made `src.services.build_sessions.reaper` unimportable
outside the API process — the blocker to running the scheduled sandbox sweep on a worker at
all. Pinned by `tests/test_import_graph.py`, which imports in a COLD-CACHE SUBPROCESS — an
in-process assertion passes spuriously because conftest has already imported the app.
"""
