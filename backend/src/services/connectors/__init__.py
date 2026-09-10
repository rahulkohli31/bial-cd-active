"""Connector access — the database-backed half of the connector feature. Public surface via
explicit `from .x import Y as Y` re-exports.

WHY THIS PACKAGE EXISTS BESIDE `src/core/connectors.py`. The registry and the window resolver
are PURE — they are handed rows and decide; they never open a session — so they live in
`src/core/`, where this repo keeps its pure cross-cutting modules. Deriving a person's access
state is a QUERY, and the rule it applies is subtle enough that three routes must not each
spell it out: `GET /v1/connectors`, the request/cancel pair, and U4's per-project switch-on
refusal and reads. A shared query with a shared rule is what `src/services/` is for.

THE DATA PLANE IS NOT HERE, AND THAT IS FORCED RATHER THAN CHOSEN. Reading a connector's actual
data lives in `src/services/lake/`, a peer package — because this `__init__` reaches
`src/db/models/` and `src/settings/api.py` has to import the lake's settings group, which would
close an import cycle through `src/db/base.py`. That package's docblock spells it out.
"""

from src.services.connectors.access import ConnectorPersonState as ConnectorPersonState
from src.services.connectors.access import PersonAccess as PersonAccess
from src.services.connectors.access import current_access as current_access
