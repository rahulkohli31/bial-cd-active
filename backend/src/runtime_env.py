"""The one lazy accessor for this process's environment segment.

WHY IT IS ITS OWN MODULE, AND WHY IT IMPORTS NOTHING. Two places need to know which environment
this process is, and neither may ask `src.config` at import time: `src.config` reaches
`src.settings.capabilities`, which reaches both `src.services.redis.config` and the sandbox
config — so a module-level import from either of those closes the cycle and makes `src.config`
itself unimportable. Both had solved it the same way, separately, with the same paragraph of
explanation written out twice: `redis/keys.py::_environment` and
`sandbox/base.py::control_plane_segment`. Two copies of a subtle constraint is two places for it
to be got wrong, and the next module needing the same trick would have made three.

This sits above every service package and has no module-scope imports, so anything may import it
from anywhere. `src/__init__.py` is empty, so importing it executes this file and nothing else —
which is the property that keeps it out of the cycle.

RESOLVED PER CALL, NEVER MEMOIZED. The segment is a property of the running settings, not of
import order, which no deployment controls; and the suite rebinds settings between cases.

THE TWO CALLERS MEAN DIFFERENT THINGS BY IT and keep their own names for it. The Redis key prefix
scopes coordination state so a process pointed at the wrong instance cannot read another
environment's fleet as a spare-list (C5); the `bial-control-plane` tag decides which control plane
is entitled to judge a container (R22). They read the same value today, and nothing says they must
forever — so neither is redefined in terms of the other.
"""

from __future__ import annotations


def environment_segment() -> str:
    """This process's `ENVIRONMENT`, resolved right now."""
    from src.config import settings

    return str(settings.ENVIRONMENT)
