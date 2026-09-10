"""The connector data plane — reaching one connected system's object store and reading bytes.

WHY THIS IS A SIBLING OF `services/storage/`, NOT A MODE INSIDE IT. `services/storage/` is the
PLATFORM's own storage — attachments and app snapshots, in the platform's own account, reached
with the platform's own identity through `DefaultAzureCredential`. A connector's lake is a
different account, reached with a DIFFERENT identity that must be named explicitly, and it is
READ-ONLY. Putting a second auth mode into the module that guards the platform's own storage
would make `DefaultAzureCredential` ambiguous exactly where it must not be; mirroring its shape
costs one package and keeps both unambiguous.

WHY IT IS `services/lake/` AND NOT `services/connectors/lake/`, which is where a reader would
first look. `services/connectors/` is the ACCESS-LEDGER package: its `__init__` re-exports
`current_access`, which reaches `src/db/models/`, which reaches `src/db/base.py`, which imports
`src.config` at module scope. `src/settings/api.py` has to import this package's `LakeConfig` to
declare the settings field — and Python runs a parent package's `__init__` before any submodule,
so nesting the lake under `connectors/` would close a
`config -> settings -> connectors -> db.models -> db.base -> config` cycle at every boot. The
same shape `src/core/connectors.py` already documents for the models package, met from the other
side. A data plane is a peer of `storage/`, `sandbox/` and `deploy/` anyway; this is the reason
it had to be.

NOTHING REACHABLE FROM THE ORM OR FROM `src.config` IS RE-EXPORTED HERE, and that is a hard
structural constraint rather than an oversight. THE RULE IS THE REACHABILITY, NOT THE FILE NAME:
`src/settings/api.py` imports `LakeConfig` to declare its settings field, and Python runs THIS
`__init__` before any submodule of it — so anything reachable from the lines below runs during
settings construction. `copy.py` is the clearest case: it reads `src/core/connectors.py`, which
reaches `src/db/models/`, which reaches `src/db/base.py`, which imports `src.config` at module
scope, so adding it to the list below closes the cycle and the process cannot boot at all.
`env.py` and `window.py` are absent for the same reason and not by accident — `env.py` reads
`src.config` directly. Import any of them by module (`from src.services.lake.copy import
schedule_window_copy`). `tests/services/lake/test_import_graph.py` fails loudly if
that ever stops being true, because none of the four static gates executes an import.

NOTHING HERE NAMES A CONNECTOR (R11). The vocabulary is `lake`, `config`, `window`, `transfer`;
which connector this is a lake FOR arrives as a `connector_key` value from the registry in
`src/core/connectors.py`.
"""

from src.services.lake.client import LakeClient as LakeClient
from src.services.lake.client import LakeEntry as LakeEntry
from src.services.lake.client import aclose_lake as aclose_lake
from src.services.lake.client import get_lake as get_lake
from src.services.lake.client import reset_lake_for_tests as reset_lake_for_tests
from src.services.lake.config import LakeConfig as LakeConfig
from src.services.lake.errors import LakeAuthError as LakeAuthError
from src.services.lake.errors import LakeError as LakeError
from src.services.lake.errors import LakeNotFoundError as LakeNotFoundError
