"""Orchestrator-test scaffolding.

The autouse `_no_live_model` guard forbids any live model request for the whole package
(mirrors `tests/services/agent/conftest.py`): the `FunctionModel` never hits the network, but an
accidental real call fails loudly instead of billing Foundry. U4 extends this with the fake
sandbox, the collecting progress sink, and the billing / run-context test doubles.
"""

from __future__ import annotations

import pytest
from pydantic_ai import models


@pytest.fixture(autouse=True)
def _no_live_model():
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = False
    yield
    models.ALLOW_MODEL_REQUESTS = previous
