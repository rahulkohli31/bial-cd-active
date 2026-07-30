"""Fixtures shared by every v1 API surface.

`building` lives here rather than under `conversations/` because the ONE GATE it exercises has
TWO consumers: the U10 turn route and the legacy `POST /v1/claude` relay. A fixture parked next
to one of them is how the other surface ends up untested — and an ungated send path is a way
around the gate, not a gap in coverage.
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
) -> Iterator[Callable[[uuid.UUID, uuid.UUID], contextlib.AbstractContextManager[None]]]:
    """Register a genuinely live build session against a conversation, so the routes' ONE gate
    ("is the agent working in this thread right now?") answers through the REAL
    `SessionManager.live_session_for_conversation` rather than a stub of it.

    Hand-built rather than started for real: what these tests exercise is the ROUTE's refusal,
    and a real start would drag in Redis, a sandbox, and a brain to prove a lookup."""
    manager = SessionManager()
    app.dependency_overrides[session_manager_dependency] = lambda: manager

    @contextlib.contextmanager
    def _live(conversation_id: uuid.UUID, user_id: uuid.UUID) -> Iterator[None]:
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
            conversation_id=conversation_id,
        )
        manager._sessions[session.session_id] = session
        manager._active_by_user[user_id] = session.session_id
        try:
            yield
        finally:
            manager._sessions.pop(session.session_id, None)
            manager._active_by_user.pop(user_id, None)

    yield _live
