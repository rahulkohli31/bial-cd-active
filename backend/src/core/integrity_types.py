"""The workspace-integrity value types, in a module with no imports of its own.

WHY THIS EXISTS: a real circular import forces this leaf, the same reason `runtime_env.py` is
one. `build_sessions.__init__` reaches `appdata` → `services.projects` → `agent.agent` →
`orchestrator.__init__`, so anything under `services/orchestrator/` importing anything under
`services/build_sessions/` at module level fails at interpreter start — on the PACKAGE, so moving
the type into that package doesn't help. `reaper.py` lives with the same cycle via a
function-scoped import; that trick works for a CALL but not for a TYPE, which must be nameable in
a signature. `core/` is the sanctioned home: `src/__init__.py` and `src/core/__init__.py` are both
empty, so importing this module executes nothing else — the property that keeps it out of the
cycle.

NOT THE ONLY CUT: the cycle is caused by where `integrity.py` sits, not what it contains — moving
it out of `services/build_sessions/` would remove the need for this file too. It stays because the
rest of the workspace-integrity work lives beside it, read by `manager` and `reaper`; the health
verdict is the one type reaching in from outside. One leaf plus three deferred calls is the
smaller price.

Nothing here may grow a module-scope import — the moment it reaches for a client, a store, or
settings it stops being a leaf and the cycle returns."""

from __future__ import annotations

import enum


class BaselineIdentity(enum.StrEnum):
    """Whether an app's root route is still the seeded golden-template baseline.

    THREE VALUES because "we could not tell" is a real answer and must not be spelled as either of
    the other two. `STILL_THE_BASELINE` is the only one that may block a completion claim, and
    `UNANSWERABLE` is the only one a retry can change — a check that could not find the baseline
    can neither convict an app of showing it nor clear it of showing it."""

    STILL_THE_BASELINE = "still_the_baseline"
    DIVERGED = "diverged"
    UNANSWERABLE = "unanswerable"


class BaselineUnanswerable(enum.StrEnum):
    """WHY the serving question could not be answered, which decides what the verdict does next.

    `UNANSWERABLE` alone cannot carry that decision: one of these causes may be waved through when
    every other signal is green, and one of them must never be. Folding them into a single value
    is what let a probe that could not answer fail a build six other checks agreed was healthy.

    Only `ROOT_IS_NOT_OURS` may become advisory. A root carrying someone else's subject means
    there is no birth certificate to compare against — that is a fact about the REPOSITORY, not
    about the app, and it cannot convict a working app of showing the starter page.

    `NO_SINGLE_ROOT` must fail closed however green the rest looks, and the reason is the whole
    care in this enum: it is indistinguishable from a container reverted to its baked image, and a
    reverted container serving the untouched template compiles, serves 200, logs no crash and
    reports no browser error. All six other signals are green exactly when the app is most broken,
    so waving this one through would print a completion claim over a blank starter page — a false
    SUCCESS, which is worse than the false failure this work removes.
    """

    #: The exec did not come back. Transient: a supervisor blip is not a fact about the app, and a
    #: retry can change the answer.
    PROBE_FAILED = "probe_failed"
    #: The root commit is not the seeded template's. Structural, and the one advisory cause.
    ROOT_IS_NOT_OURS = "root_is_not_ours"
    #: No root commit, or more than one. Structural, and fails closed.
    NO_SINGLE_ROOT = "no_single_root"
    #: The root exists and never held the root route. Structural, and fails closed: clearing an app
    #: on the strength of a missing file is the same completion claim by another route.
    BASELINE_MISSING = "baseline_missing"


class WorkspaceState(enum.StrEnum):
    """Whether this container still holds the app it is supposed to hold."""

    #: The workspace holds this app's work — or there was never any to hold. The turn proceeds
    #: exactly as it does today.
    INTACT = "intact"
    #: Positively confirmed loss: the lineage is broken, the tree is empty, AND this app has
    #: been built before. The ONLY state that may restore.
    REVERTED = "reverted"
    #: Transient — an exec error, a timeout, a storage blip. Retryable, and capped: after two
    #: consecutive unreadable answers for one app the third is `UNVERIFIABLE`, so no run of bad
    #: luck can wedge a user out of their project.
    UNREADABLE = "unreadable"
    #: Structural — retrying cannot help. Proceed under alarm with one plain sentence, and never
    #: restore: an unexplained tree must not be written over the copy it cannot be checked
    #: against.
    UNVERIFIABLE = "unverifiable"
