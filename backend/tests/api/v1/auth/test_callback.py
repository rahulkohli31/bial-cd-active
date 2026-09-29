"""GET /auth/callback and POST /auth/complete — the browser redeems, the backend validates.

The callback serves a page that redeems the code from the user's own browser; `complete` takes the
ID token that page posts back, validates it fail-closed, provisions the user and mints the session.
Entra is `tests/entra.py`: the real Authlib registry against a seeded discovery document, with ID
tokens signed by a key only the tests hold.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from authlib.integrations.starlette_client import OAuthError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from src.config import settings
from src.db.models.refresh_token import RefreshToken
from src.db.models.user import User
from src.db.session import get_db
from src.services.auth.cookies import csrf_cookie_name, refresh_cookie_name, session_cookie_name
from src.services.auth.oidc import get_oauth
from src.services.auth.refresh import hash_refresh_token
from tests.entra import (
    ABSENT,
    FOREIGN_KEY,
    TOKEN_ENDPOINT,
    authorize_params,
    complete,
    id_token,
    sign_in,
    start_sign_in,
)
from tests.factories import UserFactory


def _set_cookies(resp: httpx.Response) -> dict[str, str]:
    return {raw.split("=", 1)[0].strip(): raw for raw in resp.headers.get_list("set-cookie")}


def _cookie_value(raw: str) -> str:
    return raw.split("=", 1)[1].split(";", 1)[0]


def _app_session_cookies(resp: httpx.Response) -> set[str]:
    return {session_cookie_name(), refresh_cookie_name(), csrf_cookie_name()} & set(
        _set_cookies(resp)
    )


def _assert_login_error(resp: httpx.Response, reason: str) -> str:
    """A failed sign-in bounces to `/login?authError=<reason>&ref=<correlation id>`.

    The ref is freshly random per request, so it is asserted for SHAPE, not value, and
    returned so a caller can match it against the log line the same request emitted."""
    assert resp.status_code == 302
    base, sep, ref = resp.headers["location"].partition("&ref=")
    assert base == f"{settings.FRONTEND_URL}/login?authError={reason}"
    assert sep, "every login-error bounce must carry a ?ref= correlation id"
    assert re.fullmatch(r"[0-9a-f]{8}", ref), f"unexpected correlation id: {ref!r}"
    return ref


def _assert_step_up(resp: httpx.Response) -> dict[str, str]:
    """A step-up is a 302 straight back to Entra with `prompt=login`; returns what it sent."""
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://login.microsoftonline.com/")
    sent = authorize_params(resp.headers["location"])
    assert sent["prompt"] == "login"
    assert sent["redirect_uri"] == settings.auth.redirect_uri
    return sent


def _aadsts(code: str) -> dict[str, str]:
    """The error Entra's token endpoint gives the browser, as the page posts it on."""
    return {
        "error": "invalid_grant",
        "error_description": (
            f"AADSTS{code}: Presented multi-factor authentication has expired due to policies "
            "configured by your administrator. Trace ID: 18659dd0 Correlation ID: fc25d9f7"
        ),
    }


def _redemption(page: httpx.Response) -> dict[str, Any]:
    block = re.search(
        r'<script id="redemption" type="application/json">(.*?)</script>', page.text, re.S
    )
    assert block is not None
    decoded: dict[str, Any] = json.loads(block.group(1))
    return decoded


# --- the callback page --------------------------------------------------------------------------


