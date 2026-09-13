"""The connector half of a build's environment — and the gate that decides whether a container
gets a credential to BIAL's flight data.

THIS IS THE SECURITY BOUNDARY OF THE DATA-PLANE PASS, AND IT IS ASSERTED IN TWO PLACES ON PURPOSE.
The coordinates are labels; the managed identity is the grant. A test that only covered the
coordinate builder would pass while every build on the platform carried a credential — so the
last section here asserts on the BUILT ARM ENVELOPE, which is what ARM actually receives.

WHY THE NAMES ARE GENERATED AND WHY THAT NEEDS ITS OWN TEST. `backend/src/` may not contain the
connector's name, so the backend builds `BIAL_<KEY>_URL` from the registry key while
`sandbox/supervisor/app.py` hardcodes the literal — two independent producers of one fact. They
fail closed if they disagree (the child simply never sees a name the allowlist does not carry),
which is safe and completely silent, so the agreement is asserted rather than trusted.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Final

import pytest
from azure.mgmt.appcontainers import models as aca_models
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.core.connectors import CONNECTORS
from src.db.models.connector_access import ConnectorRequestStatus
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.services.build_sessions.appconnector_env import build_connector_env
from src.services.lake.config import LakeConfig
from src.services.lake.env import connector_env_names, identity_resource_id_for_env
from src.services.sandbox.aca import AcaControlPlane
from src.services.sandbox.config import SandboxConfig
from tests.api.v1.connectors.conftest import KEY, seed_decision
from tests.factories import ProjectFactory, UserFactory

_LAKE_URL: Final = "https://alakeaccount.blob.core.windows.net/acontainer/AOS/reports/"
_CLIENT_ID: Final = "52b74947-0621-46e2-a523-a6b466f47c33"
_RESOURCE_ID: Final = (
    "/subscriptions/80d1bab5-0000-0000-0000-000000000000/resourcegroups/bial-cd-rg"
    "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/an-identity"
)
_REPO_ROOT: Final = Path(__file__).resolve().parents[3].parent


@pytest.fixture
def lake_configured(monkeypatch: pytest.MonkeyPatch) -> LakeConfig:
    config = LakeConfig(
        url=_LAKE_URL, identity_client_id=_CLIENT_ID, identity_resource_id=_RESOURCE_ID
    )
    monkeypatch.setattr(settings, "connector_lake", config)
    return config


async def _project(
    db: AsyncSession,
    *,
    access: ConnectorRequestStatus | None = ConnectorRequestStatus.APPROVED,
    enabled: bool = True,
    with_row: bool = True,
    email: str = "citizen@rvaiglobal.com",
) -> tuple[uuid.UUID, uuid.UUID]:
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user_id=user.id)
    if access is not None:
        await seed_decision(db, user.id, access, None)
    if with_row:
        db.add(
            ProjectConnector(
                project_id=project.id,
                connector_key=KEY,
                enabled=enabled,
                window_kind=ConnectorWindowKind.RELATIVE,
                window_days=7,
            )
        )
    await db.flush()
    return user.id, project.id


# --- the two values -------------------------------------------------------------------------


async def test_an_approved_switched_on_project_gets_exactly_two_values(
    db_session: AsyncSession, lake_configured: LakeConfig
) -> None:
    """★ TWO, AND NO WINDOW DATES. An earlier draft sent the resolved pair so the rail's
    "Reading N days" promise was backed by something in the container. Nothing would have read
    them: the verified worked example reads exactly these two variables, a published app is
    uncapped by ruling, and nothing in this pass tells the agent the lake exists. So the dates
    would have been two more variables, two more allowlist rows, two more documentation rows and
    an assertion for a value with no reader."""
    user_id, project_id = await _project(db_session)

    env = await build_connector_env(db_session, user_id=user_id, project_id=project_id)

    url_name, client_id_name = connector_env_names(KEY)
    assert env == {url_name: _LAKE_URL, client_id_name: _CLIENT_ID}


def test_the_names_are_built_from_the_connector_key() -> None:
    """`backend/src/` may not contain the connector's name (R18), so the names are generated —
    which also means a second connector names its own variables with no code change here."""
    assert connector_env_names("dice") == ("BIAL_DICE_URL", "BIAL_DICE_CLIENT_ID")
    assert connector_env_names("some-other-system") == (
        "BIAL_SOME_OTHER_SYSTEM_URL",
        "BIAL_SOME_OTHER_SYSTEM_CLIENT_ID",
    )


def _supervisor_allowlist() -> set[str]:
    """The names in `sandbox/supervisor/app.py`'s `_INJECTED_ENV`, read by PARSING the file.

    Parsed rather than imported: `app.py` resolves `SUPERVISOR_TOKEN`, `WORKSPACE` and
    `pwd.getpwnam(APP_USER)` at module import, so importing it from the backend suite would need
    three environment variables seeded for a question about a literal tuple. Parsed rather than
    grepped for the same reason `test_template_next_config.py` evaluates rather than greps: a
    text search is satisfied by a comment."""
    tree = ast.parse((_REPO_ROOT / "sandbox" / "supervisor" / "app.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if "_INJECTED_ENV" not in names or node.value is None:
            continue
        return {
            call.args[0].value
            for call in ast.walk(node.value)
            if isinstance(call, ast.Call)
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        }
    raise AssertionError("_INJECTED_ENV is no longer a literal tuple in the supervisor")


def test_every_generated_name_is_on_the_supervisors_allowlist() -> None:
    """★ TWO INDEPENDENT PRODUCERS OF ONE FACT, asserted to agree for EVERY registry entry.

    The backend generates the name; the supervisor hardcodes it, because `sandbox/` sits outside
    the search that keeps the connector's name out of `backend/src/`. They fail CLOSED when they
    disagree — the child's environment is built from an empty dict, so a name the allowlist does
    not carry simply never arrives — which is safe and entirely silent. An app would build green,
    read no flight data, and report that the connector is switched off."""
    allowlist = _supervisor_allowlist()
    assert allowlist, "the parse found no names at all — the guard is broken, not passing"

    for connector_key in CONNECTORS:
        for name in connector_env_names(connector_key):
            assert name in allowlist, (
                f"{name} is generated by the backend for {connector_key!r} but is not in the "
                "supervisor's _INJECTED_ENV — the child would never see it, silently"
            )


def test_azures_own_identity_pair_is_on_the_allowlist_too() -> None:
    """Without these the coordinates are inert: Azure injects `IDENTITY_ENDPOINT`/`IDENTITY_HEADER`
    into the parent environment the moment an identity is attached, and the child env is built
    from an empty dict. The failure otherwise reads as a missing role assignment."""
    allowlist = _supervisor_allowlist()

    assert {"IDENTITY_ENDPOINT", "IDENTITY_HEADER"} <= allowlist


# --- the gate on the coordinates ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("access", "enabled", "with_row"),
    [
        (ConnectorRequestStatus.APPROVED, False, True),
        (ConnectorRequestStatus.PENDING, True, True),
        (ConnectorRequestStatus.DECLINED, True, True),
        (ConnectorRequestStatus.CANCELLED, True, True),
        (None, True, True),
        (ConnectorRequestStatus.APPROVED, True, False),
    ],
    ids=["switched-off", "pending", "declined", "cancelled", "never-asked", "never-switched-on"],
)
async def test_a_connector_that_is_not_effectively_on_yields_nothing(
    db_session: AsyncSession,
    lake_configured: LakeConfig,
    access: ConnectorRequestStatus | None,
    enabled: bool,
    with_row: bool,
) -> None:
    """One branch reached six ways — the switch AND the approval, read off `resolve_window` as a
    single `effectively_on`. Written as one parametrised test because writing them separately
    reads as six behaviours and drifts into six rules."""
    user_id, project_id = await _project(
        db_session, access=access, enabled=enabled, with_row=with_row
    )

    assert await build_connector_env(db_session, user_id=user_id, project_id=project_id) == {}


async def test_an_unconfigured_lake_yields_nothing_and_provisioning_proceeds(
    db_session: AsyncSession,
) -> None:
    """The developer-machine posture. Binds no lake fixture: with one bound this branch is
    unreachable by construction, which is how a documented off-posture stays broken on every
    deployment that has the dependency switched off."""
    user_id, project_id = await _project(db_session)

    assert settings.connector_lake is None
    assert await build_connector_env(db_session, user_id=user_id, project_id=project_id) == {}


async def test_another_persons_project_yields_nothing(
    db_session: AsyncSession, lake_configured: LakeConfig
) -> None:
    """★ THE ISOLATION PREDICATE. `project_connectors` carries no user column of its own, so the
    ownership claim rides a join on `projects` in the same WHERE clause. Drop it and an approved
    citizen's build would be handed the coordinates another citizen's project was granted."""
    _owner, project_id = await _project(db_session)
    stranger = await UserFactory.create(db_session, email="stranger@rvaiglobal.com")
    await seed_decision(db_session, stranger.id, ConnectorRequestStatus.APPROVED, None)

    assert await build_connector_env(db_session, user_id=stranger.id, project_id=project_id) == {}


async def test_a_database_failure_propagates_rather_than_reading_as_off(
    db_session: AsyncSession, lake_configured: LakeConfig
) -> None:
    """★ "OFF" AND "BROKEN" MUST NOT ARRIVE AT THE CALLER LOOKING THE SAME. `{}` means this
    project is not reading a connector; a substrate that failed means the platform does not know,
    and swallowing it would start a container that silently reads nothing."""

    class _BrokenSession:
        async def scalars(self, *_: object, **__: object) -> object:
            raise RuntimeError("the database went away mid-provision")

    from typing import cast

    with pytest.raises(RuntimeError, match="went away"):
        await build_connector_env(
            cast("AsyncSession", _BrokenSession()),
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
        )


# --- the gate on the GRANT, asserted on the built ARM envelope ---------------------------------


def _envelope(env: dict[str, str]) -> aca_models.ContainerApp:
    """The container spec ARM would actually receive for a container born with `env`.

    Built through `__new__` so no ARM client and no `DefaultAzureCredential` is constructed —
    `_envelope` is pure, which is exactly what makes the approval gate assertable with no Azure
    at all. The `identity_resource_id` is derived here the same way `client.py` derives it, from
    the coordinates in `env`, because THAT derivation is half of what is under test."""
    plane = AcaControlPlane.__new__(AcaControlPlane)
    plane._config = SandboxConfig(  # noqa: SLF001
        subscription_id="s",
        resource_group="r",
        region="westeurope",
        managed_environment_name="aca-env",
        acr_server="acr.azurecr.io",
        acr_username="acr-user",
        acr_password=SecretStr("acr-pass"),
        image_ref="acr/img:latest",
    )
    return plane._envelope(  # noqa: SLF001
        env,
        {"bial-control-plane": "test"},
        identity_resource_id=identity_resource_id_for_env(env),
    )


def test_an_approved_project_gets_the_identity_block(lake_configured: LakeConfig) -> None:
    """★ THE ONE LINE THAT MAKES ANY OF IT WORK. Without it the coordinates are inert and the
    failure reads as a role-assignment problem."""
    url_name, client_id_name = connector_env_names(KEY)
    envelope = _envelope({url_name: _LAKE_URL, client_id_name: _CLIENT_ID})

    assert envelope.identity is not None
    assert envelope.identity.type == "UserAssigned"
    assert list(envelope.identity.user_assigned_identities or {}) == [_RESOURCE_ID]


def test_a_container_without_coordinates_gets_no_identity_at_all(
    lake_configured: LakeConfig,
) -> None:
    """★ THE ASSERTION THAT STOPS THE APPROVAL GATE BECOMING DECORATIVE.

    The lake is CONFIGURED here — the platform could attach an identity — and the container still
    gets none, because this env carries no coordinates. Attaching whenever a lake is merely
    configured platform-wide would hand every citizen's build on the platform a credential to
    BIAL's flight data and defeat the approval gate the whole connector feature exists to enforce.
    Testing only the coordinate builder would pass while exactly that shipped."""
    envelope = _envelope({"BIAL_APP_ID": str(uuid.uuid4())})

    assert envelope.identity is None


def test_an_unapproved_projects_spec_is_byte_identical_to_one_with_no_lake(
    lake_configured: LakeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ ASSERTED AS AN EQUALITY, not as a list of fields that happen to match — which is why
    `_user_assigned` returns `None` rather than an empty `ManagedServiceIdentity(type="None")`."""
    plain_env = {"BIAL_APP_ID": "00000000-0000-7000-8000-000000000001"}
    with_lake = _envelope(plain_env).as_dict()

    monkeypatch.setattr(settings, "connector_lake", None)
    without_lake = _envelope(plain_env).as_dict()

    assert with_lake == without_lake
    assert "identity" not in with_lake


def test_the_identity_and_the_coordinates_cannot_be_separated(
    lake_configured: LakeConfig,
) -> None:
    """★ THE INVARIANT, stated as a property rather than left to two call sites to remember.

    `identity_resource_id_for_env` derives the grant from the coordinates, so "has the
    coordinates" and "has the credential" are the same fact. Anyone replacing it with a separate
    boolean has to make this pass."""
    url_name, client_id_name = connector_env_names(KEY)
    granted = {url_name: _LAKE_URL, client_id_name: _CLIENT_ID}

    for env in ({}, {"BIAL_APP_ID": "x"}, {client_id_name: _CLIENT_ID}, granted):
        has_coordinates = bool(env.get(url_name))
        assert (identity_resource_id_for_env(env) is not None) is has_coordinates
