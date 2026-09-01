"""Conversation-test fixtures: swap the object store for an in-memory fake so the
delete-with-cleanup sweep runs without Azurite.

The "a build is running in this thread" seam (`building`) moved up to `tests/api/v1/conftest.py`
— BOTH conversation surfaces consult that gate, so it may not live beside only one of them.
"""

from __future__ import annotations

import pytest

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
