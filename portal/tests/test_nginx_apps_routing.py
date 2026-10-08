"""Behavioural tests for the portal edge's apps router.

Every test here drives a real nginx with the real `portal/nginx.conf` against a stub app
container. The sibling Vitest suite pins the config's shape; this pins what it DOES.

The distinction is load-bearing rather than tidy. Three of the failures this router is most
exposed to — a WebSocket upgrade answered as an ordinary request, a `proxy_pass` URI collapsing
every path to `/`, and a keyless request arriving at the right container with the wrong path —
all leave `nginx -t` green and all pass a structural assertion.
"""

from __future__ import annotations

import re

import pytest
from _router import (
    ALIAS,
    APPS_DOMAIN,
    APPS_HOSTNAME,
    BACKEND_ALIAS,
    CACHE_ALIAS,
    DEAD_ALIAS,
    GHOST_ALIAS,
    OTHER_ALIAS,
    OTHER_PASS,
    OTHER_SBX_KEY,
    OWNER_CACHE_ALIAS,
    PASS,
    PORTAL_ORIGIN,
    PUB_KEY,
    ROUTE_TOKEN,
    ROUTER_IMAGE,
    SBX_KEY,
    SHR_ALIAS,
    SHR_KEY,
    UNKNOWN_ALIAS,
    Router,
    _free_port,
    _run,
    _wait_for_router,
    boot_router,
    requires_docker,
)

pytestmark = [pytest.mark.integration, requires_docker]


def _fields(body: str) -> dict[str, str]:
    """The stub's echoed `KEY=value` fields. The request TARGET is deliberately NOT one of them
    — it is positional (see `_target`), because a URL can itself contain `=` and `|`."""
    if not body.startswith("REQ="):
        raise AssertionError(f"expected the stub's echo, got: {body[:400]!r}")
    return dict(p.split("=", 1) for p in body.strip().split("|") if "=" in p)


def _target(body: str) -> str:
    """The request TARGET the router composed — the assertion that matters most here.

    Asserting the upstream HOST alone is how the keyless arm's missing prefix survived the
    first draft of this design: the request reached the right container and the framework
    answered its own 404, because under `basePath` every route lives behind the prefix.
    """
    parts = body.strip().split("|")
    return parts[1]


def _host(body: str) -> str:
    return _fields(body)["HOST"]


# --------------------------------------------------------------------------------------
# the keyed arm
# --------------------------------------------------------------------------------------