async def test_the_callback_serves_a_page_that_redeems_the_code_from_the_browser(client) -> None:
    """★ The fix for AADSTS50076 on the office network: the token request leaves the user's own
    browser, so Conditional Access sees the same network as the sign-in. The page carries what the
    browser needs and nothing is redeemed server-side."""
    state, _ = await start_sign_in(client)
    page = await client.get("/v1/auth/callback", params={"code": "entra-code", "state": state})

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    redemption = _redemption(page)
    assert redemption["token_endpoint"] == TOKEN_ENDPOINT
    assert redemption["client_id"] == settings.auth.client_id
    assert redemption["code"] == "entra-code"
    assert redemption["redirect_uri"] == settings.auth.redirect_uri
    assert redemption["scope"] == "openid profile email"
    assert redemption["state"] == state
    assert len(redemption["code_verifier"]) >= 43  # RFC 7636's floor for a PKCE verifier
    assert '<form id="complete" method="post" action="complete">' in page.text
    assert _app_session_cookies(page) == set()


async def test_the_page_runs_only_its_own_script_and_talks_only_to_the_token_endpoint(
    client,
) -> None:
    state, _ = await start_sign_in(client)
    page = await client.get("/v1/auth/callback", params={"code": "entra-code", "state": state})

    policy = page.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([^']+)'", policy)
    assert nonce is not None
    assert f'<script nonce="{nonce.group(1)}">' in page.text
    assert "default-src 'none'" in policy
    assert "connect-src https://login.microsoftonline.com;" in policy
    assert "frame-ancestors 'none'" in policy
    # The page carries the PKCE verifier: never cached. Its referrer policy must let the form post
    # carry the page's real Origin — `no-referrer` makes a browser send `Origin: null`, which the
    # cross-origin write guard refuses — while the code leaves the address bar before any request.
    assert page.headers["cache-control"] == "no-store"
    assert '<meta name="referrer" content="same-origin">' in page.text
    assert 'history.replaceState(null, "", location.pathname);' in page.text


async def test_a_code_cannot_close_the_data_block(client) -> None:
    state, _ = await start_sign_in(client)
    hostile = "</script><script>alert(1)</script>"
    page = await client.get("/v1/auth/callback", params={"code": hostile, "state": state})

    assert hostile not in page.text
    assert _redemption(page)["code"] == hostile


async def test_a_state_this_browser_never_started_gets_no_page(client) -> None:
    await start_sign_in(client)
    with capture_logs() as logs:
        resp = await client.get(
            "/v1/auth/callback", params={"code": "entra-code", "state": "somebody-elses"}
        )

    ref = _assert_login_error(resp, "auth_failed")
    failed = [entry for entry in logs if entry["event"] == "auth_callback_failed"]
    assert len(failed) == 1
    assert "mismatching_state" in failed[0]["detail"]
    assert failed[0]["trace_id"] == ref


async def test_a_callback_without_a_code_gets_no_page(client) -> None:
    state, _ = await start_sign_in(client)
    resp = await client.get("/v1/auth/callback", params={"state": state})
    _assert_login_error(resp, "auth_failed")


async def test_cancelled_consent_does_not_500(client) -> None:
    state, _ = await start_sign_in(client)
    resp = await client.get("/v1/auth/callback", params={"error": "access_denied", "state": state})
    _assert_login_error(resp, "auth_failed")
    assert _app_session_cookies(resp) == set()


async def test_a_conditional_access_error_at_the_callback_takes_the_step_up(client) -> None:
    state, _ = await start_sign_in(client)
    resp = await client.get("/v1/auth/callback", params={"state": state, **_aadsts("50076")})
    _assert_step_up(resp)


async def test_the_page_leaves_the_state_for_complete(client) -> None:
    """Rendering the page must not spend the state, or the page's own post would be refused."""
    resp = await sign_in(client, oid="page-then-post-oid")
    assert resp.headers["location"] == settings.FRONTEND_URL


# --- provisioning ---------------------------------------------------------------------------


async def test_first_signin_provisions_user_and_sets_cookies(client, db_session) -> None:
    resp = await sign_in(client, oid="brand-new-oid")

    assert resp.status_code == 302
    assert resp.headers["location"] == settings.FRONTEND_URL

    user = await db_session.scalar(select(User).where(User.azure_oid == "brand-new-oid"))
    assert user is not None
    assert user.email == "citizen@rvaiglobal.com"
    assert user.upn == "citizen@rvaiglobal.com"
    assert user.token_version == 0
    assert user.has_signed_in is True

    token_count = await db_session.scalar(
        select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user.id)
    )
    assert token_count == 1

    cookies = _set_cookies(resp)
    assert {"session", "refresh", "csrf"} <= set(cookies)


async def test_returning_signin_updates_profile_preserves_token_version(
    client, db_session
) -> None:
    existing = await UserFactory.create(
        db_session, azure_oid="returning-oid", email="old@rvaiglobal.com", token_version=5
    )
    resp = await sign_in(client, oid="returning-oid", email="new@rvaiglobal.com")

    assert resp.status_code == 302
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.azure_oid == "returning-oid")
    )
    assert count == 1
    await db_session.refresh(existing)
    assert existing.email == "new@rvaiglobal.com"
    assert existing.token_version == 5  # revocation state preserved


