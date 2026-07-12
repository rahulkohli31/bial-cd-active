"""Runner + preview serving (U3, R20/R21): byte-correct per-route CSP + sandbox,
serve-gating by status, loginRequired-gated refresh-free token injection, and the
cookie-authed runner-token mint that never exposes the session JWT/refresh."""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppStatus
from src.services.appserving.csp import build_frame_csp, build_preview_csp, build_shell_csp
from src.services.appserving.runner_token import verify_runner_token
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import AppRegistryFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_COMPILED = "var PreviewApp=()=>React.createElement('div',null,'live');"
# The test client's origin (httpx ASGITransport base_url host), used in the frame /
# preview connect-src.
_ORIGIN = "http://test"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _make_app(db: AsyncSession, **overrides: object):
    user = await UserFactory.create(db)
    return await AppRegistryFactory.create(db, user_id=user.id, **overrides)


async def _approved_app(db: AsyncSession, **overrides: object):
    return await _make_app(
        db,
        status=AppStatus.APPROVED,
        approved_snapshot={"compiled": _COMPILED, "src": "x", "entry": "PreviewApp"},
        **overrides,
    )


# --- serve-gating --------------------------------------------------------------


async def test_approved_app_serves_shell(client, db_session) -> None:
    app = await _approved_app(db_session)
    resp = await client.get(f"/apps/{app.id}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # The sandboxed iframe attribute — opaque origin (NO allow-same-origin).
    assert "allow-scripts allow-forms allow-downloads" in body
    assert "allow-same-origin" not in body
    # Config injected with the appKey + appId.
    assert app.app_key in body
    assert str(app.id) in body


async def test_disabled_app_not_served(client, db_session) -> None:
    app = await _make_app(
        db_session,
        status=AppStatus.DISABLED,
        approved_snapshot={"compiled": _COMPILED},
    )
    resp = await client.get(f"/apps/{app.id}")
    assert resp.status_code == 404
    assert "not available" in resp.text.lower()


async def test_pending_without_snapshot_not_served(client, db_session) -> None:
    app = await _make_app(db_session, status=AppStatus.PENDING, approved_snapshot=None)
    assert (await client.get(f"/apps/{app.id}")).status_code == 404
    assert (await client.get(f"/apps/{app.id}/frame")).status_code == 404


async def test_pending_with_prior_snapshot_still_served(client, db_session) -> None:
    # A pending re-submit keeps serving the prior approved snapshot.
    app = await _make_app(
        db_session,
        status=AppStatus.PENDING,
        approved_snapshot={"compiled": _COMPILED},
    )
    assert (await client.get(f"/apps/{app.id}")).status_code == 200
    assert (await client.get(f"/apps/{app.id}/frame")).status_code == 200


# --- CSP / sandbox (served header == the byte-exact builder output) ------------


async def test_shell_csp_matches_builder(client, db_session) -> None:
    app = await _approved_app(db_session)
    resp = await client.get(f"/apps/{app.id}")
    assert resp.headers["content-security-policy"] == build_shell_csp()
    # Shell frames its own frame same-origin (global DENY would break it).
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"


async def test_frame_csp_matches_builder_and_is_scoped(client, db_session) -> None:
    app = await _approved_app(db_session)
    resp = await client.get(f"/apps/{app.id}/frame")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    # Byte-exact against the frame builder (which the csp unit test pins).
    assert csp == build_frame_csp(_ORIGIN)
    # connect-src scoped to self + the explicit portal origin — no wildcard egress.
    assert f"connect-src 'self' {_ORIGIN}" in csp
    # img-src allows data:+blob: but NOT a bare https: (token-beacon prevention).
    assert "img-src 'self' data: blob:" in csp
    assert "img-src 'self' https:" not in csp
    assert "form-action 'none'" in csp
    # The pre-compiled app JS is embedded.
    assert _COMPILED in resp.text


async def test_preview_csp_matches_builder_and_differs_from_frame(client) -> None:
    resp = await client.get("/preview")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    assert csp == build_preview_csp(_ORIGIN)
    # Preview is strictly more permissive on script-src than the frame (in-browser
    # JSX compile), so the two policies must differ.
    assert csp != build_frame_csp(_ORIGIN)
    assert "@babel/standalone" in resp.text


# --- injection parity ----------------------------------------------------------


async def test_injection_is_refresh_free_and_uses_user_shape(client, db_session) -> None:
    app = await _approved_app(db_session)
    shell = (await client.get(f"/apps/{app.id}")).text
    # The shell posts {config, accessToken, user} — never the refresh token.
    assert "accessToken:accessToken" in shell
    assert "user:currentUser" in shell
    assert "refresh" not in shell.lower()


async def test_preview_and_frame_inject_same_user_global(client, db_session) -> None:
    app = await _approved_app(db_session)
    frame = (await client.get(f"/apps/{app.id}/frame")).text
    preview = (await client.get("/preview")).text
    # Preview↔runner parity: both wire window.__BIAL_USER before mount.
    assert "window.__BIAL_USER" in frame
    assert "window.__BIAL_USER" in preview
    assert "runnerReady" in frame
    assert "previewReady" in preview


def test_preview_shell_mounts_once_and_rerenders_idempotently() -> None:
    # F-12 regression guard. String-level by nature (annotate as such): a pure Python test can
    # only assert the cached-root SHAPE — the authoritative proof is the in-browser console
    # check in the projects-e2e journey, where a fresh ReactDOM.createRoot(root) per build turn
    # raised "createRoot() on a container that has already been passed" 8× over 12 turns.
    from src.services.appserving.shells import PREVIEW_SHELL

    # The root is created ONCE behind a cache guard, then a bare render reuses it every turn.
    assert "if(!__previewRoot)__previewRoot=ReactDOM.createRoot(root);" in PREVIEW_SHELL
    assert "__previewRoot.render(React.createElement(window.__PreviewApp));" in PREVIEW_SHELL
    # NOT the old unconditional create-and-render-in-one that leaked a root each turn...
    assert "ReactDOM.createRoot(root).render(" not in PREVIEW_SHELL
    # ...and NOT the serving frame's one-shot `if(__bialRoot)return;` short-circuit, which
    # would freeze the preview after turn 1 — the preview must re-render on every postMessage.
    assert "if(__previewRoot)return" not in PREVIEW_SHELL
    # Error recovery: an error wipes the container (textContent=''), detaching the DOM the cached
    # root owns, so the error path must tear the root DOWN — otherwise the next good turn
    # reconciles against detached nodes and wedges. renderPreview routes BOTH error branches
    # through __previewError (never bare __bialShowError), which unmounts and nulls the root.
    reset_root = "if(__previewRoot){try{__previewRoot.unmount();}catch(e){}__previewRoot=null;}"
    assert reset_root in PREVIEW_SHELL
    assert "__previewError(root,'App did not define a PreviewApp component.')" in PREVIEW_SHELL
    assert "catch(e){__previewError(root,'Preview error:" in PREVIEW_SHELL


# --- runner-token mint (P1) ----------------------------------------------------


async def test_runner_token_requires_session_cookie(client, db_session) -> None:
    app = await _approved_app(db_session)
    resp = await client.post(f"/apps/{app.id}/runner-token")
    assert resp.status_code == 401
    # Cookie-auth denial is the DetailBody shape, not the AppApiError envelope.
    assert set(resp.json()) == {"detail"}


async def test_runner_token_mints_scoped_token_without_exposing_session(
    client, db_session
) -> None:
    user = await UserFactory.create(db_session, email="runner@rvaiglobal.com")
    app = await AppRegistryFactory.create(
        db_session,
        user_id=user.id,
        status=AppStatus.APPROVED,
        approved_snapshot={"compiled": _COMPILED},
    )
    session_jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    resp = await client.post(f"/apps/{app.id}/runner-token", headers=_cookie(session_jwt))
    assert resp.status_code == 200
    body = resp.json()
    # Only the fresh token + display user — never the refresh cookie or raw JWT field.
    assert set(body) == {"accessToken", "user"}
    assert "refresh" not in resp.text.lower()
    # The minted token verifies to the caller (a real short-lived token).
    claims = verify_runner_token(body["accessToken"])
    assert claims.user_id == user.id
    # No refresh cookie is set on this response.
    assert "set-cookie" not in {k.lower() for k in resp.headers}


async def test_runner_token_unknown_app_is_404(client, db_session) -> None:
    from uuid import uuid4

    user = await UserFactory.create(db_session)
    session_jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    resp = await client.post(f"/apps/{uuid4()}/runner-token", headers=_cookie(session_jwt))
    assert resp.status_code == 404
    # The route's own raise is the AppApiError envelope (distinct from the cookie 401).
    assert resp.json() == {"error": {"message": "App not found."}}


async def test_unhandled_error_becomes_clean_500(app, db_session, monkeypatch) -> None:
    # An unexpected failure inside a runner route must surface as the global unhandled-500
    # (`{"detail": "Internal server error"}`) — never leak internals. Force `render_shell`
    # to blow up on an otherwise-serveable app and observe the response with a non-raising
    # transport (the conftest `client` re-raises app exceptions).
    approved = await _approved_app(db_session)

    def _boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("render exploded")

    monkeypatch.setattr("src.api.v1.apps.runner.render_shell", _boom)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(f"/apps/{approved.id}")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal server error"}


def test_runner_documents_error_codes_in_openapi() -> None:
    # runner mounts outside v1, so its 500 default is router-local (U8). runner_token
    # documents 401/404; the HTML shell/frame document their returned 404.
    from src.main import create_app

    paths = create_app().openapi()["paths"]
    token = set(paths["/apps/{app_id}/runner-token"]["post"]["responses"])
    assert {"401", "404", "500"} <= token
    assert "404" in paths["/apps/{app_id}"]["get"]["responses"]  # HTML shell
    assert "404" in paths["/apps/{app_id}/frame"]["get"]["responses"]  # HTML frame
