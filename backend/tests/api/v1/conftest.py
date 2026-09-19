"""Fixtures shared by every v1 API surface.

`building` lives here rather than under `conversations/` because the gate it exercises is the
one-per-user workspace, which more than one surface consults. A fixture parked next to one
consumer is how the others end up untested.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Iterator

import pytest

from src.api.v1.build_sessions.deps import session_manager_dependency
from src.services.build_sessions import SessionManager
from src.services.build_sessions.manager import BuildSession
from src.services.sandbox import SandboxHandle


@pytest.fixture
def building(
    app,
) -> Iterator[Callable[[uuid.UUID], contextlib.AbstractContextManager[None]]]:
    """Hold this user's one workspace with a genuinely live session, so the routes' workspace
    gate answers through the REAL `SessionManager.active_session_for` rather than a stub of it.

    Hand-built rather than started for real: what these tests exercise is the ROUTE's refusal,
    and a real start would drag in Redis, a sandbox, and a brain to prove a lookup. The session
    is PROVISIONING with no `turn_finish`, which is what "genuinely working" looks like to
    `is_letting_go_of_the_workspace` — an ended session is waited for, not refused."""
    manager = SessionManager()
    app.dependency_overrides[session_manager_dependency] = lambda: manager

    @contextlib.contextmanager
    def _live(user_id: uuid.UUID) -> Iterator[None]:
        session = BuildSession(
            session_id=uuid.uuid7(),
            user_id=user_id,
            project_id=uuid.uuid4(),
            app_id=uuid.uuid4(),
            prompt="build it",
            lock_token="tok",
            handle=SandboxHandle(
                fqdn="x.example",
                token="t",
                app_name="sbx-x",
                preview_url="https://x.example/",
                ready=False,
            ),
        )
        manager._sessions[session.session_id] = session
        manager._active_by_user[user_id] = session.session_id
        try:
            yield
        finally:
            manager._sessions.pop(session.session_id, None)
            manager._active_by_user.pop(user_id, None)

    yield _live
