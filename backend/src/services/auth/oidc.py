"""Entra ID OIDC client + fail-closed identity validator.

`build_oauth()` registers `entra` against the TENANT-SPECIFIC discovery document
(`{tenant_id}/v2.0`, never `common`/`organizations` — a templated issuer defeats the exact
`iss` match), PKCE (`S256`), `openid profile email`. `get_oauth()` is the seam tests override.

The app registration is a single-page-application client, so the user's own browser redeems the
code: `pending_redemption` hands the callback page what it needs, and `identity_from_browser`
takes back the ID token it got. Redeeming from the browser keeps the token request on the same
network as the sign-in, which is where Conditional Access evaluates it.

`validate_entra_token` is the fail-closed gate: `userinfo` exists only AFTER the signature,
`iss`, `aud`, `exp` and `nonce` are validated, so its presence IS the proof. We hard-assert
`oid`/`sub`/`tid == tenant_id` and a non-null email (`preferred_username` fallback) — else
`AuthError`: no session, no user row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from authlib.integrations.base_client import MismatchingStateError
from authlib.integrations.starlette_client import OAuth, OAuthError
from joserfc.errors import JoseError
from starlette.requests import Request

from src.config import settings
from src.services.auth.errors import (
    REASON_INVALID_CALLBACK,
    REASON_WRONG_TENANT,
    AuthError,
)

_SCOPES = "openid profile email"


@dataclass(frozen=True, slots=True)
class EntraIdentity:
    """The validated identity extracted from the Entra token — a typed object
    parsed at the boundary, never a raw claims dict passed inward."""

    oid: str
    email: str
    upn: str | None
    display_name: str | None


def build_oauth() -> OAuth:
    """Register the `entra` provider (tenant discovery + PKCE, single-page-application client)."""
    oauth = OAuth()
    # No token-endpoint settings: this client never calls the token endpoint. The browser redeems
    # the code with the PKCE verifier and no secret, which is all a single-page-application
    # registration accepts.
    oauth.register(
        name="entra",
        server_metadata_url=settings.auth.server_metadata_url,
        client_id=settings.auth.client_id,
        client_kwargs={"scope": _SCOPES, "code_challenge_method": "S256"},
    )
    return oauth


@dataclass(frozen=True, slots=True)
class PendingRedemption:
    """What the callback page needs to redeem the code from the user's own browser."""

    token_endpoint: str
    client_id: str
    code: str
    redirect_uri: str
    code_verifier: str
    scope: str
    state: str


async def pending_redemption(oauth: OAuth, request: Request) -> PendingRedemption:
    """The redemption the callback's `code` and `state` describe, checked against this browser's
    own sign-in state.

    Raises `OAuthError` when Entra sent an error instead of a code, and `MismatchingStateError`
    when the state is not one this browser started. The state stays in the session: `complete`
    consumes it."""
    error = request.query_params.get("error")
    if error:
        raise OAuthError(error=error, description=request.query_params.get("error_description"))
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    started = await oauth.entra.framework.get_state_data(request.session, state)
    if not started:
        raise MismatchingStateError()
    if not code:
        raise OAuthError(error="invalid_request", description="the callback carried no code")
    metadata = await oauth.entra.load_server_metadata()
    return PendingRedemption(
        token_endpoint=str(metadata["token_endpoint"]),
        client_id=settings.auth.client_id,
        code=code,
        redirect_uri=str(started["redirect_uri"]),
        code_verifier=str(started["code_verifier"]),
        scope=_SCOPES,
        state=state,
    )


async def identity_from_browser(
    oauth: OAuth,
    request: Request,
    *,
    state: str,
    id_token: str,
    error: str,
    error_description: str,
) -> EntraIdentity:
    """The identity in the ID token the browser redeemed, or the error it was given instead.

    The state is consumed first, whatever follows, so a posted form is good for one attempt only.
    It must be one this browser started (the SameSite=Lax transient cookie carries it), and the
    token's nonce must be the one stored with it: together they bind the token to the sign-in
    this browser began, which is what stops a token being planted in someone else's session."""
    started = await oauth.entra.framework.get_state_data(request.session, state)
    await oauth.entra.framework.clear_state_data(request.session, state)
    if not started:
        raise MismatchingStateError()
    if error:
        raise OAuthError(error=error, description=error_description)
    nonce = started.get("nonce")
    if not id_token or not nonce:
        raise AuthError("the browser returned no ID token", reason=REASON_INVALID_CALLBACK)

    metadata = await oauth.entra.load_server_metadata()
    # The token arrived through the browser, so the audience is pinned exactly. Authlib on its own
    # accepts a foreign `aud` whenever `azp` names this client.
    claims_options = {
        "iss": {"essential": True, "value": metadata["issuer"]},
        "aud": {"essential": True, "value": settings.auth.client_id},
    }
    try:
        userinfo = await oauth.entra.parse_id_token(
            {"id_token": id_token}, nonce=nonce, claims_options=claims_options
        )
    except JoseError as exc:
        raise AuthError(
            f"the ID token did not validate: {type(exc).__name__}",
            reason=REASON_INVALID_CALLBACK,
        ) from exc
    return validate_entra_token({"userinfo": userinfo})


@lru_cache(maxsize=1)
def get_oauth() -> OAuth:
    """Process-wide OAuth registry — the seam the endpoints depend on. Tests
    override this dependency (or pre-seed the returned registry's
    `.entra.server_metadata`) to run the flow without a live tenant."""
    return build_oauth()


def validate_entra_token(token: Mapping[str, Any]) -> EntraIdentity:
    """Fail-closed identity extraction from a validated token response.

    Raises `AuthError` on missing `userinfo` (unvalidated token), missing
    `oid`/`sub`, a foreign `tid`, or a missing email+UPN. Never returns on doubt."""
    userinfo = token.get("userinfo")
    if not isinstance(userinfo, Mapping) or not userinfo:
        raise AuthError(
            "callback token carries no validated userinfo", reason=REASON_INVALID_CALLBACK
        )

    oid = userinfo.get("oid")
    sub = userinfo.get("sub")
    if not oid or not sub:
        raise AuthError("callback identity missing oid/sub", reason=REASON_INVALID_CALLBACK)

    # Hard tenant boundary — a foreign tenant or personal account is rejected
    # fail-closed.
    if userinfo.get("tid") != settings.auth.tenant_id:
        raise AuthError("callback tenant does not match", reason=REASON_WRONG_TENANT)

    # Entra's `email` claim is optional even with the `email` scope;
    # `preferred_username` (the UPN) is reliably present for work accounts. A
    # missing email is a DEFINED fail-closed outcome, never a NOT NULL crash on
    # upsert.
    email = userinfo.get("email") or userinfo.get("preferred_username")
    if not email:
        raise AuthError(
            "callback identity has neither email nor UPN", reason=REASON_INVALID_CALLBACK
        )

    # Capture the UPN unconditionally (separate from email) as the deterministic
    # join key for the deferred POC->Postgres migration.
    upn = userinfo.get("preferred_username")
    display_name = userinfo.get("name")
    return EntraIdentity(
        oid=str(oid),
        email=str(email),
        upn=str(upn) if upn else None,
        display_name=str(display_name) if display_name else None,
    )
