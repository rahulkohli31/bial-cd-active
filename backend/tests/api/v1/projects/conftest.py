"""Project-test fixtures: swap the object store for an in-memory fake (cascade-delete blob
sweep), inject a chat model, and bind the billing drain to the test session, so the
description/code-seed tests — which now reach the model through
`POST /{project_id}/description:generate`, not the retired `/v1/claude` relay — roll back
cleanly."""

from __future__ import annotations

import contextlib

import pytest

from tests.fakes import FakeStorage

# THE DELETE BODY, in one place. `DELETE /v1/projects/{id}` requires a signed reason (#158
# §13), and roughly thirty tests in this directory delete a project as a SETUP step — teardown,
# cascade and ownership cases that care about what the delete destroys, not about what the
# body must contain. Inlining the JSON at each of them meant the last required field was a
# thirty-site sweep, and the next one would be too.
#
# The validation cases deliberately do NOT use this: `test_delete_remark.py` builds its own
# bodies, because a constant that always satisfies the rules cannot test them.
DELETE_BODY = {"remark": "No longer needed by the ground operations team"}


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture(autouse=True)
def _override_billing(app, db_session) -> None:
    from src.api.v1.conversations._shared import billing_session_factory

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session  # the fixture owns teardown (rollback); don't close here

    app.dependency_overrides[billing_session_factory] = lambda: lambda: _session()


@pytest.fixture(autouse=True)
def _override_storage(app, fake_storage) -> None:
    from src.api.v1.attachments.router import storage_dependency

    app.dependency_overrides[storage_dependency] = lambda: fake_storage


@pytest.fixture
def set_chat_model(app):
    """Inject a Pydantic AI model (a TestModel/FunctionModel) for description generation —
    the describe endpoint resolves the same `chat_model` dependency as the chat relay."""

    def _set(model) -> None:
        from src.api.v1.conversations._shared import chat_model

        app.dependency_overrides[chat_model] = lambda: model

    return _set
