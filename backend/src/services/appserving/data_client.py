"""Loader for the injected BIALData client script (ported from Express
`bial-data-client.js` → `assets/bial_data_client.js`).

The self-contained client the generated app code calls (records/files/parse + the
injected user). Read once from the packaged asset and cached — it ships under
`src/` so it is present in the built image."""

from __future__ import annotations

from functools import cache
from pathlib import Path

_ASSET = Path(__file__).parent / "assets" / "bial_data_client.js"


@cache
def bial_data_client_script() -> str:
    """The inlinable BIALData client JS (cached). Embedded verbatim in the runner
    frame and the builder preview, both of which run in an opaque-origin sandbox."""
    return _ASSET.read_text(encoding="utf-8")
