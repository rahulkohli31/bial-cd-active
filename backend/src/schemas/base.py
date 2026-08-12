"""The single camelCase base model for every request/response schema across the
API layer.

Replaces the per-router `_CamelModel` copies (admin, apps/router)
and the two ad-hoc camelCase spellings (auth's inline `ConfigDict` on
`ProfileLimits`, usage's per-field `serialization_alias`) with one config, so no
domain declares its own base.

`alias_generator=to_camel` emits snake_case fields as camelCase on the wire
(`resets_at` -> `resetsAt`); `populate_by_name=True` keeps the snake_case field
name valid on input too, so internal construction (`Model(resets_at=...)`) and
alias-keyed input both work. FastAPI serializes `response_model` bodies with
`by_alias=True` by default, so a `CamelModel` route response emits camelCase keys.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base for all API schemas: snake_case in Python, camelCase on the wire."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
