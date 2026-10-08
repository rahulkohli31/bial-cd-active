"""Where BIAL Chat's file analysis runs: one Azure dynamic-sessions pool.

`Settings.analysis` is typed `AnalysisConfig | None`; pydantic-settings validates one
`ANALYSIS__*` env block against it. Unset means the analysis runtime is off, which is a supported
posture in every environment: BIAL Chat then refuses Office and CSV files at its doors and answers
everything else as before.

The endpoint is a label, not a credential. Calls carry a token for the backend's own identity, and
that identity's role on the pool is the whole of the access control.
"""

from __future__ import annotations

from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, model_validator

# Static: pydantic echoes validator messages into the startup `ValidationError`.
_ENDPOINT_SHAPE = (
    "ANALYSIS__POOL_ENDPOINT must be the session pool's https:// management endpoint, as "
    "`az containerapp sessionpool show --query properties.poolManagementEndpoint` prints it"
)


class AnalysisConfig(BaseModel):
    """The session pool's management endpoint. The idle cool-down is a pool setting, not ours."""

    model_config = ConfigDict(extra="forbid")

    pool_endpoint: str

    @model_validator(mode="after")
    def _the_endpoint_must_be_https(self) -> Self:
        parts = urlsplit(self.pool_endpoint)
        if parts.scheme != "https" or not parts.hostname or not parts.path.strip("/"):
            raise ValueError(_ENDPOINT_SHAPE)
        return self
