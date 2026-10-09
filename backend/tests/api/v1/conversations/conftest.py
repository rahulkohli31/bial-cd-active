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

    Without this, a turn silently answers from the last SAVED copy when no sandbox is
    configured — exactly the state an unbound test env is in. Tests ABOUT that absence unbind
    it via `no_workspace_service`. Pulls in `fake_redis` too: binding a workspace is what makes
    the send route's reclaim preflight reachable, and that preflight reads the coordination
    store — the two are one deployment fact, not two independent fixtures."""
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
def shared_storage(fake_storage, monkeypatch):
    """ONE store for both consumers of it. The upload route takes its store by injected
    dependency; the send route's rehydrator reaches the accessor-level `get_storage()`. The
    directory fixture binds only the first, so a test that uploads and then SENDS the upload
    needs the accessor bound to the same object or the send answers 503 and proves nothing."""
    from src.services.storage import accessor

    monkeypatch.setattr(accessor, "_backend_singleton", fake_storage)
    return fake_storage


@pytest.fixture
def no_workspace_service(app) -> None:
    """The no-sandbox case the binding above guards against, opted into by name: a deployment
    with no sandbox service at all.

    Overrides the autouse binding above rather than fighting it, so a test that wants the
    refusal says so in its signature and every other test keeps the live path."""
    from src.api.v1.build_sessions.deps import sandbox_or_none_dependency

    app.dependency_overrides[sandbox_or_none_dependency] = lambda: None


# --- The turn-driving seams, shared by the four files that drive turns -----------------
#
# DELIBERATELY NOT `autouse`, unlike the storage and workspace fixtures above: six other files
# in this directory drive no turns at all, and a directory-wide autouse `_override_billing`
# would rebind their billing factory for no reason. The four that need them opt in with a
# module-level `pytestmark = pytest.mark.usefixtures("_fresh_engine", "_override_billing")`.

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
