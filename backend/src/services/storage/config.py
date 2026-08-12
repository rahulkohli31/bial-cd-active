"""Azure Blob Storage configuration model.

The control-plane persists attachments and generated-app files to Azure Blob
Storage only. `Settings.object_store` is typed `StorageConfig | None`, where
`StorageConfig` is a plain alias for `AzureStorageConfig`; pydantic-settings
validates one `OBJECT_STORE__*` env block against it. This single funnel is the
ONLY place dynamic input becomes a typed config member — there is no hand-written
`TypeAdapter` on the env path.

Every credential is a `SecretStr`, unwrapped only at the SDK boundary in the
backend (per security.md). Knob fields keep a default only where the empty/None
value has a DEFINED meaning the code branches on.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator


class AzureStorageConfig(BaseModel):
    """Azure Blob Storage. Exactly one auth mode is enforced by the validator:
    `connection_string` (must embed an AccountKey), `account_key`, or
    `use_managed_identity`."""

    # `extra="forbid"` makes a mistyped OBJECT_STORE__* nested key fail at startup
    # instead of being silently ignored and falling back to a default (fail-first).
    model_config = ConfigDict(extra="forbid")

    provider: Literal["azure"] = "azure"
    account_url: str
    container: str
    connection_string: SecretStr | None = None
    account_key: SecretStr | None = None
    use_managed_identity: bool = False

    @model_validator(mode="after")
    def _exactly_one_auth_mode(self) -> Self:
        modes = (
            self.connection_string is not None,
            self.account_key is not None,
            self.use_managed_identity,
        )
        if sum(modes) != 1:
            # STATIC message only — never interpolate any field value. pydantic
            # echoes validator messages into ValidationError (and thus logs).
            raise ValueError(
                "Azure storage requires exactly one auth mode: set exactly one of "
                "connection_string, account_key, or use_managed_identity."
            )
        if self.connection_string is not None:
            # A service SAS (for signed reads) needs a shared account key. A
            # SAS-based connection string has SharedAccessSignature= instead and
            # cannot mint one — fail fast. Scan for the key WITHOUT interpolating
            # any parsed substring (account key / SAS token) into the message.
            has_account_key = any(
                part.startswith("AccountKey=")
                for part in self.connection_string.get_secret_value().split(";")
            )
            if not has_account_key:
                raise ValueError(
                    "Azure connection_string must contain an AccountKey: a "
                    "SAS-based connection string cannot mint a service SAS for "
                    "signed reads."
                )
        return self


# Plain alias — `Settings.object_store` is typed `StorageConfig | None`. Azure
# Blob is the only provider, so there is no discriminated union to funnel through;
# the alias keeps `src.config`'s import stable if a second provider ever returns.
StorageConfig = AzureStorageConfig