async def test_a_first_sign_in_lands_on_the_row_created_from_the_directory(
    client, db_session
) -> None:
    pre_created = await UserFactory.create(
        db_session,
        azure_oid="pre-created-oid",
        email="from.directory@rvaiglobal.com",
        display_name="From Directory",
        token_version=2,
        has_signed_in=False,
    )
    pre_created_id = pre_created.id

    resp = await sign_in(
        client, oid="pre-created-oid", email="signed.in@rvaiglobal.com", name="Signed In"
    )

    assert resp.headers["location"] == settings.FRONTEND_URL
    rows = (
        await db_session.scalars(select(User).where(User.azure_oid == "pre-created-oid"))
    ).all()
    assert len(rows) == 1
    await db_session.refresh(rows[0])
    assert rows[0].id == pre_created_id
    assert rows[0].token_version == 2
    assert rows[0].email == "signed.in@rvaiglobal.com"
    assert rows[0].display_name == "Signed In"
    assert rows[0].has_signed_in is True


async def test_a_suspended_pre_created_user_is_refused_and_stays_not_signed_in(
    app, client, db_session
) -> None:
    """Each request gets its own savepoint session, closed uncommitted the way `get_db` closes
    one, so the refused sign-in's upsert is undone here as it is in production."""
    pre_created = await UserFactory.create(
        db_session,
        azure_oid="suspended-pre-created-oid",
        email="from.directory@rvaiglobal.com",
        has_signed_in=False,
        suspended_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    async def _request_session():
        async with AsyncSession(
            bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as session:
            yield session

    app.dependency_overrides[get_db] = _request_session
    resp = await sign_in(client, oid="suspended-pre-created-oid", email="signed.in@rvaiglobal.com")

    _assert_login_error(resp, "account_suspended")
    await db_session.refresh(pre_created)
    assert pre_created.has_signed_in is False
    assert pre_created.email == "from.directory@rvaiglobal.com"


# --- the ID token, fail-closed ----------------------------------------------------------------


async def test_wrong_tenant_redirects_to_login_error(client, db_session) -> None:
    resp = await sign_in(client, oid="foreign-oid", tid="ffffffff-ffff-ffff-ffff-ffffffffffff")

    _assert_login_error(resp, "wrong_tenant")
    assert _app_session_cookies(resp) == set()  # no session minted
    assert await db_session.scalar(select(User).where(User.azure_oid == "foreign-oid")) is None


@pytest.mark.parametrize(
    ("claims", "key"),
    [
        pytest.param({"nonce": "not-the-one-this-browser-sent"}, None, id="nonce"),
        pytest.param({"nonce": ABSENT}, None, id="no-nonce"),
        pytest.param({}, FOREIGN_KEY, id="signature"),
        pytest.param({"aud": "another-app"}, None, id="audience"),
        pytest.param({"aud": "another-app", "azp": settings.auth.client_id}, None, id="azp-alone"),
        pytest.param({"iss": "https://login.microsoftonline.com/other/v2.0"}, None, id="issuer"),
        pytest.param({"exp": 1_700_000_000, "nbf": 1_690_000_000}, None, id="expired"),
    ],
)
async def test_an_id_token_that_does_not_validate_signs_nobody_in(
    client, db_session, claims: dict[str, Any], key: Any
) -> None:
    """★ The ID token now reaches the backend through the browser, so every check that makes it
    trustworthy runs here. `azp-alone` pins the audience exactly: Authlib on its own accepts a
    foreign `aud` when `azp` names this client."""
    state, nonce = await start_sign_in(client)
    token = id_token(nonce, oid="forged-oid", **claims, **({"key": key} if key else {}))
    with capture_logs() as logs:
        resp = await complete(client, state, id_token=token)

    ref = _assert_login_error(resp, "invalid_callback")
    assert _app_session_cookies(resp) == set()
    assert await db_session.scalar(select(User).where(User.azure_oid == "forged-oid")) is None
    rejected = [entry for entry in logs if entry["event"] == "auth_callback_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["trace_id"] == ref


async def test_something_that_is_not_a_token_signs_nobody_in(client) -> None:
    state, _ = await start_sign_in(client)
    resp = await complete(client, state, id_token="not-a-jwt")
    _assert_login_error(resp, "invalid_callback")


async def test_a_post_with_neither_a_token_nor_an_error_signs_nobody_in(client) -> None:
    state, _ = await start_sign_in(client)
    resp = await complete(client, state)
    _assert_login_error(resp, "invalid_callback")


async def test_a_state_is_good_for_one_post_only(client) -> None:
    """★ Replay: the same form posted twice. Mutation check: drop `clear_state_data` in
    `identity_from_browser` and the second post signs in."""
    state, nonce = await start_sign_in(client)
    token = id_token(nonce, oid="replayed-oid")
    first = await complete(client, state, id_token=token)
    assert first.headers["location"] == settings.FRONTEND_URL

    second = await complete(client, state, id_token=token)
    _assert_login_error(second, "auth_failed")
    assert _app_session_cookies(second) == set()


async def test_a_post_from_a_browser_that_never_started_the_sign_in_is_refused(
    client, db_session
) -> None:
    """★ Login CSRF: a valid token posted without the transient cookie that started the sign-in
    (what a cross-site form carries, since that cookie is SameSite=Lax) signs nobody in."""
    state, nonce = await start_sign_in(client)
    client.cookies.clear()

    resp = await complete(client, state, id_token=id_token(nonce, oid="planted-oid"))

    _assert_login_error(resp, "auth_failed")
    assert await db_session.scalar(select(User).where(User.azure_oid == "planted-oid")) is None


async def test_keys_out_of_reach_fail_closed_not_500(client, monkeypatch) -> None:
    state, nonce = await start_sign_in(client)

    async def unreachable(force: bool = False) -> dict[str, Any]:
        raise httpx.ConnectError("entra unreachable")

    monkeypatch.setattr(get_oauth().entra, "fetch_jwk_set", unreachable)
    resp = await complete(client, state, id_token=id_token(nonce))
    _assert_login_error(resp, "auth_failed")


# --- optional-email handling --------------------------------------------------------------------


async def test_missing_email_provisions_via_preferred_username(client, db_session) -> None:
    resp = await sign_in(client, oid="no-email-oid", email=ABSENT)

    assert resp.headers["location"] == settings.FRONTEND_URL
    user = await db_session.scalar(select(User).where(User.azure_oid == "no-email-oid"))
    assert user is not None
    assert user.email == "citizen@rvaiglobal.com"  # fell back to preferred_username


async def test_missing_email_and_upn_rejected(client, db_session) -> None:
    resp = await sign_in(client, oid="nada-oid", email=ABSENT, preferred_username=ABSENT)

    _assert_login_error(resp, "invalid_callback")
    assert await db_session.scalar(select(User).where(User.azure_oid == "nada-oid")) is None


@pytest.mark.parametrize("field", ["oid", "sub"])
async def test_missing_oid_or_sub_rejected(client, field: str) -> None:
    resp = await sign_in(client, **{field: ABSENT})
    _assert_login_error(resp, "invalid_callback")


# --- cookie attributes + no-token-persistence ---------------------------------------------------


async def test_cookie_attributes_follow_the_matrix(client) -> None:
    resp = await sign_in(client, oid="cookie-oid")
    cookies = _set_cookies(resp)

    # session: HttpOnly, root path.
    assert "HttpOnly" in cookies["session"]
    assert "Path=/;" in cookies["session"] or cookies["session"].rstrip().endswith("Path=/")
    # refresh: HttpOnly, path-scoped to the refresh endpoint, NO Domain (host-only).
    assert "HttpOnly" in cookies["refresh"]
    assert "Path=/api/v1/auth/refresh" in cookies["refresh"]
    assert "Domain=" not in cookies["refresh"]
    # csrf: readable by JS -> NOT HttpOnly.
    assert "HttpOnly" not in cookies["csrf"]


async def test_only_refresh_hash_is_persisted_not_the_id_token(client, db_session) -> None:
    state, nonce = await start_sign_in(client)
    token = id_token(nonce, oid="hash-oid")
    resp = await complete(client, state, id_token=token)

    user = await db_session.scalar(select(User).where(User.azure_oid == "hash-oid"))
    assert user is not None
    row = await db_session.scalar(select(RefreshToken).where(RefreshToken.user_id == user.id))
    assert row is not None
    raw_refresh = _cookie_value(_set_cookies(resp)["refresh"])
    assert row.token_hash == hash_refresh_token(raw_refresh)
    assert row.token_hash != token


# --- diagnostics: every failure names itself in the log, and on screen -------------------------


async def test_a_browser_reported_error_is_logged_with_the_same_ref_as_the_bounce(client) -> None:
    state, _ = await start_sign_in(client)
    with capture_logs() as logs:
        resp = await complete(
            client, state, error="invalid_client", error_description="AADSTS7000215: bad client"
        )

    ref = _assert_login_error(resp, "auth_failed")
    failed = [entry for entry in logs if entry["event"] == "auth_callback_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "OAuthError"
    assert "AADSTS7000215" in failed[0]["detail"]
    assert failed[0]["trace_id"] == ref


async def test_the_page_reports_a_token_request_that_never_left_the_browser(client) -> None:
    state, _ = await start_sign_in(client)
    resp = await complete(
        client, state, error="token_request_failed", error_description="TypeError: Failed"
    )
    _assert_login_error(resp, "auth_failed")


async def test_each_failed_sign_in_gets_a_distinct_ref(client) -> None:
    """Two failures must not collapse to one id, or the log line cannot identify which
    attempt a user is holding a screenshot of."""
    state, _ = await start_sign_in(client)
    first = await complete(client, state, error="access_denied")
    state, _ = await start_sign_in(client)
    second = await complete(client, state, error="access_denied")
    first_ref = _assert_login_error(first, "auth_failed")
    second_ref = _assert_login_error(second, "auth_failed")
    assert first_ref != second_ref


# --- MFA step-up: AADSTS50078 and its family --------------------------------------------------


@pytest.mark.parametrize("code", ["50076", "50078", "50079", "70044"])
async def test_a_stale_mfa_session_is_sent_back_to_entra_once_with_prompt_login(
    client, code: str
) -> None:
    """★ Entra refuses the token request for a session whose MFA has expired, and every "try
    again" does the same. A Conditional Access refusal now asks Entra for a FRESH sign-in instead
    of bouncing to a retry that cannot work. Mutation check: drop the step-up arm and this goes red
    on the auth_failed bounce."""
    state, _ = await start_sign_in(client)
    with capture_logs() as logs:
        resp = await complete(client, state, **_aadsts(code))

    _assert_step_up(resp)
    assert _app_session_cookies(resp) == set()  # still no session: failing closed is unchanged
    assert "oauth_transient" in _set_cookies(resp)  # the one-retry marker rides this cookie
    step_up = [entry for entry in logs if entry["event"] == "auth_step_up_redirect"]
    assert len(step_up) == 1
    assert step_up[0]["aadsts"] == code
    assert re.fullmatch(r"[0-9a-f]{8}", step_up[0]["trace_id"])


async def test_a_second_rejection_after_the_step_up_bounces_to_reauth_required_never_loops(
    client,
) -> None:
    """★ One automatic retry, never a redirect loop between the backend and Entra."""
    state, _ = await start_sign_in(client)
    first = await complete(client, state, **_aadsts("50078"))
    retried = _assert_step_up(first)

    second = await complete(client, retried["state"], **_aadsts("50078"))

    _assert_login_error(second, "reauth_required")
    assert _app_session_cookies(second) == set()


async def test_a_successful_sign_in_after_the_step_up_clears_the_marker(client) -> None:
    state, _ = await start_sign_in(client)
    retried = _assert_step_up(await complete(client, state, **_aadsts("50078")))

    signed_in = await complete(
        client, retried["state"], id_token=id_token(retried["nonce"], oid="stepped-up-oid")
    )
    assert signed_in.headers["location"] == settings.FRONTEND_URL
    assert session_cookie_name() in _set_cookies(signed_in)

    # A later stale session gets its one automatic retry again, because success cleared the marker.
    state, _ = await start_sign_in(client)
    _assert_step_up(await complete(client, state, **_aadsts("50078")))


async def test_other_entra_rejections_keep_the_generic_bounce(client) -> None:
    state, _ = await start_sign_in(client)
    resp = await complete(
        client, state, error="invalid_client", error_description="AADSTS7000215: Invalid client."
    )
    _assert_login_error(resp, "auth_failed")


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(httpx.ConnectError("entra unreachable"), id="network"),
        pytest.param(OAuthError(error="server_error", description="metadata refused"), id="oauth"),
        pytest.param(ValueError("malformed discovery document"), id="malformed"),
    ],
)
async def test_a_step_up_that_cannot_reach_entra_falls_closed_to_the_banner(
    client, monkeypatch, failure: Exception
) -> None:
    entra = get_oauth().entra
    state, _ = await start_sign_in(client)

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise failure

    with monkeypatch.context() as patched:
        patched.setattr(entra, "authorize_redirect", unreachable)
        resp = await complete(client, state, **_aadsts("50078"))
    _assert_login_error(resp, "reauth_required")
    assert _app_session_cookies(resp) == set()

    # The marker went with the failed attempt, so once Entra answers again the next stale session
    # still gets its one automatic retry. Mutation check: drop the pop in `_step_up`'s failure arm
    # and this goes red on the banner.
    state, _ = await start_sign_in(client)
    _assert_step_up(await complete(client, state, **_aadsts("50078")))


async def test_a_forced_sign_in_that_entra_refuses_again_goes_to_the_banner(client) -> None:
    """The login page's own forced sign-in already spent the one retry: a refusal after it is the
    banner, not a second trip to Entra."""
    state, _ = await start_sign_in(client, prompt="login")
    resp = await complete(client, state, **_aadsts("50078"))
    _assert_login_error(resp, "reauth_required")


async def test_an_ordinary_sign_in_clears_a_forced_one_it_replaced(client) -> None:
    """A forced sign-in that was abandoned for the ordinary button must not leave its marker
    behind, or the next stale MFA session would go straight to the banner without its automatic
    retry. Mutation check: drop the pop in `login` and this goes red on the banner."""
    await start_sign_in(client, prompt="login")
    state, _ = await start_sign_in(client)

    resp = await complete(client, state, **_aadsts("50078"))

    _assert_step_up(resp)