def test_config_passes_nginx_t_after_substitution(router: Router) -> None:
    """The shipped config, with real values substituted, actually parses.

    A template that only ever gets eyeballed is a config that fails on the deploy host. This is
    the cheapest possible proof it does not.
    """
    import subprocess

    proc = subprocess.run(
        ["docker", "exec", router.container, "nginx", "-t"], capture_output=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert b"syntax is ok" in proc.stderr


@pytest.mark.parametrize(
    ("key", "container"), [(ALIAS, SBX_KEY), (SHR_ALIAS, SHR_KEY), (PUB_KEY, PUB_KEY)]
)
def test_keyed_request_reaches_its_app_with_the_prefix_intact(
    router: Router, key: str, container: str
) -> None:
    """The prefix is real end to end: the router forwards it unchanged rather than
    stripping it, because the app is configured to live at it. The address carries the alias (or
    a published name); the host dialled is the container the lookup named."""
    status, _, body = router.request(f"/a/{key}/dashboard?tab=1")
    assert status == 200
    assert _target(body) == f"/a/{key}/dashboard?tab=1"
    assert _host(body) == f"{container}.{APPS_DOMAIN}"


def test_upstream_host_is_the_container_not_the_browser_host(router: Router) -> None:
    """Azure Container Apps routes by Host. Forwarding the browser's host would make its front
    door reject the hop before the app ever runs; the browser-facing name still travels in
    X-Forwarded-Host for any absolute-URL reconstruction the app needs."""
    _, _, body = router.request(f"/a/{ALIAS}/")
    fields = _fields(body)
    assert fields["HOST"] == f"{SBX_KEY}.{APPS_DOMAIN}"
    assert fields["XFH"] == APPS_HOSTNAME


def test_post_body_and_method_survive_the_hop(router: Router) -> None:
    status, _, body = router.request(
        f"/a/{ALIAS}/submit",
        method="POST",
        headers={"Content-Type": "text/plain", "Content-Length": "5"},
        body=b"hello",
    )
    assert status == 200
    assert body.startswith("REQ=POST|")
    assert _target(body) == f"/a/{ALIAS}/submit"


@pytest.mark.parametrize(
    "bad",
    [
        "1a2b3c4d5e6f70819a2b3c4d5e6f708",  # 31 hex
        "1a2b3c4d5e6f70819a2b3c4d5e6f70811",  # 33 hex
        "1A2B3C4D5E6F70819A2B3C4D5E6F7081",  # uppercase
        "1a2b3c4d5e6f70819a2b3c4d5e6f708g",  # non-hex
        "dev-1a2b3c4d5e6f70819a2b3c4d5e6f",  # wrong prefix
    ],
)
def test_a_malformed_key_is_404_and_never_another_app(router: Router, bad: str) -> None:
    """The narrow match is what turns a mistyped key into a 404 rather than a DNS lookup
    for an attacker-named host — and, just as importantly, it must not fall through to the
    keyless arm, which would resolve it from a cookie and serve SOMEONE ELSE'S app under the
    address the person typed."""
    status, _, body = router.request(f"/a/{bad}/", headers={"Cookie": f"__Host-bial_app={ALIAS}"})
    assert status == 404
    assert "REQ=" not in body


def test_dot_dot_normalizes_before_location_matching(router: Router) -> None:
    """`/a/<key>/../` collapses to `/a/` BEFORE nginx picks a location, so it lands on the
    malformed-key arm and reaches no upstream at all. Only a running nginx can show that."""
    status, _, body = router.request(f"/a/{ALIAS}/../")
    assert status == 404
    assert "REQ=" not in body


def test_a_path_that_names_two_aliases_is_refused_not_routed_under_one(router: Router) -> None:
    """The lookup reads the raw request URI and this location matches the normalized one, so a
    request whose two disagree must not be looked up as one alias and routed as another."""
    status, _, body = router.request(f"/a/{ALIAS}/../../a/{OTHER_ALIAS}/x")
    assert status == 404
    assert "REQ=" not in body
    # The keyless arm has the same exposure: a raw path naming one alias that normalizes away from
    # `/a/`, with the Referer naming another.
    referer = {"Referer": f"https://{APPS_HOSTNAME}/a/{OTHER_ALIAS}/page"}
    status, _, body = router.request(f"/a/{ALIAS}/../../x", headers=referer)
    assert status == 404
    assert "REQ=" not in body


def test_unknown_but_wellformed_key_is_404_and_names_no_upstream(router: Router) -> None:
    """The router holds no registry, so an unknown key fails as a DNS MISS. Left alone
    that is a 502 whose body and headers can name the composed upstream and hand anyone who
    reaches the gateway the environment's naming convention."""
    status, headers, body = router.request(f"/a/{UNKNOWN_ALIAS}/")
    assert status == 404
    assert APPS_DOMAIN not in body
    assert UNKNOWN_ALIAS not in body
    joined = " ".join(headers.values())
    assert APPS_DOMAIN not in joined


def test_the_404_page_has_a_body_and_a_way_back(router: Router) -> None:
    """The person who reaches this is a BIAL employee who followed a link, not a developer. A
    stale key, a reaped sandbox and a typo all land here."""
    status, headers, body = router.request(f"/a/{UNKNOWN_ALIAS}/")
    assert status == 404
    assert headers["content-type"].startswith("text/html")
    assert PORTAL_ORIGIN in body
    assert "not available" in body.lower()


@pytest.mark.parametrize("dest", ["iframe", "frame", "embed", "object"])
def test_the_framed_404_never_follows_the_readers_dark_scheme(router: Router, dest: str) -> None:
    """A BLACK RECTANGLE INSIDE A LIGHT PANE reads as a crash, not as a colour scheme.

    Production report, with a screenshot: "the black screen is coming". The framed body carried a
    `prefers-color-scheme: dark` block, so a citizen whose OS is set to dark got this page drawn
    near-black inside the workspace's permanently light preview rectangle — and a black rectangle
    where an app should be is indistinguishable from the app having died.

    The pane it is drawn into does not follow the OS, so neither may this. `color-scheme: light`
    says so to the browser as well, which is what stops form controls and scrollbars being
    auto-darkened underneath a light page.

    THE TOP-LEVEL PAGE IS DELIBERATELY THE OPPOSITE — see the test below. It owns a whole tab, so
    following the reader there is correct. Two pages, two answers, and the difference is exactly
    whether something else already decided the background.
    """
    _, _, body = router.request(f"/a/{UNKNOWN_ALIAS}/", headers={"Sec-Fetch-Dest": dest})
    assert "color-scheme:light" in body.replace(" ", "")
    assert "prefers-color-scheme" not in body
    # Liveness: this is the real framed body and not an empty response, which is what an
    # absence assertion on its own would happily accept.
    assert "isn" in body and "running" in body.lower()


def test_the_top_level_404_DOES_follow_the_readers_dark_scheme(router: Router) -> None:
    """The other half, asserted so the two pages cannot be "fixed" into agreeing.

    This one is a whole tab with nothing else deciding its background, so honouring the reader's
    setting is right. It declares `color-scheme: dark light` so the browser darkens its own
    furniture to match, and keeps the media block that does the darkening.
    """
    _, _, body = router.request(f"/a/{UNKNOWN_ALIAS}/")
    assert "color-scheme:darklight" in body.replace(" ", "")
    assert "prefers-color-scheme:dark" in body.replace(" ", "")
    assert "not available" in body.lower()


@pytest.mark.parametrize("dest", ["iframe", "frame", "embed", "object"])
def test_the_framed_404_offers_no_link_out_of_the_frame(router: Router, dest: str) -> None:
    """THE PREVIEW PANE IS NOT A TAB, and the page it gets must not pretend otherwise.

    Production report: the workspace's preview pane framed this page, its "Go to the BIAL app
    portal" button was a plain `<a href>` with no target, and pressing it loaded the ENTIRE portal
    inside the preview rectangle. The obvious fix — `target="_top"` — is unavailable: the pane
    frames apps with a sandbox that deliberately withholds `allow-top-navigation`, because the
    framed app is unreviewed agent-generated code and granting top-nav would let any generated app
    redirect the citizen's whole browser tab. So the page drops the link when it is framed.

    Asserted as the ABSENCE OF A LINK plus the PRESENCE OF A BODY: the absence alone would pass on
    an empty response, which is the false-green this repo has shipped before."""
    status, headers, body = router.request(
        f"/a/{UNKNOWN_ALIAS}/", headers={"Sec-Fetch-Dest": dest}
    )
    assert status == 404
    assert headers["content-type"].startswith("text/html")
    # Liveness: there IS a page, and it speaks to the citizen who built the app.
    assert "isn" in body and "running" in body.lower()
    # The guarantee: nothing to press, and no portal address to press it towards.
    assert "<a " not in body.lower()
    assert PORTAL_ORIGIN not in body
    # …and none of the tab-reader's advice, which is nonsense addressed to the app's own author.
    assert "shared the link" not in body.lower()


@pytest.mark.parametrize("dest", ["document", None])
def test_a_top_level_reader_still_gets_the_way_back(router: Router, dest: str | None) -> None:
    """The other half of the pair, and the reason the map defaults to the full page: a real tab —
    and any browser too old to send `Sec-Fetch-Dest` at all — still gets the button, because for
    that reader it is the only way back."""
    headers = {"Sec-Fetch-Dest": dest} if dest else {}
    status, _, body = router.request(f"/a/{UNKNOWN_ALIAS}/", headers=headers)
    assert status == 404
    assert PORTAL_ORIGIN in body
    assert "not available" in body.lower()


@pytest.mark.parametrize(
    "target", ["/_sup/health", "/_sup", f"/a/{ALIAS}/_sup/health", f"/a/{SHR_ALIAS}/_sup/health"]
)
def test_supervisor_surface_is_refused_at_the_router(router: Router, target: str) -> None:
    """The supervisor is bearer-guarded downstream, but this router claims as an invariant that
    it is unreachable from a browser, and an invariant should be enforced where it is claimed
    rather than depend on a check designed for a different threat. The `shr-` case (#198) is
    the one the issue names explicitly: a shared-runtime container's supervisor must be exactly
    as unreachable as a build sandbox's or a published app's."""
    status, _, body = router.request(target, headers={"Cookie": f"__Host-bial_app={ALIAS}"})
    assert status == 404
    assert "REQ=" not in body


def test_websocket_upgrade_is_answered_101_on_the_keyed_arm(router: Router) -> None:
    """Live reload rides this. Without `proxy_http_version 1.1` in THIS server block nginx
    defaults to HTTP/1.0 and answers the upgrade as an ordinary request — with the `Upgrade`
    header still forwarded, which is why only a real 101 proves anything."""
    status, head = router.websocket(f"/a/{ALIAS}/_next/webpack-hmr")
    assert status == 101, head
    assert "sec-websocket-accept" in head.lower()
    assert f"X-Stub-Target: /a/{ALIAS}/_next/webpack-hmr" in head


def test_apps_site_proxies_nothing_to_the_backend(router: Router) -> None:
    """A generated app must not be able to reach the control plane on its own origin. `/api/`
    on the apps host is an ordinary keyless app path, never the portal's backend route."""
    status, _, body = router.request("/api/v1/auth/me")
    assert status == 404
    assert "REQ=" not in body


# --------------------------------------------------------------------------------------
# the portal site must be unaffected (regression)
# --------------------------------------------------------------------------------------


def test_portal_site_is_still_the_default_server(router: Router) -> None:
    """nginx serves an unmatched Host from the FIRST block on the listen address. Move the apps
    block above the portal's and every portal request with an unexpected Host is served by the
    apps site — the portal goes dark, with `nginx -t` green."""
    status, _, body = router.request("/", host="something-unmatched.invalid")
    assert status == 200
    assert "portal-index" in body


def test_portal_spa_fallback_still_serves_on_an_unmatched_host(router: Router) -> None:
    status, _, body = router.request("/projects/123", host="portal.bial.test")
    assert status == 200
    assert "portal-index" in body


def test_apps_host_does_not_serve_the_spa(router: Router) -> None:
    """The apps site serves no SPA files. If it did, an app request that missed its route would
    silently render the portal instead of failing."""
    status, _, body = router.request("/index.html")
    assert status == 404
    assert "portal-index" not in body


# --------------------------------------------------------------------------------------
# request-body ceilings
# --------------------------------------------------------------------------------------

_MIB = 1024 * 1024


def test_the_portal_site_passes_a_body_at_the_attachment_routes_own_ceiling(
    router_without_backend: Router,
) -> None:
    """The attachment route refuses above 44 MiB with a sentence naming the file's own limit, so
    the edge has to let that much through or a citizen reads nginx's bare 413 instead. This
    harness has no backend: a body past the ceiling ends at the unreachable upstream."""
    status, _, _ = router_without_backend.request(
        "/api/attachments",
        host="portal.bial.test",
        method="POST",
        headers={"Content-Type": "application/json"},
        body=b"x" * (44 * _MIB),
    )
    assert status == 502


@pytest.mark.parametrize(
    ("host", "target", "ceiling_mib"),
    [
        ("portal.bial.test", "/api/attachments", 45),
        (APPS_HOSTNAME, f"/a/{ALIAS}/submit", 40),
    ],
)
def test_each_site_refuses_a_body_over_its_own_ceiling(
    router: Router, host: str, target: str, ceiling_mib: int
) -> None:
    """Only the length is declared: nginx refuses on the header without reading any body."""
    status, _, _ = router.request(
        target,
        host=host,
        method="POST",
        headers={"Content-Length": str(ceiling_mib * _MIB + 1)},
    )
    assert status == 413


# --------------------------------------------------------------------------------------
# the keyless arm
# --------------------------------------------------------------------------------------


def test_keyless_request_is_prefixed_not_merely_routed(router: Router) -> None:
    """The one assertion this whole arm exists for.

    Under `basePath` Next gates every route behind the prefix, route handlers included. A
    request proxied to the right container as `/api/items` is answered with the framework's own
    404. Asserting the upstream host would pass against that broken behaviour; asserting the
    REQUEST LINE is what distinguishes a working fallback from a decorative one.
    """
    _, _, body = router.request(
        "/api/items", headers={"Referer": f"https://{APPS_HOSTNAME}/a/{ALIAS}/dashboard"}
    )
    assert _target(body) == f"/a/{ALIAS}/api/items"
    assert _host(body) == f"{SBX_KEY}.{APPS_DOMAIN}"


def test_keyless_query_string_survives_the_rewrite(router: Router) -> None:
    _, _, body = router.request(
        "/api/items?page=2&q=a+b",
        headers={"Referer": f"https://{APPS_HOSTNAME}/a/{ALIAS}/"},
    )
    assert _target(body) == f"/a/{ALIAS}/api/items?page=2&q=a+b"


def test_referer_beats_cookie_so_two_open_tabs_stay_correct(router: Router) -> None:
    """The failure this ordering prevents is not a broken image. A host-wide cookie is
    last-write-wins, so with two apps open one app's form post lands in the other app's
    database — and each app owns its own database."""
    _, _, body = router.request(
        "/api/items",
        headers={
            "Referer": f"https://{APPS_HOSTNAME}/a/{ALIAS}/page",
            "Cookie": f"__Host-bial_app={OTHER_ALIAS}",
        },
    )
    assert _target(body) == f"/a/{ALIAS}/api/items"
    assert _host(body) == f"{SBX_KEY}.{APPS_DOMAIN}"


def test_cookie_answers_a_top_level_navigation_with_no_referer(router: Router) -> None:
    _, _, body = router.request(
        "/reports",
        headers={"Cookie": f"__Host-bial_app={PUB_KEY}", "Sec-Fetch-Mode": "navigate"},
    )
    assert _target(body) == f"/a/{PUB_KEY}/reports"


@pytest.mark.parametrize(
    ("target", "headers"),
    [
        ("/favicon.ico", {}),  # the browser asks at the origin root regardless of the page
        ("/hero.png", {"Referer": f"https://{APPS_HOSTNAME}/a/{ALIAS}/styles.css"}),
        ("/logo.png", {"Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}),
    ],
)
def test_plain_html_and_browser_originated_requests_arrive_prefixed(
    router: Router, target: str, headers: dict[str, str]
) -> None:
    """`basePath` rewrites what the FRAMEWORK generates. It does not touch a plain `<img src>`,
    a CSS `url()`, or `/favicon.ico`, so this arm carries steady traffic rather than the
    occasional hand-written call."""
    _, _, body = router.request(target, headers={"Cookie": f"__Host-bial_app={ALIAS}", **headers})
    assert _target(body) == f"/a/{ALIAS}{target}"


def test_keyless_form_post_arrives_prefixed_with_its_body(router: Router) -> None:
    status, _, body = router.request(
        "/submit",
        method="POST",
        headers={
            "Referer": f"https://{APPS_HOSTNAME}/a/{ALIAS}/form",
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": "7",
        },
        body=b"a=1&b=2",
    )
    assert status == 200
    assert body.startswith("REQ=POST|")
    assert _target(body) == f"/a/{ALIAS}/submit"


def test_keyless_websocket_is_also_prefixed_and_upgraded(router: Router) -> None:
    status, head = router.websocket(
        "/_next/webpack-hmr",
        headers={"Referer": f"https://{APPS_HOSTNAME}/a/{ALIAS}/page"},
    )
    assert status == 101, head
    assert f"X-Stub-Target: /a/{ALIAS}/_next/webpack-hmr" in head


def test_no_signal_at_all_is_404_never_a_fallthrough(router: Router) -> None:
    status, _, body = router.request("/api/items")
    assert status == 404
    assert "REQ=" not in body


@pytest.mark.parametrize(
    "headers",
    [
        {"Cookie": "__Host-bial_app=nothex"},
        {"Cookie": "__Host-bial_app=" + ALIAS + "extra"},
        {"Cookie": "__Host-bial_app=../../etc/passwd"},
        # A container name was never a key a browser may hold.
        {"Cookie": f"__Host-bial_app={SBX_KEY}"},
        {"Referer": f"https://{APPS_HOSTNAME}/a/{SBX_KEY}/"},
        {"Referer": "https://evil.example/a/" + ALIAS + "/"},
        {"Referer": f"https://{APPS_HOSTNAME}/a/NOTHEX/"},
    ],
)
def test_a_signal_that_is_not_the_exact_key_shape_is_refused(
    router: Router, headers: dict[str, str]
) -> None:
    """Both signals are browser-supplied and both reach a DNS lookup from this container's
    network position, so an unvalidated one is a request-forgery primitive rather than merely
    a bad route. The `evil.example` case is why the Referer pattern is anchored to the apps
    hostname and not to `[^/]+`."""
    status, _, body = router.request("/api/items", headers=headers)
    assert status == 404
    assert "REQ=" not in body


def test_a_signal_naming_a_vanished_app_is_404_not_502(router: Router) -> None:
    status, _, body = router.request(
        "/api/items", headers={"Cookie": f"__Host-bial_app={GHOST_ALIAS}"}
    )
    assert status == 404
    assert UNKNOWN_ALIAS not in body


# --------------------------------------------------------------------------------------
# the fallback cookie
# --------------------------------------------------------------------------------------


def test_top_level_navigation_sets_exactly_one_correctly_attributed_cookie(
    router: Router,
) -> None:
    """No `Domain`, so it stays host-only to the apps hostname and never reaches the portal.
    Plain `SameSite=Lax` works inside the portal's iframe only because the two hostnames share
    the registrable domain; if the portal ever moves, this silently stops working in the frame
    and `SameSite=None; Secure` becomes required."""
    # A REAL BROWSER SENDS BOTH `Sec-Fetch-*` HEADERS. `none` is what a typed address or a
    # bookmark carries — the ordinary way somebody opens a link a colleague sent them.
    _, headers, _ = router.request(
        f"/a/{ALIAS}/",
        headers={
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Site": "none",
        },
    )
    assert headers["__set_cookie_count"] == "1"
    cookie = headers["set-cookie"]
    assert cookie.startswith(f"__Host-bial_app={ALIAS};")
    assert "Path=/" in cookie
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Domain" not in cookie


def test_cookie_is_also_set_when_the_app_loads_inside_the_portal_iframe(
    router: Router,
) -> None:
    """The COMMON case, not the top-level one. Testing only a document navigation would pass
    while every preview in the cockpit failed to get a fallback cookie at all."""
    # The portal and the apps host share the registrable domain, so framing one from the other
    # is `same-site` — which is precisely why the cookie's plain `SameSite=Lax` works in the
    # frame at all, and why `same-site` has to be on the allowed list here.
    _, headers, _ = router.request(
        f"/a/{ALIAS}/",
        headers={
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "iframe",
            "Sec-Fetch-Site": "same-site",
        },
    )
    assert headers["__set_cookie_count"] == "1"
    assert headers["set-cookie"].startswith(f"__Host-bial_app={ALIAS};")


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image", "Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"},
        {},  # a client that sends no Sec-Fetch-* headers at all
        # ★ THE CROSS-SITE NAVIGATION. `Sec-Fetch-Mode: navigate` ALONE admits this — a link or
        # a `window.open` from any page on the internet — so an attacker who gets one click could
        # otherwise plant the routing cookie and repoint every later keyless call the victim's
        # own app makes. This is the case the second signal exists for.
        {
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Site": "cross-site",
        },
    ],
)
def test_a_subresource_request_never_writes_the_cookie(
    router: Router, headers: dict[str, str]
) -> None:
    """THE MUTANT THAT MUST FAIL. Without the navigate gate, any page anywhere forces a write
    with a single `<img src="https://<apps-host>/a/<their-key>/x">`, and every subsequent
    keyless call from the victim's own app is silently redirected to the attacker's container,
    carrying whatever that app sends. Since apps hold their own databases, that is a write to
    someone else's data."""
    _, got, _ = router.request(f"/a/{ALIAS}/logo.png", headers=headers)
    assert got["__set_cookie_count"] == "0"
    assert "set-cookie" not in got


def test_the_keyless_arm_never_writes_the_cookie(router: Router) -> None:
    """Only the keyed arm knows a key that came from the ADDRESS. Letting the keyless arm
    write one would let a resolved-from-cookie request re-affirm its own guess forever."""
    _, got, _ = router.request(
        "/page",
        headers={"Cookie": f"__Host-bial_app={ALIAS}", "Sec-Fetch-Mode": "navigate"},
    )
    assert got["__set_cookie_count"] == "0"


# --------------------------------------------------------------------------------------
# The boot guard. A malformed input must be a REFUSED BOOT, not a config that loads.
# --------------------------------------------------------------------------------------

_GOOD_ENV = {
    "PORT": "8080",
    "DNS_RESOLVER": "127.0.0.11",
    "BACKEND_URL": "http://backend:8000",
    "APPS_DOMAIN": APPS_DOMAIN,
    "APPS_HOSTNAME": APPS_HOSTNAME,
    "PORTAL_ORIGIN": PORTAL_ORIGIN,
    "INTERNAL_ROUTE_TOKEN": ROUTE_TOKEN,
}


@pytest.mark.parametrize(
    ("override", "expect"),
    [
        ({"APPS_HOSTNAME": ""}, "APPS_HOSTNAME is unset or empty"),
        ({"APPS_HOSTNAME": "https://apps.bial.test"}, "must be a bare hostname"),
        ({"APPS_HOSTNAME": "*.bial.test"}, "must be a bare hostname"),
        ({"APPS_HOSTNAME": "apps.bial.test/"}, "must be a bare hostname"),
        ({"APPS_HOSTNAME": "apps.bial.test:8080"}, "must be a bare hostname"),
        ({"APPS_DOMAIN": ""}, "APPS_DOMAIN is unset or empty"),
        ({"APPS_DOMAIN": "*.bial-apps.test"}, "must be a bare domain"),
        ({"APPS_DOMAIN": "https://bial-apps.test"}, "must be a bare domain"),
        ({"APPS_DOMAIN": ".bial-apps.test"}, "must be a bare domain"),
        ({"PORTAL_ORIGIN": ""}, "PORTAL_ORIGIN is unset or empty"),
        ({"PORTAL_ORIGIN": "https://portal.bial.test/"}, "must have no trailing slash"),
        ({"PORTAL_ORIGIN": "portal.bial.test"}, "must start with https:// or http://"),
        ({"PORTAL_ORIGIN": "https://portal.bial.test/app"}, "must have no path"),
        ({"DNS_RESOLVER": ""}, "DNS_RESOLVER is unset or empty"),
        ({"BACKEND_URL": "http://backend:8000/"}, "must have no path or trailing slash"),
        ({"INTERNAL_ROUTE_TOKEN": ""}, "INTERNAL_ROUTE_TOKEN is unset or empty"),
        ({"INTERNAL_ROUTE_TOKEN": "x" * 31}, "must be at least 32 characters"),
        ({"INTERNAL_ROUTE_TOKEN": "a;b" + "x" * 40}, "must contain only letters, digits"),
    ],
)
def test_container_refuses_to_boot_on_a_missing_or_malformed_input(
    images: None, docker_network: str, override: dict[str, str], expect: str
) -> None:
    """PRESENCE IS NOT THE BAR. Every value rejected here produces a config that LOADS and
    misroutes rather than one that breaks: a `*.` prefix or a scheme makes the apps block never
    match the forwarded Host, so every app request is served the portal's index.html instead of
    the app — which looks like "the app renders the portal", not like a routing error."""
    env = {**_GOOD_ENV, **override}
    proc = boot_router(env, network=docker_network)
    assert proc.stderr != b"__STAYED_UP__", f"container booted with {override!r}"
    assert proc.returncode != 0
    assert expect in proc.stderr.decode(), proc.stderr.decode()[-2000:]


def test_the_good_environment_actually_boots(images: None, docker_network: str) -> None:
    """The guard tests above are only meaningful if the same environment minus the override
    gets through. Without this, a guard that rejected everything would pass all of them."""
    proc = boot_router(_GOOD_ENV, network=docker_network)
    assert proc.stderr == b"__STAYED_UP__", proc.stderr.decode()[-2000:]


# --------------------------------------------------------------------------------------
# The operator's only way to tell the two 404s apart
# --------------------------------------------------------------------------------------


def test_the_access_log_separates_no_such_app_from_a_dead_app(
    stub_apps: None, docker_network: str
) -> None:
    """The router answers a flat 404 for BOTH an unknown key and a dead app, since naming the
    upstream to a browser would disclose the environment's naming convention. The log is the
    ONLY place an operator can tell them apart, and an incident walks someone straight to it,
    so it is pinned.

    The discriminating field is `$upstream_addr`, NOT `$upstream_status`: a refused connect
    reports `upstream_status=502` even though nothing ever answered, so only a resolved
    address separates them.
    """
    import subprocess
    import uuid as _uuid

    ghost = GHOST_ALIAS  # its container is named by the lookup and never given a DNS alias
    listening_elsewhere = "sbx-" + "1111111111111111111111111111"  # DEAD_ALIAS's container

    # A container that RESOLVES but is not serving on 443 — "the app died", not "no such app".
    dead = f"dead-{_uuid.uuid4().hex[:8]}"
    _run(
        ["docker", "run", "-d", "--name", dead, "--network", docker_network,
         "--network-alias", f"{listening_elsewhere}.{APPS_DOMAIN}", "nginx:alpine-slim"],
        timeout=120,
    )  # fmt: skip
    port = _free_port()
    router = f"router-log-{_uuid.uuid4().hex[:8]}"
    _run(
        ["docker", "run", "-d", "--name", router, "--network", docker_network,
         "-p", f"127.0.0.1:{port}:8080",
         "-e", "PORT=8080", "-e", "DNS_RESOLVER=127.0.0.11",
         "-e", f"BACKEND_URL=https://{BACKEND_ALIAS}",
         "-e", f"APPS_DOMAIN={APPS_DOMAIN}", "-e", f"APPS_HOSTNAME={APPS_HOSTNAME}",
         "-e", f"PORTAL_ORIGIN={PORTAL_ORIGIN}",
         "-e", f"INTERNAL_ROUTE_TOKEN={ROUTE_TOKEN}", ROUTER_IMAGE],
        timeout=120,
    )  # fmt: skip
    try:
        r = Router(port=port, container=router)
        _wait_for_router(r, router)
        assert r.request(f"/a/{ghost}/")[0] == 404
        assert r.request(f"/a/{DEAD_ALIAS}/")[0] == 404
        logs = subprocess.run(
            ["docker", "logs", router], capture_output=True, timeout=60
        ).stdout.decode()
    finally:
        _run(["docker", "rm", "-f", router, dead], timeout=90)

    ghost_line = next(ln for ln in logs.splitlines() if ghost in ln)
    dead_line = next(ln for ln in logs.splitlines() if DEAD_ALIAS in ln)
    assert "upstream=-" in ghost_line, ghost_line
    assert "upstream=-" not in dead_line, dead_line
    assert ":443" in dead_line, dead_line
    # And the field that looks like it should discriminate does not — pinned so nobody
    # "simplifies" the log format down to it.
    assert "upstream_status=502" in dead_line, dead_line


def test_a_dead_apps_404_does_not_plant_a_routing_cookie(router: Router) -> None:
    """A navigation to an app that no longer exists must not pin the browser to it.

    The cookie is the fallback that resolves EVERY later keyless request from this browser. If a
    stale link planted one, the person would be routed to a dead container for the rest of the
    session — including from a different app they opened afterwards, since the cookie is
    host-wide. The keyed location's `add_header` does not survive the `error_page` hop into
    `@app_gone`, which is what makes this hold; it is asserted rather than assumed because that
    is a property of nginx's header inheritance, not of anything written here.
    """
    status, headers, _ = router.request(
        f"/a/{UNKNOWN_ALIAS}/",
        headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
    )
    assert status == 404
    assert headers["__set_cookie_count"] == "0"
    assert "set-cookie" not in headers


# --------------------------------------------------------------------------------------
# what the edge discloses, and what it never serves
# --------------------------------------------------------------------------------------

_EDGE_VALUES = {
    "x-frame-options": "SAMEORIGIN",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "strict-transport-security": "max-age=63072000; includeSubDomains",
}


@pytest.mark.parametrize("target", ["/api/projects", "/api/v1/auth/callback"])
def test_a_proxied_response_carries_each_security_header_once_with_the_edges_value(
    router: Router, target: str
) -> None:
    """The stub backend sends its own copy of all four. One must reach the browser."""
    status, headers, body = router.request(target, host="portal.bial.test")
    assert status == 200
    assert "REQ=" in body
    for name, value in _EDGE_VALUES.items():
        assert headers[f"__count:{name}"] == "1", name
        assert headers[name] == value


@pytest.mark.parametrize(
    ("target", "document"),
    [("/", True), ("/index.html", True), ("/projects/123", True), ("/assets/app.js", False)],
)
def test_every_portal_page_carries_hsts_and_only_the_document_the_full_policy(
    router: Router, target: str, document: bool
) -> None:
    """Every SPA route reaches `= /index.html` by internal redirect, so it gets the document's
    policy; an asset keeps the framing one."""
    status, headers, _ = router.request(target, host="portal.bial.test")
    assert status == 200
    assert headers["strict-transport-security"] == _EDGE_VALUES["strict-transport-security"]
    assert ("default-src 'self'" in headers["content-security-policy"]) is document


def test_the_sign_in_callback_keeps_the_framing_policy(router: Router) -> None:
    """The callback brings its own nonce policy, and a script-src from the edge would block it."""
    _, headers, _ = router.request("/api/v1/auth/callback", host="portal.bial.test")
    assert "script-src" not in headers["content-security-policy"]


@pytest.mark.parametrize("host", ["portal.bial.test", APPS_HOSTNAME])
def test_neither_site_names_its_version(router: Router, host: str) -> None:
    _, headers, _ = router.request("/", host=host)
    assert headers["server"] == "nginx"


def test_an_app_response_does_not_name_the_software_behind_it(router: Router) -> None:
    status, headers, body = router.request(f"/a/{ALIAS}/")
    assert status == 200
    assert "REQ=" in body
    assert "via" not in headers
    assert "x-powered-by" not in headers


@pytest.mark.parametrize(
    "target",
    [
        f"/a/{ALIAS}/_next/static/chunks/main.js.map",
        f"/a/{PUB_KEY}/_next/static/chunks/main.js.map",
        f"/a/{ALIAS}/__nextjs_source-map?filename=x",
        f"/a/{SHR_ALIAS}/__nextjs_restart_dev",
        f"/a/{ALIAS}/_next/mcp",
        f"/a/{PUB_KEY}/_next/development/request-insights",
    ],
)
def test_next_source_and_dev_endpoints_never_reach_an_app(router: Router, target: str) -> None:
    status, _, body = router.request(target)
    assert status == 404
    assert "REQ=" not in body


@pytest.mark.parametrize("target", ["/__nextjs_original-stack-frames", "/_next/mcp"])
def test_a_root_relative_dev_endpoint_never_reaches_an_app_either(
    router: Router, target: str
) -> None:
    """Next's dev client asks for some of these with no key in the path; the keyless arm would
    otherwise resolve the app from the Referer and forward it."""
    referer = {"Referer": f"https://{APPS_HOSTNAME}/a/{ALIAS}/"}
    status, _, body = router.request(
        target,
        method="POST",
        headers={**referer, "Content-Type": "application/json"},
        body=b"{}",
    )
    assert status == 404
    assert "REQ=" not in body

    status, _, body = router.request("/api/items", headers=referer)
    assert status == 200
    assert _target(body) == f"/a/{ALIAS}/api/items"


@pytest.mark.parametrize("route", ["data/route.map", "data/__nextjs_notes", "docs/_next/x.map"])
def test_an_apps_own_routes_below_its_root_still_reach_it(router: Router, route: str) -> None:
    status, _, body = router.request(f"/a/{PUB_KEY}/{route}")
    assert status == 200
    assert _target(body) == f"/a/{PUB_KEY}/{route}"


# --------------------------------------------------------------------------------------
# the alias lookup
# --------------------------------------------------------------------------------------


def test_two_aliases_each_reach_their_own_container_keyed_and_keyless(router: Router) -> None:
    """★ THE MISROUTE GUARD. A variable that outlives the lookup — a server-level `set` — gives
    every alias the same cache key, and every app is then routed to the container looked up first.
    Alternated, so a request that merely inherits the previous one's answer is caught."""
    for _ in range(2):
        for alias, container in [(ALIAS, SBX_KEY), (OTHER_ALIAS, OTHER_SBX_KEY)]:
            _, _, keyed = router.request(f"/a/{alias}/page")
            assert _host(keyed) == f"{container}.{APPS_DOMAIN}"
            referer = {"Referer": f"https://{APPS_HOSTNAME}/a/{alias}/page"}
            _, _, by_referer = router.request("/api/items", headers=referer)
            assert (_host(by_referer), _target(by_referer)) == (
                f"{container}.{APPS_DOMAIN}",
                f"/a/{alias}/api/items",
            )
            _, _, by_cookie = router.request(
                "/api/items", headers={"Cookie": f"__Host-bial_app={alias}"}
            )
            assert _host(by_cookie) == f"{container}.{APPS_DOMAIN}"


def _log_lines(router: Router, needle: str) -> list[str]:
    import subprocess

    logs = subprocess.run(
        ["docker", "logs", router.container], capture_output=True, timeout=60
    ).stdout.decode()
    return [ln for ln in logs.splitlines() if needle in ln]


def test_the_lookup_is_cached_and_a_published_app_never_makes_one(router: Router) -> None:
    """The first request for an alias asks the backend; the next, within the window, does not. The
    cache silently does nothing under `proxy_buffering off` or the backend's `no-store`, so only
    the log's `alias_cache` field says whether it is working."""
    assert router.request(f"/a/{CACHE_ALIAS}/one")[0] == 200
    assert router.request(f"/a/{CACHE_ALIAS}/two")[0] == 200
    assert router.request(f"/a/{PUB_KEY}/one")[0] == 200
    [first, second] = _log_lines(router, f"/a/{CACHE_ALIAS}/")
    assert "alias_cache=MISS" in first and "alias_cache=HIT" in second
    assert "alias_cache=-" in _log_lines(router, f"/a/{PUB_KEY}/one")[0]


@pytest.mark.parametrize("refused", [SBX_KEY, SHR_KEY, UNKNOWN_ALIAS])
def test_a_container_name_or_an_unknown_alias_is_not_available_while_an_alias_serves(
    router: Router, refused: str
) -> None:
    """From deploy a container name in the address is refused, so a published link's `pub-` cannot
    be swapped for the owner's `sbx-`. Paired with a live alias: refusal alone would pass on a
    router that served nothing."""
    status, _, body = router.request(f"/a/{refused}/")
    assert (status, "REQ=" in body, "not available" in body.lower()) == (404, False, True)
    assert router.request(f"/a/{ALIAS}/")[0] == 200


@pytest.mark.parametrize("code", [401, 403, 404, 500])
def test_an_apps_own_error_reaches_the_browser_unchanged(router: Router, code: int) -> None:
    status, _, body = router.request(f"/a/{ALIAS}/__status/{code}")
    assert status == code
    assert body.startswith("REQ=")


def test_the_internal_lookup_is_unreachable_from_a_browser_and_from_an_app(router: Router) -> None:
    status, _, body = router.request("/__bial_route")
    assert (status, "REQ=" in body) == (404, False)
    # The app's own `X-Accel-Redirect` is not followed: the app's page is what comes back.
    status, _, body = router.request(f"/a/{ALIAS}/__accel")
    assert (status, body.startswith("REQ=")) == (200, True)


def test_an_app_is_never_sent_the_routing_cookie_and_keeps_its_own(router: Router) -> None:
    for cookie in [
        f"__Host-bial_app={ALIAS}; theme=dark",
        f"theme=dark; __Host-bial_app={ALIAS}; lang=en",
        f"theme=dark; __Host-bial_app={ALIAS}",
        # The name this cookie used to carry, holding a container's name, until browsers drop it.
        f"bial_app={SBX_KEY}; theme=dark",
    ]:
        _, _, body = router.request(f"/a/{OTHER_ALIAS}/", headers={"Cookie": cookie})
        sent = _fields(body)["CK"]
        assert "bial_app" not in sent and ALIAS not in sent and SBX_KEY not in sent
        assert "theme=dark" in sent
    _, _, body = router.request(
        f"/a/{OTHER_ALIAS}/", headers={"Cookie": f"__Host-bial_app={ALIAS}"}
    )
    assert _fields(body)["CK"] == ""


def test_an_apps_redirect_carries_the_prefix_once_and_never_the_container(router: Router) -> None:
    status, headers, _ = router.request(f"/a/{ALIAS}/__redirect")
    assert status == 302
    # nginx makes a relative replacement absolute with the request's own Host, so only the tail
    # and the absence of the container are asserted.
    location = headers["location"]
    assert location.endswith(f"/a/{ALIAS}/next") and location.count("/a/") == 1
    assert APPS_DOMAIN not in location and SBX_KEY not in location


def test_a_backend_that_cannot_answer_reads_as_app_not_available_and_spares_published_apps(
    router_without_backend: Router,
) -> None:
    status, _, body = router_without_backend.request(f"/a/{ALIAS}/")
    assert (status, "not available" in body.lower()) == (404, True)
    status, _, body = router_without_backend.request(
        "/api/items", headers={"Referer": f"https://{APPS_HOSTNAME}/a/{ALIAS}/"}
    )
    assert (status, "REQ=" in body) == (404, False)
    assert router_without_backend.request(f"/a/{PUB_KEY}/")[0] == 200


# --------------------------------------------------------------------------------------
# the preview pass
# --------------------------------------------------------------------------------------

_NAVIGATION = {
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Site": "none",
}
_FETCH = {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Site": "same-origin"}
_HANDOFF = f"{PORTAL_ORIGIN}/api/v1/auth/preview-handoff/"
_BINDING = "ab" * 16
_TICKET = "tIcKeT_0123456789-abcdefghijklmnopqrstuvwxy"


@pytest.mark.parametrize(
    ("target", "cookie", "rest"),
    [
        (f"/a/{ALIAS}/page?tab=1", None, f"{ALIAS}/a/{ALIAS}/page?tab=1"),
        ("/reports?tab=1", f"__Host-bial_app={ALIAS}", f"{ALIAS}/reports?tab=1"),
    ],
)
def test_a_navigation_without_the_pass_goes_to_the_hand_over_with_one_binding_cookie(
    router: Router, target: str, cookie: str | None, rest: str
) -> None:
    """Exactly one cookie: the routing cookie is never written for an app this person was
    refused."""
    headers = {**_NAVIGATION, **({"Cookie": cookie} if cookie else {})}
    status, got, _ = router.request(target, headers=headers, preview_pass=None)
    assert status == 302
    assert got["location"].startswith(_HANDOFF)
    binding, _, tail = got["location"].removeprefix(_HANDOFF).partition("/")
    assert re.fullmatch(r"[0-9a-f]{32}", binding)
    assert tail == rest
    assert got["__set_cookie_count"] == "1"
    assert got["set-cookie"] == (
        f"__Host-bial_handoff={binding}; Path=/; Max-Age=600; Secure; HttpOnly; SameSite=Lax"
    )
    assert got["cache-control"] == "no-store"


def test_a_fetch_without_the_pass_is_not_available_and_never_bounced(router: Router) -> None:
    """A fetch cannot follow a sign-in, so it gets the page rather than a portal address."""
    status, got, body = router.request(f"/a/{ALIAS}/api/items", headers=_FETCH, preview_pass=None)
    assert (status, "REQ=" in body, "not available" in body.lower()) == (404, False, True)
    assert (got["__set_cookie_count"], "location" in got) == ("0", False)
    status, _, body = router.request(f"/a/{ALIAS}/api/items", headers=_FETCH)
    assert (status, _target(body)) == (200, f"/a/{ALIAS}/api/items")


def test_a_websocket_without_the_pass_is_refused_and_never_bounced(router: Router) -> None:
    """Chromium sends no Fetch Metadata on a handshake, so only `Upgrade` keeps it from
    bouncing."""
    status, head = router.websocket(f"/a/{ALIAS}/_next/webpack-hmr", preview_pass=None)
    assert status == 404, head
    assert "location:" not in head.lower()
    status, head = router.websocket(f"/a/{ALIAS}/_next/webpack-hmr")
    assert status == 101, head


@pytest.mark.parametrize(
    "cookie",
    [
        f"__Host-bial_pass={PASS}; theme=dark; __Host-bial_handoff={_BINDING}; lang=en; "
        f"__Host-bial_app={ALIAS}",
        f"__Host-bial_app={ALIAS}; theme=dark; __Host-bial_handoff={_BINDING}; lang=en; "
        f"__Host-bial_pass={PASS}",
    ],
)
@pytest.mark.parametrize(
    ("target", "headers"),
    [
        (f"/a/{ALIAS}/", {}),
        (f"/a/{PUB_KEY}/", {}),
        ("/api/items", {"Referer": f"https://{APPS_HOSTNAME}/a/{ALIAS}/"}),
    ],
)
def test_no_app_is_sent_the_edges_own_cookies_and_each_keeps_its_own(
    router: Router, cookie: str, target: str, headers: dict[str, str]
) -> None:
    """An app that read the pass could open its holder's previews from anywhere."""
    status, _, body = router.request(
        target, headers={**headers, "Cookie": cookie}, preview_pass=None
    )
    assert status == 200
    assert _fields(body)["CK"] == "theme=dark; lang=en"


def test_a_found_answer_is_cached_per_pass_and_never_served_to_another(router: Router) -> None:
    """Cached by the alias alone, the owner's yes would open the preview to anyone for a
    minute."""
    assert router.request(f"/a/{OWNER_CACHE_ALIAS}/one", headers=_FETCH)[0] == 200
    assert router.request(f"/a/{OWNER_CACHE_ALIAS}/two", headers=_FETCH)[0] == 200
    status, _, body = router.request(
        f"/a/{OWNER_CACHE_ALIAS}/three",
        headers={**_FETCH, "Cookie": f"__Host-bial_pass={OTHER_PASS}"},
        preview_pass=None,
    )
    assert (status, "REQ=" in body) == (404, False)
    one, two, three = _log_lines(router, f"/a/{OWNER_CACHE_ALIAS}/")
    assert "alias_cache=MISS" in one and "denied=-" in one
    assert "alias_cache=HIT" in two and "denied=-" in two
    assert "alias_cache=MISS" in three and "denied=1" in three
    # The flag, never the binding: the only 32-hex value on the line is the alias.
    assert set(re.findall(r"[0-9a-f]{32}", three)) == {OWNER_CACHE_ALIAS}


def test_the_entry_route_forwards_only_the_ticket_and_binding_and_never_logs_the_ticket(
    router: Router, router_without_backend: Router
) -> None:
    status, got, body = router.request(
        f"/__bial_enter?ticket={_TICKET}",
        headers={**_NAVIGATION, "Cookie": f"theme=dark; __Host-bial_handoff={_BINDING}"},
    )
    assert status == 302
    assert got["location"] == "https://apps.bial.test/a/entered/"
    assert got["__set_cookies"].split("\n") == [
        "__Host-bial_pass=entered-pass; Path=/; Max-Age=43200; Secure; HttpOnly; SameSite=Lax",
        "__Host-bial_handoff=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax",
    ]
    fields = _fields(body)
    assert _target(body) == "/internal/preview-pass"
    assert (fields["TICKET"], fields["BINDING"], fields["TOKEN_OK"]) == (_TICKET, _BINDING, "1")
    assert (fields["HOST"], fields["CK"]) == (BACKEND_ALIAS, "")
    lines = _log_lines(router, "/__bial_enter")
    assert lines
    assert all("ticket" not in ln and _TICKET not in ln for ln in lines)

    # A backend that cannot answer ends on the gone page, still without the ticket in the log.
    status, got, _ = router_without_backend.request(f"/__bial_enter?ticket={_TICKET}")
    assert (status, got.get("location")) == (302, f"https://{APPS_HOSTNAME}/__bial_gone")
    lines = _log_lines(router_without_backend, "/__bial_enter")
    assert lines
    assert all("ticket" not in ln and _TICKET not in ln for ln in lines)


@pytest.mark.parametrize(
    ("target", "headers"),
    [("/sw.js", {"Referer": f"https://{APPS_HOSTNAME}/a/{ALIAS}/"}), (f"/a/{ALIAS}", {})],
)
def test_a_worker_script_outside_an_apps_own_address_is_refused(
    router: Router, target: str, headers: dict[str, str]
) -> None:
    """Registered from the root it would see every app and the entry route; from `/a/<key>`,
    every app."""
    worker = {"Service-Worker": "script", "Sec-Fetch-Dest": "serviceworker", **headers}
    status, _, body = router.request(target, headers=worker)
    assert (status, "REQ=" in body) == (404, False)
    status, _, body = router.request(target, headers=headers)
    assert (status, body.startswith("REQ=")) == (200, True)


def test_a_worker_script_inside_an_apps_address_is_served_without_a_wider_scope(
    router: Router,
) -> None:
    status, got, body = router.request(f"/a/{ALIAS}/sw.js", headers={"Service-Worker": "script"})
    assert (status, _target(body)) == (200, f"/a/{ALIAS}/sw.js")
    assert "service-worker-allowed" not in got
    # The stub does send it: the portal site, which does not hide it, passes it through.
    _, got, _ = router.request("/apps/sw.js", host="portal.bial.test")
    assert got["service-worker-allowed"] == "/"


@pytest.mark.parametrize(("dest", "words"), [("document", "not available"), ("iframe", "running")])
def test_the_gone_page_never_bounces_and_serves_the_readers_page(
    router: Router, dest: str, words: str
) -> None:
    status, got, body = router.request(
        "/__bial_gone", headers={**_NAVIGATION, "Sec-Fetch-Dest": dest}, preview_pass=None
    )
    assert (status, "location" in got, got["__set_cookie_count"]) == (404, False, "0")
    assert words in body.lower()
    assert (PORTAL_ORIGIN in body) is (dest == "document")
