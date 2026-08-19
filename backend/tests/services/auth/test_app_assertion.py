"""Generated-app identity assertion — mint + JWKS, fail-closed unit tests (issue #92,
R3-R6, R11). `verify_app_assertion` mirrors what a generated app does itself in
production (against the published JWKS, not this module) — these tests exercise that
exact contract from the control-plane side.
"""

from __future__ import annotations

import time
import uuid

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from src.services.auth.app_assertion import (
    get_jwks,
    mint_app_assertion,
    mint_launch_code,
    verify_app_assertion,
    verify_launch_code,
)


def _mint(app_id: uuid.UUID, plane: str = "preview", **overrides: object) -> str:
    kwargs: dict[str, object] = {
        "entra_oid": "entra-oid-1",
        "email": "citizen@rvaiglobal.com",
        "display_name": "A Citizen",
        "app_id": app_id,
        "plane": plane,
    }
    kwargs.update(overrides)
    return mint_app_assertion(**kwargs)  # type: ignore[arg-type]


def test_mint_verify_roundtrips_identity() -> None:
    app_id = uuid.uuid4()
    token = _mint(app_id)
    claims = verify_app_assertion(token, app_id=app_id, plane="preview")
    assert claims.entra_oid == "entra-oid-1"
    assert claims.email == "citizen@rvaiglobal.com"
    assert claims.display_name == "A Citizen"
    assert claims.app_id == app_id
    assert claims.plane == "preview"


def test_key_on_entra_object_id_not_email() -> None:
    # R5: `sub` (the verifiable identity claim) is the Entra object id, never email —
    # email rides only as a convenience claim.
    app_id = uuid.uuid4()
    token = _mint(app_id, entra_oid="stable-oid-xyz", email="old@rvaiglobal.com")
    claims = verify_app_assertion(token, app_id=app_id, plane="preview")
    assert claims.entra_oid == "stable-oid-xyz"


# --- R6: app + plane binding ----------------------------------------------------


def test_rejected_when_presented_to_a_different_app() -> None:
    app_id = uuid.uuid4()
    other_app_id = uuid.uuid4()
    token = _mint(app_id)
    with pytest.raises(ValueError):
        verify_app_assertion(token, app_id=other_app_id, plane="preview")


def test_rejected_when_presented_on_a_different_plane() -> None:
    app_id = uuid.uuid4()
    token = _mint(app_id, plane="preview")
    with pytest.raises(ValueError):
        verify_app_assertion(token, app_id=app_id, plane="deployed")


def test_preview_and_deployed_assertions_for_the_same_app_are_distinct() -> None:
    app_id = uuid.uuid4()
    preview_token = _mint(app_id, plane="preview")
    deployed_token = _mint(app_id, plane="deployed")
    assert preview_token != deployed_token
    assert verify_app_assertion(preview_token, app_id=app_id, plane="preview").plane == "preview"
    assert (
        verify_app_assertion(deployed_token, app_id=app_id, plane="deployed").plane == "deployed"
    )


# --- R16: hard lifetime, fail-closed on expiry/forgery ---------------------------


def test_expired_assertion_rejected() -> None:
    app_id = uuid.uuid4()
    now = int(time.time())
    from src.services.auth.app_assertion import _key_id, _signing_key  # noqa: PLC0415

    claims = {
        "sub": "entra-oid-1",
        "email": "citizen@rvaiglobal.com",
        "name": None,
        "aud": str(app_id),
        "plane": "preview",
        "iat": now - 7200,
        "exp": now - 3600,  # already expired
    }
    header = {"alg": "RS256", "kid": _key_id(), "typ": "JWT"}
    expired = jwt.encode(header, claims, _signing_key())
    with pytest.raises(ValueError):
        verify_app_assertion(expired, app_id=app_id, plane="preview")


def test_wrong_key_forgery_rejected() -> None:
    app_id = uuid.uuid4()
    now = int(time.time())
    forged_key = RSAKey.generate_key(2048)
    claims = {
        "sub": "entra-oid-1",
        "email": "citizen@rvaiglobal.com",
        "name": None,
        "aud": str(app_id),
        "plane": "preview",
        "iat": now,
        "exp": now + 300,
    }
    forged = jwt.encode({"alg": "RS256", "typ": "JWT"}, claims, forged_key)
    with pytest.raises(ValueError):
        verify_app_assertion(forged, app_id=app_id, plane="preview")


