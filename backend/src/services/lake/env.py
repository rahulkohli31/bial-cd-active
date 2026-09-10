"""What a container is told about the lake, and whether it is given the identity to read it.

TWO VALUES AND ONE GRANT, AND THEY MOVE TOGETHER. The two values are labels — a URL and a client
id — and on their own they grant nothing. The grant is the user-assigned managed identity attached
to the container app: a container that has one can list and download the whole flight container
whether or not it was ever told where to look. So gating the coordinates while attaching the
identity unconditionally would be theatre, and it would hand every citizen's build on the platform
a credential to BIAL's flight data. **The same predicate answers both**, and this module is where
that is made structural rather than remembered: `identity_resource_id_for_env` derives the grant
from the presence of the coordinates, so a caller cannot attach one without the other.

THE NAMES ARE GENERATED FROM THE CONNECTOR'S KEY. `backend/src/` may not contain the connector's
name (R11, enforced by a word-boundary grep), so the container's `BIAL_DICE_URL` is built here as
`f"BIAL_{key.upper()}_URL"` and never written down. `sandbox/supervisor/app.py` hardcodes the
literals, because `sandbox/` sits outside that search — the two are produced independently and are
asserted to agree for every registry entry. They fail CLOSED when they disagree: the backend sets
a name the supervisor's allowlist does not carry, and the child simply never sees it.

NOT RE-EXPORTED FROM THIS PACKAGE'S `__init__`. It reads the connector registry, which reaches
`src/db/models/`, and `src/settings/api.py` imports this package's `config` module — see the
`__init__` docblock and `tests/services/lake/test_import_graph.py`.
"""

from __future__ import annotations

from typing import Final

from src.core.connectors import CONNECTORS

# The prefix every injected variable shares, so a reader inside a workspace can tell what the
# platform put there from what their own app added.
_PREFIX: Final = "BIAL_"

# ONE LAKE, ONE CONNECTOR — AND THE SECOND ONE MUST NOT SILENTLY INHERIT THE FIRST'S.
# `settings.connector_lake` is a SINGLE global block. `lake_env_for` uses the connector key only
# to NAME the two variables and hands back the same URL and client id whatever key it is asked
# for; `identity_resource_id_for_env` attaches that one identity if ANY connector's URL is
# present. With one registry entry that is exact. With two it would hand connector B's container
# connector A's flight data — and connector A's managed identity — under an approval the citizen
# gave for a different system, while this module, `copy.py` and the registry all promise that a
# second connector is "just a registry entry".
#
# So the promise is made structural instead of remembered. This fails at IMPORT, in every
# environment, the moment a second entry is added — not at the first read in production, and not
# as a wrong answer nobody notices. Whoever adds the entry has to key the lake configuration by
# connector key first, which was always the change the second connector actually required.
if len(CONNECTORS) > 1:  # one registry entry today; this is the guard for the day there are two
    raise RuntimeError(
        "connector_lake is a single global configuration block and cannot serve more than one "
        "connector: key the lake settings by connector key before adding a second registry entry"
    )


def connector_env_names(connector_key: str) -> tuple[str, str]:
    """`(url_name, client_id_name)` for one connector key — the ONE place either is spelled.

    `-` becomes `_` because a connector key is a lowercase slug and an environment variable name
    cannot carry a hyphen; a key that produced an unusable name would fail silently, since the
    supervisor's allowlist would simply never match it."""
    slug = connector_key.upper().replace("-", "_")
    return f"{_PREFIX}{slug}_URL", f"{_PREFIX}{slug}_CLIENT_ID"


def lake_env_for(connector_key: str) -> dict[str, str]:
    """The two values a container needs to read this connector's lake, or `{}` when no lake is
    configured.

    NO WINDOW DATES. An earlier draft sent the resolved pair so the rail's "Reading N days"
    promise was backed by something in the container; that was wrong. The verified worked example
    reads exactly these two variables, nothing in this pass tells the agent the lake exists, and a
    published app is uncapped by ruling — so the dates would have been two more variables, two
    more allowlist rows, two more documentation rows and an assertion for a value with no reader.
    The window's only job in this pass is deciding which files transfer.
    """
    # LOCAL, AND NOT FOR AN IMPORT CYCLE — hoisting this to module scope has been tried and the
    # process boots. It stays local because this package is where two import directions meet:
    # `src/settings/api.py` reaches `lake/config.py` from the settings side while this module is
    # reached from the ORM side, and none of the four static gates executes an import, so the day
    # that stops being safe would be found in production rather than in CI.
    from src.config import settings

    lake = settings.connector_lake
    if lake is None:
        return {}
    url_name, client_id_name = connector_env_names(connector_key)
    return {url_name: lake.url, client_id_name: lake.identity_client_id}


def identity_resource_id_for_env(app_env: dict[str, str]) -> str | None:
    """The ARM resource id of the identity this container should be given, or `None`.

    DERIVED FROM THE COORDINATES ALREADY IN THE ENVIRONMENT, and that is the whole point. The
    access decision — a lake configured, the connector switched on for this project, the owner
    approved — is made ONCE, by `build_connector_env`, and its answer is the presence or absence
    of these names. Re-deriving it here would be a second place the platform decides who may read
    BIAL's flight data, and two such places eventually disagree. Reading it back out of the env
    dict makes "identity attached" and "coordinates present" the same fact rather than two facts
    that have to be kept in step.

    Returns `None` for every container that was not given coordinates, which is every container on
    a deployment with no lake, every project with the connector off, and every project whose owner
    is pending, declined or has never asked.
    """
    # LOCAL, AND NOT FOR AN IMPORT CYCLE — hoisting this to module scope has been tried and the
    # process boots. It stays local because this package is where two import directions meet:
    # `src/settings/api.py` reaches `lake/config.py` from the settings side while this module is
    # reached from the ORM side, and none of the four static gates executes an import, so the day
    # that stops being safe would be found in production rather than in CI.
    from src.config import settings

    lake = settings.connector_lake
    if lake is None:
        return None
    for connector_key in CONNECTORS:
        url_name, _ = connector_env_names(connector_key)
        if app_env.get(url_name):
            return lake.identity_resource_id
    return None
