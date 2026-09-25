"""The build-outcome record: the reason tokens a stored row can carry.

Rows of this shape are PERMANENT in the production transcript, and nothing in `src` appends one
any more — a build runs as an ordinary Write chat turn and records its ending as a
`turn_terminal` row instead. The writer that produces a faithful row for a test lives in
`tests/fakes.py`.
"""

from __future__ import annotations

from typing import Final

# The graceful end reasons whose prose differs from a natural finish. The token and the sentence
# it produces must move together, because a drifted token does not fail loudly — it falls
# straight back through to "Build finished.".
#
# SPELLED HERE RATHER THAN IMPORTED FROM `turns/copy.END_REASONS`, which is where every other
# producer's reason lives: `src/services/turns/__init__` imports the turn engine, and the engine
# imports this package, so a module-level import of anything under `turns` from here is a cycle.
# `tests/services/turns/test_end_reasons.py` holds these four against that collection instead.
STOPPED_BY_USER: Final = "stopped_by_user"
FORCE_ENDED: Final = "force_ended"
# The idle reaper's reason — part of the documented terminal set (`build_sessions/schemas.py`).
IDLE_TEARDOWN: Final = "idle_teardown"
# A spent daily budget, raised by the turn engine.
QUOTA_EXCEEDED: Final = "quota_exceeded"
