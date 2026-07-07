"""Agent-test guard: forbid any live model request for the whole package. TestModel /
FunctionModel never hit the network, but this makes an accidental real call fail loudly (CI)."""

from __future__ import annotations

import pytest
from pydantic_ai import models


@pytest.fixture(autouse=True)
def _no_live_model():
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = False
    yield
    models.ALLOW_MODEL_REQUESTS = previous
