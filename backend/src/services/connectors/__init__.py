"""The database-backed half of the connector feature. Public surface via explicit
`from .x import Y as Y` re-exports.

WHY THIS PACKAGE EXISTS BESIDE `src/core/connectors.py`. The registry and the window resolver
are PURE — they are handed rows and decide; they never open a session — so they live in
`src/core/`, where this repo keeps its pure cross-cutting modules. Loading a project's connector
rows and resolving them once for a turn is a QUERY, read by both turn routes, and a shared query
is what `src/services/` is for.

THE DATA PLANE IS NOT HERE, AND THAT IS FORCED RATHER THAN CHOSEN. Reading a connector's actual
data lives in `src/services/lake/`, a peer package — because this `__init__` reaches
`src/db/models/` and `src/settings/api.py` has to import the lake's settings group, which would
close an import cycle through `src/db/base.py`. That package's docblock spells it out.
"""

from src.services.connectors.connected import (
    connected_systems_for_project as connected_systems_for_project,
)
