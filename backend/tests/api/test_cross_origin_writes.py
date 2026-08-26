"""The cross-origin write guard — what moving generated apps onto a BIAL hostname cost.

WHY IT EXISTS, stated once here so nobody deletes it as belt-and-braces. Generated apps used to
be served from `*.azurecontainerapps.io`, a different registrable domain from the portal's. That
made them CROSS-SITE, and `SameSite=Lax` withheld the session cookie from anything an app sent to
the control plane — so the API's opt-in CSRF design had a second, structural line behind it.

Serving apps from `citizenapps.bialairport.com` removes that line: they are now SAME-SITE with
`blrcitizen.bialairport.com`, and Lax sends the cookie. Since a generated app's code is written by
a model from a citizen's prompt, it is untrusted by construction — and the admin, attachment and
feedback routers declare no `RequireCsrf`. This guard is what closes the door that opened.
"""

from __future__ import annotations

import pytest

from src.config import settings

_APP_ORIGIN = "https://citizenapps.bialairport.com"


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
async def test_a_mutating_request_from_another_origin_is_refused(client, method: str) -> None:
    """★ THE MUTANT THAT MUST FAIL. Remove the guard and a generated app can drive any mutating
    route with the viewer's session attached.

    Asserted against a route that declares NO `RequireCsrf` — that is the whole point. A route
    which already has CSRF was never the exposure.
    """
    # `request(...)` rather than the per-verb helpers: httpx's `delete()` takes no body, and
    # the guard has to hold for a bodyless DELETE exactly as it does for a POST.
    response = await client.request(
        method.upper(), "/v1/admin/apps", headers={"Origin": _APP_ORIGIN}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cross_origin_write_refused"


async def test_the_refusal_lands_before_the_route_and_before_auth(client) -> None:
    """It must not depend on the route existing, on the caller being signed in, or on the body
    parsing — a guard that only fires deep in a handler is a guard with holes in front of it."""
    response = await client.post(
        "/v1/this-route-does-not-exist", headers={"Origin": _APP_ORIGIN}, content=b"not json"
    )
    assert response.status_code == 403


async def test_the_portals_own_origin_is_allowed_through(client) -> None:
    """The SPA sends its own origin on every mutating call. If this went red the portal would be
    entirely unusable, which is the failure mode a guard like this is most likely to cause."""
    response = await client.post(
        "/v1/admin/apps", headers={"Origin": settings.FRONTEND_URL}, json={}
    )
    assert response.status_code != 403


async def test_a_request_with_no_origin_is_allowed_through(client) -> None:
    """Browsers attach `Origin` to every mutating request, cross-site form posts included, so an
    absent one means a non-browser caller — curl, a probe, a server-to-server call. Those hold no
    cookie to abuse, and refusing them would break every scripted client for no gain."""
    response = await client.post("/v1/admin/apps", json={})
    assert response.status_code != 403


@pytest.mark.parametrize("method", ["get", "head"])
async def test_reads_from_another_origin_are_not_touched(client, method: str) -> None:
    """`GET`/`HEAD` are excluded deliberately: they are not supposed to mutate, and what a
    cross-origin caller may READ is the CORS layer's decision, not this guard's."""
    response = await getattr(client, method)("/v1/health", headers={"Origin": _APP_ORIGIN})
    assert response.status_code != 403