def test_tampered_payload_rejected() -> None:
    app_id = uuid.uuid4()
    token = _mint(app_id)
    header, payload, sig = token.split(".")
    tampered_payload = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    with pytest.raises(ValueError):
        verify_app_assertion(f"{header}.{tampered_payload}.{sig}", app_id=app_id, plane="preview")


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b", "a.b.c.d"])
def test_malformed_token_rejected(garbage: str) -> None:
    with pytest.raises(ValueError):
        verify_app_assertion(garbage, app_id=uuid.uuid4(), plane="preview")


# --- R3, R11: the published JWKS is what a generated app actually verifies with --


def test_jwks_shape_is_public_only() -> None:
    jwks = get_jwks()
    assert "keys" in jwks and len(jwks["keys"]) == 1
    key = jwks["keys"][0]
    assert key["kty"] == "RSA"
    assert key["use"] == "sig"
    assert key["alg"] == "RS256"
    assert "kid" in key and key["kid"]
    # Public numbers only — no private exponent/primes anywhere in the document.
    assert set(key) <= {"kty", "n", "e", "kid", "use", "alg"}


def test_a_minted_assertion_verifies_against_the_published_jwks_alone() -> None:
    # The exact contract a generated app relies on (R11): verify using ONLY the
    # public JWKS document, never anything only the control-plane holds.
    app_id = uuid.uuid4()
    token = _mint(app_id)
    jwks = get_jwks()
    public_key = RSAKey.import_key(jwks["keys"][0])

    decoded = jwt.decode(token, public_key, algorithms=["RS256"])
    assert decoded.claims["aud"] == str(app_id)
    assert decoded.claims["plane"] == "preview"
    assert decoded.claims["sub"] == "entra-oid-1"



# --- R10: the launch-exchange code — a distinct capability from the app assertion ----


def test_launch_code_roundtrips_and_carries_the_deep_link() -> None:
    app_id = uuid.uuid4()
    code = mint_launch_code(
        entra_oid="entra-oid-1",
        email="citizen@rvaiglobal.com",
        display_name="A Citizen",
        app_id=app_id,
        next_path="/records/42",
    )
    claims = verify_launch_code(code)
    assert claims.entra_oid == "entra-oid-1"
    assert claims.email == "citizen@rvaiglobal.com"
    assert claims.app_id == app_id
    assert claims.next_path == "/records/42"


def test_launch_code_cannot_be_used_as_an_app_assertion() -> None:
    # A launch code and an app assertion are signed with the SAME key but carry
    # DIFFERENT audiences — a code presented where an assertion is expected fails the
    # audience check, never partially validates.
    app_id = uuid.uuid4()
    code = mint_launch_code(
        entra_oid="entra-oid-1",
        email="citizen@rvaiglobal.com",
        display_name=None,
        app_id=app_id,
        next_path="/",
    )
    with pytest.raises(ValueError):
        verify_app_assertion(code, app_id=app_id, plane="deployed")


def test_an_app_assertion_cannot_be_used_as_a_launch_code() -> None:
    app_id = uuid.uuid4()
    assertion = _mint(app_id, plane="deployed")
    with pytest.raises(ValueError):
        verify_launch_code(assertion)


def test_launch_code_expired_rejected() -> None:
    app_id = uuid.uuid4()
    now = int(time.time())
    from src.services.auth.app_assertion import (  # noqa: PLC0415
        _LAUNCH_EXCHANGE_AUDIENCE,
        _key_id,
        _signing_key,
    )

    claims = {
        "sub": "entra-oid-1",
        "email": "citizen@rvaiglobal.com",
        "name": None,
        "aud": _LAUNCH_EXCHANGE_AUDIENCE,
        "app_id": str(app_id),
        "next": "/",
        "iat": now - 120,
        "exp": now - 60,
    }
    header = {"alg": "RS256", "kid": _key_id(), "typ": "JWT"}
    expired = jwt.encode(header, claims, _signing_key())
    with pytest.raises(ValueError):
        verify_launch_code(expired)


def test_jwks_kid_matches_the_token_header_kid() -> None:
    app_id = uuid.uuid4()
    token = _mint(app_id)
    header = jwt.decode(
        token, RSAKey.import_key(get_jwks()["keys"][0]), algorithms=["RS256"]
    ).header
    assert header["kid"] == get_jwks()["keys"][0]["kid"]
