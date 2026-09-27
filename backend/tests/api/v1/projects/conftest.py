"""Project-test fixtures: swap the object store for an in-memory fake (cascade-delete blob
sweep) and bind the billing drain to the test session."""

from __future__ import annotations

import contextlib

import pytest

from tests.fakes import FakeStorage

# THE DELETE BODY, in one place. `DELETE /v1/projects/{id}` requires a signed reason, and
# roughly thirty tests in this directory delete a project as a SETUP step — teardown,
# cascade and ownership cases that care about what the delete destroys, not about what the
# body must contain. Inlining the JSON at each of them meant the last required field was a
# thirty-site sweep, and the next one would be too.
#
# The validation cases deliberately do NOT use this: `test_delete_remark.py` builds its own
# bodies, because a constant that always satisfies the rules cannot test them.
DELETE_BODY = {"remark": "No longer needed by the ground operations team"}

# A DESCRIPTION THAT CLEARS THE BAR, in one place, for the same reason DELETE_BODY is: #191
# made description required and word-bounded, and most tests in this directory that create a
# project via the live endpoint don't care what the description says — they care about
# something else and just need a valid one to get past create. The issue's own worked example
# (#191 R16) doubles as this constant, so it is also exercised as ordinary product copy rather
# than test-only text. 26 words — comfortably inside the bound.
#
# The validation cases deliberately do NOT use this: `test_project_description_words.py` builds
# its own bodies, because a constant that always satisfies the rules cannot test them.
_VALID_DESCRIPTION = (
    "Ground staff log VIP movement requests for each terminal. A duty supervisor approves or "
    "rejects them, and the day's approved movements appear on a shared dashboard."
)


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
