"""Conversation-test fixtures: swap the object store for an in-memory fake so the
delete-with-cleanup sweep runs without Azurite.

The "a build is running in this thread" seam (`building`) moved up to `tests/api/v1/conftest.py`
— BOTH conversation surfaces consult that gate, so it may not live beside only one of them.
"""

from __future__ import annotations

import contextlib

import pytest

from src.config import settings
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.fakes import FakeStorage


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture(autouse=True)
def _override_storage(app, fake_storage) -> None:
    from src.api.v1.attachments.router import storage_dependency

    app.dependency_overrides[storage_dependency] = lambda: fake_storage


@pytest.fixture(autouse=True)
def _bind_a_workspace(app, fake_redis, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandbox client on BOTH seams, for every conversation test, by default.

    THIS IS NOT SCAFFOLDING — IT IS THE POINT OF R98. A turn used to answer from the last SAVED
    copy of the app when no sandbox service was configured, and "no sandbox service configured"
    is exactly what the test environment is. So the whole conversation suite was exercising the
    degraded path while believing it exercised the live one, and the day that degrade arm was
    deleted every one of these tests would have started refusing at send with nothing to say why.

    Both kinds now read the project's live app and only that, so the fixture binds what the
    product requires rather than what the old branch tolerated. The tests that are ABOUT the
    absence — R98's refusal — unbind it explicitly, which is the honest shape: the exception is
    written down at the test that needs it, not assumed by every test that does not.

    IT PULLS IN `fake_redis` FOR A REASON THAT IS NOT INCIDENTAL. Binding a workspace is what
    makes the send route's reclaim preflight reachable — with no sandbox it was skipped
    entirely — and that preflight reads the coordination store. A suite that binds one without
    the other proves the refusal it wants and then dies on a store nobody configured, which
    reads as a fixture problem rather than as what it is: the two are one deployment fact.
    """
    from src.api.v1.build_sessions.deps import sandbox_dependency, sandbox_or_none_dependency
    from src.config import settings
    from src.services.sandbox.config import SandboxConfig
    from tests.api.v1.build_sessions.conftest import _sandbox_config
    from tests.fakes import FakeSandboxClient

    assert isinstance(_sandbox_config(), SandboxConfig)
    monkeypatch.setattr(settings, "sandbox", _sandbox_config())
    client = FakeSandboxClient()
    app.dependency_overrides[sandbox_dependency] = lambda: client
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: client


@pytest.fixture
def no_workspace_service(app) -> None:
    """The R98 case, opted into by name: a deployment with no sandbox service at all.

    Overrides the autouse binding above rather than fighting it, so a test that wants the
    refusal says so in its signature and every other test keeps the live path."""
    from src.api.v1.build_sessions.deps import sandbox_or_none_dependency

    app.dependency_overrides[sandbox_or_none_dependency] = lambda: None


# =============================================================================
# The turn-driving seams, shared by the four files that drive turns
# =============================================================================
#
# These were byte-identical copies in `test_turn_stream.py`, `test_build_transition.py`,
# `test_project_grounding.py` and `test_context_gate.py` — the last two added the 3rd and 4th
# copy, and `test_build_transition.py` had already resorted to importing `_headers` out of
# ANOTHER TEST MODULE to avoid a fifth. They live here now.
#
# THEY ARE DELIBERATELY NOT `autouse`, unlike the storage and workspace fixtures above. Six other
# files in this directory drive no turns at all, and a directory-wide autouse `_override_billing`
# would rebind their billing factory for no reason. The four that need them opt in with a
# module-level `pytestmark = pytest.mark.usefixtures("_fresh_engine", "_override_billing")` —
# the same blast radius the per-module `autouse=True` had, said out loud at the module that wants
# it rather than assumed for eight.

_TTL = settings.auth.access_ttl_seconds


def _headers(user, *, with_csrf: bool = True) -> dict[str, str]:
    """A signed-in browser's cookies. `with_csrf=False` is the unsafe-method rejection case."""
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    if not with_csrf:
        return {"Cookie": f"session={jwt}"}
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


@pytest.fixture
def _fresh_engine():
    """One turn engine per test, and a clean mid-reply guard either side of it — the engine is a
    process global, so one leaked from a previous test would decide the next test's answer."""
    _mid_reply.clear()
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    yield engine
    set_turn_engine_for_tests(None)
    _mid_reply.clear()


@pytest.fixture
def _override_billing(app, db_session) -> None:
    """Bill against the test's own session, so a turn's usage row is visible to its assertions."""
    from src.api.v1.conversations._shared import billing_session_factory

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    app.dependency_overrides[billing_session_factory] = lambda: lambda: _session()


@pytest.fixture
def set_chat_model(app):
    def _set(model) -> None:
        from src.api.v1.conversations._shared import chat_model

        app.dependency_overrides[chat_model] = lambda: model

    return _set
