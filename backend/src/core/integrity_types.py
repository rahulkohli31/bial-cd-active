"""The workspace-integrity value types, in a module with no imports of its own.

A LEAF IN `core/` FOR THE REASON `runtime_env.py` IS ONE, and it is a circular import that is real
rather than incidental. `src.services.build_sessions.__init__` reaches `appdata` →
`services.projects` → `agent.agent` → `services.orchestrator.__init__`, so anything under
`services/orchestrator/` that imports anything under `services/build_sessions/` at module level
fails at interpreter start — and it fails on the PACKAGE, so moving the type to a leaf module
inside that package does not help. `reaper.py` has lived with the same cycle for as long as it has
existed and solves it with a function-scoped import.

That answer works for a CALL and not for a TYPE: a verdict the health check stores on its own
outcome has to be nameable in a signature, and a function-scoped import cannot name it. `core/`
is the sanctioned home — both `src/__init__.py` and `src/core/__init__.py` are empty, so importing
this executes this file and nothing else, which is the whole property that keeps it out of the
cycle (`runtime_env.py` says the same thing at length).

NOT THE ONLY CUT, and the alternative is worth naming rather than leaving to be re-derived. The
cycle is caused by where `integrity.py` SITS, not by what it contains — it imports only the
sandbox client, the object store and structlog, none of which reach the orchestrator — so moving
that module out of `services/build_sessions/` entirely would remove the need for this file and for
the function-scoped call wrappers. It stays where it is because the rest of the workspace-integrity
work lands beside it and is read by `manager` and `reaper`, both of which live in that package; the
one module reaching in from outside is the health verdict. One leaf plus three deferred calls is
the smaller price.

Nothing here may grow a module-scope import. The moment this reaches for a client, a store or a
settings object it stops being a leaf and the cycle comes back."""

from __future__ import annotations

import enum


class BaselineIdentity(enum.StrEnum):
    """Whether an app's root route is still the seeded golden-template baseline (U6, R9).

    THREE VALUES because "we could not tell" is a real answer and must not be spelled as either of
    the other two. `STILL_THE_BASELINE` is the only one that may block a completion claim, and
    `UNANSWERABLE` is the only one a retry can change — a check that could not find the baseline
    can neither convict an app of showing it nor clear it of showing it."""

    STILL_THE_BASELINE = "still_the_baseline"
    DIVERGED = "diverged"
    UNANSWERABLE = "unanswerable"
