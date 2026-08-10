"""Microsoft Entra ID + session configuration.

The backend runs the OIDC Authorization-Code + PKCE flow itself and mints its own
cookie session (ADR-0007). Every Entra + session parameter flows through one
`AUTH__*` env block validated into `AuthConfig`, mirroring
`services/storage/config.py`: `extra="forbid"` so a mistyped nested key fails at
startup instead of silently defaulting, `SecretStr` on every credential
(unwrapped only at the JOSE / OAuth boundary, per security.md), and required
fields carry NO default so a missing tenant/secret fails at `Settings()`
construction (fail-first-python.md).

The single tenant is hard-configured: `tenant_id` pins the tenant-specific
discovery document and thus the exact issuer, which is what makes the
outside-tenant rejection possible — never `common`/`organizations`, whose
templated issuer defeats an exact `iss` match (R17).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

# Entra's tenant-scoped OIDC authority. `{tenant_id}/v2.0` is the exact issuer
# Authlib validates the id_token `iss` against; the discovery doc under it lists
# the endpoints + JWKS.
_ENTRA_AUTHORITY = "https://login.microsoftonline.com"

# A shorter session/HMAC key than this is a real forgery risk, not a style nit:
# the one secret signs the session JWT and the CSRF double-submit token and seeds
# SessionMiddleware. 32 chars is the floor for an HS256 key.
_MIN_SESSION_SECRET_LEN = 32


class AuthConfig(BaseModel):
    """Entra ID + session config, populated from one `AUTH__*` env block."""

    # `extra="forbid"` makes a mistyped AUTH__* nested key fail at startup instead
    # of being silently ignored and falling back to a default (fail-first).
    model_config = ConfigDict(extra="forbid")

    # Required — no default. A missing one fails at Settings() construction.
    tenant_id: str
    client_id: str
    session_secret: SecretStr
    # Optional — unused while the Entra app registration is a PUBLIC client: the backend presents
    # no secret at the token endpoint (see build_oauth; AADSTS700025). Kept so a deployment that
    # still sets AUTH__CLIENT_SECRET boots unchanged. Restore to a required field when the app is
    # switched back to a confidential client (backlog: Entra OIDC confidential-client hardening).
    client_secret: SecretStr | None = None
    # The full external callback URL registered as the Entra reply URL. It is used
    # VERBATIM as the OAuth redirect_uri (never rebuilt via request.url_for) so it
    # byte-matches the registered reply URL through the /api-stripping edge — a
    # forwarded-header fix corrects scheme/host but not a stripped path prefix
    # (KD-8).
    redirect_uri: str
    # A1: the SECOND registered Entra reply URL, for the sandbox login broker
    # (`/auth/sandbox/callback`) — additive to `redirect_uri`, on the SAME Entra app
    # registration. Must share `redirect_uri`'s origin: `build_oauth`'s Origin-header
    # workaround (oidc.py) derives `spa_origin` from `redirect_uri` alone, so a
    # second path on that same host needs no change there.
    sandbox_redirect_uri: str

    # Optional knobs — a default only where the value has a defined meaning.
    access_ttl_seconds: int = 900  # session JWT lifetime (~15m)
    refresh_ttl_seconds: int = 604800  # per-refresh-token lifetime (~7d)
    absolute_session_seconds: int = 28800  # family hard cap (~8h) — AE3
    session_cookie_max_age: int = 600  # oauth_transient state cookie (~10m)
    # None -> derive Secure/prefix from Settings.is_production (the usual path);
    # an explicit bool overrides it (e.g. a non-prod https staging box).
    cookie_secure: bool | None = None

    @field_validator("session_secret")
    @classmethod
    def _session_secret_is_strong(cls, value: SecretStr) -> SecretStr:
        # STATIC message only — never echo the secret. pydantic reflects validator
        # messages into ValidationError (and thus logs).
        if len(value.get_secret_value()) < _MIN_SESSION_SECRET_LEN:
            raise ValueError(
                "AUTH__SESSION_SECRET must be at least 32 characters: it signs the "
                "session JWT and CSRF token and seeds SessionMiddleware."
            )
        return value

    @field_validator("redirect_uri")
    @classmethod
    def _redirect_uri_is_absolute(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(
                "AUTH__REDIRECT_URI must be an absolute http(s) URL matching the "
                "registered Entra reply URL (e.g. https://host/api/v1/auth/callback)."
            )
        return value

    @field_validator("sandbox_redirect_uri")
    @classmethod
    def _sandbox_redirect_uri_is_absolute(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(
                "AUTH__SANDBOX_REDIRECT_URI must be an absolute http(s) URL matching the "
                "SECOND registered Entra reply URL (e.g. https://host/api/v1/auth/sandbox/callback)."
            )
        return value

    @property
    def server_metadata_url(self) -> str:
        """Tenant-specific OIDC discovery document. Pins the concrete issuer for
        the exact `iss` match (R17); Authlib fetches endpoints + JWKS from here."""
        return f"{_ENTRA_AUTHORITY}/{self.tenant_id}/v2.0/.well-known/openid-configuration"
