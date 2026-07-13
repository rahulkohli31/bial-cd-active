"""`attach_entra_token` wires managed-identity auth onto an async engine (ADR-0027).

No live Azure or Postgres: azure-identity's credential is faked and the registered
`do_connect` listener is invoked directly, asserting it injects a fresh Entra token
as the password and a verify-full TLS context on every connection.
"""

from __future__ import annotations

import ssl
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from src.db.base import _OSSRDBMS_SCOPE, attach_entra_token


class _FakeToken:
    token = "fake-entra-token-xyz"


class _FakeCredential:
    """Stand-in for azure.identity.DefaultAzureCredential — records how it was built
    and which scope was requested, without any network call."""

    init_kwargs: dict[str, Any] = {}
    last_scopes: tuple[str, ...] = ()

    def __init__(self, **kwargs: Any) -> None:
        type(self).init_kwargs = kwargs

    def get_token(self, *scopes: str, **_kw: Any) -> _FakeToken:
        type(self).last_scopes = scopes
        return _FakeToken()


def test_attach_entra_token_injects_token_and_verify_full_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # attach_entra_token imports azure.identity lazily, so patch the class there.
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _FakeCredential)

    engine = create_async_engine("postgresql+asyncpg://mi-name@host/citizen_one")
    attach_entra_token(engine)

    # One credential, built once, targeting the managed identity from settings
    # (DB_ENTRA_CLIENT_ID is unset in .env.test -> None == system-assigned).
    assert _FakeCredential.init_kwargs == {"managed_identity_client_id": None}

    # SQLAlchemy stores do_connect listeners on the dialect dispatch, which is
    # dynamically typed — reach it through an Any-typed local so ty/mypy/pyright stay
    # happy without a per-checker suppression.
    dispatch: Any = engine.sync_engine.dialect.dispatch
    listeners = list(dispatch.do_connect)
    assert listeners, "attach_entra_token must register a do_connect listener"

    cparams: dict[str, Any] = {}
    for fn in listeners:
        fn(engine.sync_engine.dialect, None, [], cparams)

    # The fresh Entra token becomes asyncpg's password, fetched with the verified
    # ossrdbms scope; TLS is verify-full (server cert + hostname checked).
    assert cparams["password"] == "fake-entra-token-xyz"
    assert _FakeCredential.last_scopes == (_OSSRDBMS_SCOPE,)
    ssl_ctx = cparams["ssl"]
    assert isinstance(ssl_ctx, ssl.SSLContext)
    assert ssl_ctx.check_hostname is True
    assert ssl_ctx.verify_mode == ssl.CERT_REQUIRED
