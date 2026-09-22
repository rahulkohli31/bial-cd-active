"""Fixtures for the kind that resolves no container.

★ THIS DIRECTORY EXISTS FOR ONE REASON: `tests/api/v1/conversations/conftest.py` binds a sandbox
client on BOTH seams for every test under it, autouse. That is right for the two kinds whose every
turn reads a project's live app — and it makes "a generic turn needs no sandbox" untestable by
CONSTRUCTION, because the refusal under test could never fire. A test that inherited it would be
green in exactly the state where the guard had been deleted.

So the generic turn's tests live beside that suite rather than inside it, bind nothing, and take
the turn-driving seams by importing them. Nothing here binds a workspace, and nothing here should
ever start to.
"""

from __future__ import annotations

from tests.api.v1.conversations.conftest import (
    _fresh_engine as _fresh_engine,
)
from tests.api.v1.conversations.conftest import (
    _override_billing as _override_billing,
)
from tests.api.v1.conversations.conftest import (
    fake_storage as fake_storage,
)
from tests.api.v1.conversations.conftest import (
    set_chat_model as set_chat_model,
)
