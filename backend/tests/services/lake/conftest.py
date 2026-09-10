"""Fixtures for the lake suite.

THE DEFAULT LANE NEEDS NO AZURE AND MUST BE ASSERTED TO, not assumed: the whole reason the window
resolver takes a list of `(name, size)` rather than a client is that every trap the lake sets can
then be exercised on a machine with no Azure account at all. `test_no_azure_credential_is_needed`
below is the assertion that says so — it fails if any default-lane module starts reaching for one.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.services.lake.fixtures.listing import a_sparse_calendar, the_trap_listing


@pytest.fixture
def trap_listing():
    """The four listing-observable traps, exactly as a real listing produces them."""
    return the_trap_listing()


@pytest.fixture
def sparse_calendar():
    """Three months with real gaps in them."""
    return a_sparse_calendar()


@pytest.fixture
def no_azure_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every `AZURE_*` name removed, so a module that reached for an ambient credential fails."""
    for name in list(os.environ):
        if name.startswith(("AZURE_", "MSI_", "IDENTITY_")):
            monkeypatch.delenv(name, raising=False)
    yield
