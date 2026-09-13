"""The one check that talks to a real lake — opt-in, and it SKIPS rather than fails without one.

WHY IT EXISTS WHEN EVERYTHING ELSE IS DETERMINISTIC. The deterministic suite proves the platform
handles the shapes the lake produces. It cannot prove the lake still produces them. Four of the
traps this feature is written around are facts about somebody else's data pipeline — the month
folder, the directory placeholders, the zero-byte stubs, the sparse calendar — and a trap that
quietly stops existing upstream leaves a test passing forever while protecting nothing. This is
the only thing that can notice.

WHY IT IS OPT-IN AND WILL NEVER RUN IN CI. `pytest` is deliberately not in this project's CI at
all (the suite needs a database built to a runbook), and the substrate behind this check is a
replica lake in a free-trial subscription that goes read-only when the credit runs out. So it is
marked `integration`, which `addopts = "-m 'not integration and not destructive_migration'"`
already deselects, and it skips CLEANLY when the lake is unconfigured — the e2e conftest's rule,
so the lane degrades to a visible skip rather than a hang or a false pass.

WHICH CREDENTIAL IT USES, AND WHY THAT IS NOT A HOLE. `LakeClient` builds
`ManagedIdentityCredential(client_id=...)` and nothing can change that — the whole point of the
module is that the identity is named explicitly, so a swappable credential would be the first
thing to erode. A managed identity only exists INSIDE Azure, so a test that used the client's own
credential could never run on a developer machine and this file would skip forever, which is the
same as not existing.

So this check injects a `BlobServiceClient` built from the developer's `az login` into the
client's own cache — the same seam `test_client.py` uses for its fakes — and then drives the REAL
`list_files` / `download` / `_raise_absent` code paths against the REAL lake. Everything is
genuine except which principal is asking.

**It therefore does NOT prove the managed-identity path.** That is proven separately and from
inside Azure by the `dice-mi-probe` Container Apps job (`az containerapp job start -n
dice-mi-probe -g bial-cd-rg`), which exists for exactly this reason. Do not read a green run here
as evidence that the role assignment or the token mint works — it is evidence that the listing,
the object-name pattern, the trap shapes and the byte handling are right.

HOW TO RUN IT. `az login` against the subscription holding the lake, point `CONNECTOR_LAKE__URL`
at it, then:

    cd backend && CONNECTOR_LAKE__URL=https://<account>.blob.core.windows.net/<container>/<pre>/ \
      uv run pytest tests/integration/test_lake_live.py -m integration -v -s

Its output belongs in the PR body, pasted rather than linked, because nothing else will ever run
it.

WHAT IT DELIBERATELY DOES NOT DO. It does not parse parquet. The control plane copies bytes
verbatim and has no parquet dependency, and adding one to test something the backend never reads
is exactly the drift this split exists to prevent — the row-level traps (the 11.1% amendment
overcount, the null departure time, the padded airline name) belong to the worked example in the
golden template and are proven against the replica out of band.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential
from azure.storage.blob.aio import BlobServiceClient

from src.core.connectors import CONNECTORS
from src.services.lake import client as lake_client
from src.services.lake.client import LakeClient, reset_lake_for_tests
from src.services.lake.config import LakeConfig
from src.services.lake.window import LAKE_FILE_PATTERN, select_files
from src.services.usage import ist_today

pytestmark = pytest.mark.integration

_DICE = CONNECTORS["dice"]
#: The four bytes every parquet file starts and ends with. Checked instead of parsing: it is the
#: whole of what the control plane is entitled to know about the file.
_PARQUET_MAGIC = b"PAR1"


# FUNCTION-SCOPED, not module-scoped, and that is forced rather than chosen: `asyncio_mode = auto`
# gives each test its own event loop, so a module-scoped async fixture builds its aiohttp session
# on one loop and hands it to tests running on another — which fails as
# `got Future attached to a different loop` at the first DOWNLOAD, after the listing tests have
# already passed. The cost is one extra listing per test against a container holding a few dozen
# small objects.
@pytest.fixture
async def lake() -> AsyncIterator[LakeClient]:
    """A `LakeClient` pointed at a real lake and reading with the developer's own `az` login.

    Reads the environment DIRECTLY rather than through `settings`, so this check can be pointed at
    a replica without the whole process being configured for one — and so the skip message names
    the variable to set.

    THE CACHE INJECTION IS THE POINT, NOT A WORKAROUND. See the module docblock: the client's
    credential is deliberately unswappable, so the only way to exercise its real code against real
    bytes from outside Azure is to hand its cache a service client built from a credential that
    works here. The client's own `list_files` / `download` / error translation all run unchanged.
    """
    url = os.environ.get("CONNECTOR_LAKE__URL")
    if not url:
        pytest.skip(
            "no lake configured — set CONNECTOR_LAKE__URL to a real lake "
            "(https://<account>.blob.core.windows.net/<container>/<prefix>/) to run this"
        )

    config = LakeConfig(
        url=url,
        # Neither identifier is used on this path. The client id names the identity a CONTAINER
        # would present, and the resource id attaches that identity to a container app; this test
        # is neither. They are filled so the model constructs, and nothing reads them.
        identity_client_id=os.environ.get(
            "CONNECTOR_LAKE__IDENTITY_CLIENT_ID", "00000000-0000-0000-0000-000000000000"
        ),
        identity_resource_id=os.environ.get(
            "CONNECTOR_LAKE__IDENTITY_RESOURCE_ID", "/not-used-by-this-test"
        ),
    )

    credential = AzureCliCredential()
    service_client = BlobServiceClient(config.account_url, credential=credential)
    await reset_lake_for_tests()
    lake_client._client_cache[lake_client._fingerprint(config)] = lake_client._LakeClientState(
        service_client, credential=cast("ManagedIdentityCredential", credential)
    )
    try:
        yield LakeClient(config)
    finally:
        await service_client.close()
        await credential.close()
        lake_client._client_cache.clear()


@pytest.fixture
async def listing(lake: LakeClient) -> tuple:
    entries = await lake.list_files()
    assert entries, "the lake answered with nothing at all — check the container and the prefix"
    return entries


async def test_the_listing_still_carries_directory_placeholders(listing) -> None:
    """★ The trap the ordering rule exists for. If this ever stops being true, the flat-listing
    behaviour of the account has changed and `select_files`'s name-before-size rule has lost its
    reason — which is worth knowing, and worth writing down rather than discovering."""
    zero_length = [entry for entry in listing if entry.size == 0]
    directories = [entry for entry in zero_length if LAKE_FILE_PATTERN.search(entry.name) is None]

    assert directories, (
        "no zero-length non-file entries came back. Either this is no longer a "
        "hierarchical-namespace account, or the prefix is wrong."
    )


async def test_at_least_one_file_is_filed_under_the_wrong_month(listing) -> None:
    """★ THE MONTH FOLDER IS THE LOAD MONTH. Measured on the replica: the file dated the 31st of
    a month sits under the NEXT month's folder, because the load ran the following morning. This
    asserts the shape rather than one date, so it survives the replica being regenerated."""
    months = {
        1: "JANUARY",
        2: "FEBRUARY",
        3: "MARCH",
        4: "APRIL",
        5: "MAY",
        6: "JUNE",
        7: "JULY",
        8: "AUGUST",
        9: "SEPTEMBER",
        10: "OCTOBER",
        11: "NOVEMBER",
        12: "DECEMBER",
    }
    mismatched = []
    for entry in listing:
        match = LAKE_FILE_PATTERN.search(entry.name)
        if match is None:
            continue
        month = int(match.group("day")[4:6])
        if f"/{months[month]}/" not in entry.name.upper():
            mismatched.append(entry.name)

    assert mismatched, (
        "every file sits under the folder matching its own date. The load-date/flight-date "
        "split may have changed upstream — re-read the profile before trusting the resolver."
    )


async def test_a_window_selects_a_plausible_set(listing) -> None:
    """The whole resolver, over a real listing. Asserts a SHAPE, not a count: the replica is
    regenerated and the real lake gains a file every morning, so a fixed number would rot."""
    latest = ist_today() - timedelta(days=_DICE.freshness_lag_days)
    window_start = latest - timedelta(days=_DICE.max_window_days - 1)
    from src.core.connectors import ResolvedWindow
    from src.db.models.project_connector import ConnectorWindowKind

    selection = select_files(
        listing,
        ResolvedWindow(
            effectively_on=True,
            kind=ConnectorWindowKind.RELATIVE,
            start=window_start,
            end=latest,
            days=_DICE.max_window_days,
            clamped=False,
            earliest=window_start,
            latest=latest,
        ),
        max_files=_DICE.max_window_days,
    )

    assert len(selection.files) <= _DICE.max_window_days
    assert all(window_start <= item.day <= latest for item in selection.files)
    assert all(item.size > 0 for item in selection.files)
    print(  # noqa: T201 — this output is what gets pasted into the PR body
        f"\nlisting={len(listing)} selected={len(selection.files)} "
        f"unreadable_days={selection.skipped} bytes={selection.total_bytes} "
        f"window={window_start}..{latest} read_at={datetime.now(UTC).isoformat()}"
    )


async def test_one_file_downloads_and_looks_like_parquet(lake: LakeClient, listing) -> None:
    """★ THE BYTES ARE REAL AND THEY ARE NOT DECODED. Checked by its magic number at both ends —
    the control plane copies verbatim and has no parquet dependency, and adding one to test
    something the backend never reads is exactly the drift this split exists to prevent."""
    candidates = [
        entry
        for entry in listing
        if entry.size > 0 and LAKE_FILE_PATTERN.search(entry.name) is not None
    ]
    assert candidates, "no readable file in the listing at all"

    payload = await lake.download(candidates[-1].name)

    assert isinstance(payload, bytes)
    assert payload.startswith(_PARQUET_MAGIC)
    assert payload.endswith(_PARQUET_MAGIC), "a truncated file would pass a start-only check"
    assert len(payload) == candidates[-1].size, "the listing's size and the bytes must agree"
