"""Behavioural tests for the portal edge's apps router (plan units U2 and U3).

Every test here drives a real nginx with the real `portal/nginx.conf` against a stub app
container. The sibling Vitest suite pins the config's shape; this pins what it DOES.

The distinction is load-bearing rather than tidy. Three of the failures this router is most
exposed to — a WebSocket upgrade answered as an ordinary request, a `proxy_pass` URI collapsing
every path to `/`, and a keyless request arriving at the right container with the wrong path —
all leave `nginx -t` green and all pass a structural assertion.
"""

from __future__ import annotations

import pytest
from _router import (
    APPS_DOMAIN,
    APPS_HOSTNAME,
    GHOST_KEY,
    OTHER_SBX_KEY,
    PORTAL_ORIGIN,
    PUB_KEY,
    ROUTER_IMAGE,
    SBX_KEY,
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
# U2 — the keyed arm
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


@pytest.mark.parametrize("key", [SBX_KEY, PUB_KEY])
def test_keyed_request_reaches_its_app_with_the_prefix_intact(router: Router, key: str) -> None:
    """AE1. The prefix is real end to end: the router forwards it unchanged rather than
    stripping it, because the app is configured to live at it."""
    status, _, body = router.request(f"/a/{key}/dashboard?tab=1")
    assert status == 200
    assert _target(body) == f"/a/{key}/dashboard?tab=1"
    assert _host(body) == f"{key}.{APPS_DOMAIN}"


def test_upstream_host_is_the_container_not_the_browser_host(router: Router) -> None:
    """Azure Container Apps routes by Host. Forwarding the browser's host would make its front
    door reject the hop before the app ever runs; the browser-facing name still travels in
    X-Forwarded-Host for any absolute-URL reconstruction the app needs."""
    _, _, body = router.request(f"/a/{SBX_KEY}/")
    fields = _fields(body)
    assert fields["HOST"] == f"{SBX_KEY}.{APPS_DOMAIN}"
    assert fields["XFH"] == APPS_HOSTNAME


def test_post_body_and_method_survive_the_hop(router: Router) -> None:
    """R6 — method and body are preserved, not just the path."""
    status, _, body = router.request(
        f"/a/{SBX_KEY}/submit",
        method="POST",
        headers={"Content-Type": "text/plain", "Content-Length": "5"},
        body=b"hello",
    )
    assert status == 200
    assert body.startswith("REQ=POST|")
    assert _target(body) == f"/a/{SBX_KEY}/submit"


@pytest.mark.parametrize(
    "bad",
    [
        "sbx-1a2b3c4d5e6f70819a2b3c4d5e6",  # 27 hex
        "sbx-1a2b3c4d5e6f70819a2b3c4d5e6f0",  # 29 hex
        "sbx-1A2B3C4D5E6F70819A2B3C4D5E6F",  # uppercase
        "sbx-1a2b3c4d5e6f70819a2b3c4d5e6g",  # non-hex
        "dev-1a2b3c4d5e6f70819a2b3c4d5e6f",  # wrong prefix
    ],
)
def test_a_malformed_key_is_404_and_never_another_app(router: Router, bad: str) -> None:
    """R8. The narrow match is what turns a mistyped key into a 404 rather than a DNS lookup
    for an attacker-named host — and, just as importantly, it must not fall through to the
    keyless arm, which would resolve it from a cookie and serve SOMEONE ELSE'S app under the
    address the person typed."""
    status, _, body = router.request(f"/a/{bad}/", headers={"Cookie": f"bial_app={SBX_KEY}"})
    assert status == 404
    assert "REQ=" not in body


def test_dot_dot_normalizes_before_location_matching(router: Router) -> None:
    """`/a/<key>/../` collapses to `/a/` BEFORE nginx picks a location, so it lands on the
    malformed-key arm and reaches no upstream at all. Only a running nginx can show that."""
    status, _, body = router.request(f"/a/{SBX_KEY}/../")
    assert status == 404
    assert "REQ=" not in body


def test_unknown_but_wellformed_key_is_404_and_names_no_upstream(router: Router) -> None:
    """AE3. The router holds no registry, so an unknown key fails as a DNS MISS. Left alone
    that is a 502 whose body and headers can name the composed upstream and hand anyone who
    reaches the gateway the environment's naming convention."""
    status, headers, body = router.request(f"/a/{GHOST_KEY}/")
    assert status == 404
    assert APPS_DOMAIN not in body
    assert GHOST_KEY not in body
    joined = " ".join(headers.values())
    assert APPS_DOMAIN not in joined


def test_the_404_page_has_a_body_and_a_way_back(router: Router) -> None:
    """The person who reaches this is a BIAL employee who followed a link, not a developer. A
    stale key, a reaped sandbox and a typo all land here."""
    status, headers, body = router.request(f"/a/{GHOST_KEY}/")
    assert status == 404
    assert headers["content-type"].startswith("text/html")
    assert PORTAL_ORIGIN in body
    assert "not available" in body.lower()


@pytest.mark.parametrize("target", ["/_sup/health", "/_sup", f"/a/{SBX_KEY}/_sup/health"])
def test_supervisor_surface_is_refused_at_the_router(router: Router, target: str) -> None:
    """The supervisor is bearer-guarded downstream, but this plan claims as an invariant that
    it is unreachable from a browser, and an invariant should be enforced where it is claimed
    rather than depend on a check designed for a different threat."""
    status, _, body = router.request(target, headers={"Cookie": f"bial_app={SBX_KEY}"})
    assert status == 404
    assert "REQ=" not in body


def test_websocket_upgrade_is_answered_101_on_the_keyed_arm(router: Router) -> None:
    """AE6. Live reload rides this. Without `proxy_http_version 1.1` in THIS server block nginx
    defaults to HTTP/1.0 and answers the upgrade as an ordinary request — with the `Upgrade`
    header still forwarded, which is why only a real 101 proves anything."""
    status, head = router.websocket(f"/a/{SBX_KEY}/_next/webpack-hmr")
    assert status == 101, head
    assert "sec-websocket-accept" in head.lower()
    assert f"X-Stub-Target: /a/{SBX_KEY}/_next/webpack-hmr" in head


def test_apps_site_proxies_nothing_to_the_backend(router: Router) -> None:
    """A generated app must not be able to reach the control plane on its own origin. `/api/`
    on the apps host is an ordinary keyless app path, never the portal's backend route."""
    status, _, body = router.request("/api/v1/auth/me")
    assert status == 404
    assert "REQ=" not in body


# --------------------------------------------------------------------------------------
# U2 — the portal site must be unaffected (regression)
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
# U3 — the keyless arm
# --------------------------------------------------------------------------------------


def test_keyless_request_is_prefixed_not_merely_routed(router: Router) -> None:
    """AE2, and the one assertion this whole arm exists for.

    Under `basePath` Next gates every route behind the prefix, route handlers included. A
    request proxied to the right container as `/api/items` is answered with the framework's own
    404. Asserting the upstream host would pass against that broken behaviour; asserting the
    REQUEST LINE is what distinguishes a working fallback from a decorative one.
    """
    _, _, body = router.request(
        "/api/items", headers={"Referer": f"https://{APPS_HOSTNAME}/a/{SBX_KEY}/dashboard"}
    )
    assert _target(body) == f"/a/{SBX_KEY}/api/items"
    assert _host(body) == f"{SBX_KEY}.{APPS_DOMAIN}"


def test_keyless_query_string_survives_the_rewrite(router: Router) -> None:
    _, _, body = router.request(
        "/api/items?page=2&q=a+b",
        headers={"Referer": f"https://{APPS_HOSTNAME}/a/{SBX_KEY}/"},
    )
    assert _target(body) == f"/a/{SBX_KEY}/api/items?page=2&q=a+b"


def test_referer_beats_cookie_so_two_open_tabs_stay_correct(router: Router) -> None:
    """The failure this ordering prevents is not a broken image. A host-wide cookie is
    last-write-wins, so with two apps open one app's form post lands in the other app's
    database — and each app owns its own database."""
    _, _, body = router.request(
        "/api/items",
        headers={
            "Referer": f"https://{APPS_HOSTNAME}/a/{SBX_KEY}/page",
            "Cookie": f"bial_app={OTHER_SBX_KEY}",
        },
    )
    assert _target(body) == f"/a/{SBX_KEY}/api/items"
    assert _host(body) == f"{SBX_KEY}.{APPS_DOMAIN}"


def test_cookie_answers_a_top_level_navigation_with_no_referer(router: Router) -> None:
    _, _, body = router.request(
        "/reports",
        headers={"Cookie": f"bial_app={PUB_KEY}", "Sec-Fetch-Mode": "navigate"},
    )
    assert _target(body) == f"/a/{PUB_KEY}/reports"


@pytest.mark.parametrize(
    ("target", "headers"),
    [
        ("/favicon.ico", {}),  # the browser asks at the origin root regardless of the page
        ("/hero.png", {"Referer": f"https://{APPS_HOSTNAME}/a/{SBX_KEY}/styles.css"}),
        ("/logo.png", {"Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}),
    ],
)
def test_plain_html_and_browser_originated_requests_arrive_prefixed(
    router: Router, target: str, headers: dict[str, str]
) -> None:
    """`basePath` rewrites what the FRAMEWORK generates. It does not touch a plain `<img src>`,
    a CSS `url()`, or `/favicon.ico`, so this arm carries steady traffic rather than the
    occasional hand-written call."""
    _, _, body = router.request(target, headers={"Cookie": f"bial_app={SBX_KEY}", **headers})
    assert _target(body) == f"/a/{SBX_KEY}{target}"


def test_keyless_form_post_arrives_prefixed_with_its_body(router: Router) -> None:
    status, _, body = router.request(
        "/submit",
        method="POST",
        headers={
            "Referer": f"https://{APPS_HOSTNAME}/a/{SBX_KEY}/form",
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": "7",
        },
        body=b"a=1&b=2",
    )
    assert status == 200
    assert body.startswith("REQ=POST|")
    assert _target(body) == f"/a/{SBX_KEY}/submit"


def test_keyless_websocket_is_also_prefixed_and_upgraded(router: Router) -> None:
    status, head = router.websocket(
        "/_next/webpack-hmr",
        headers={"Referer": f"https://{APPS_HOSTNAME}/a/{SBX_KEY}/page"},
    )
    assert status == 101, head
    assert f"X-Stub-Target: /a/{SBX_KEY}/_next/webpack-hmr" in head


def test_no_signal_at_all_is_404_never_a_fallthrough(router: Router) -> None:
    status, _, body = router.request("/api/items")
    assert status == 404
    assert "REQ=" not in body


@pytest.mark.parametrize(
    "headers",
    [
        {"Cookie": "bial_app=sbx-nothex"},
        {"Cookie": "bial_app=" + SBX_KEY + "extra"},
        {"Cookie": "bial_app=../../etc/passwd"},
        {"Referer": "https://evil.example/a/" + SBX_KEY + "/"},
        {"Referer": f"https://{APPS_HOSTNAME}/a/sbx-NOTHEX/"},
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
    status, _, body = router.request("/api/items", headers={"Cookie": f"bial_app={GHOST_KEY}"})
    assert status == 404
    assert GHOST_KEY not in body


# --------------------------------------------------------------------------------------
# U3 — the fallback cookie
# --------------------------------------------------------------------------------------


def test_top_level_navigation_sets_exactly_one_correctly_attributed_cookie(
    router: Router,
) -> None:
    """No `Domain`, so it stays host-only to the apps hostname and never reaches the portal.
    Plain `SameSite=Lax` works inside the portal's iframe only because the two hostnames share
    the registrable domain; if the portal ever moves, this silently stops working in the frame
    and `SameSite=None; Secure` becomes required."""
    _, headers, _ = router.request(
        f"/a/{SBX_KEY}/", headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    )
    assert headers["__set_cookie_count"] == "1"
    cookie = headers["set-cookie"]
    assert cookie.startswith(f"bial_app={SBX_KEY};")
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
    _, headers, _ = router.request(
        f"/a/{SBX_KEY}/", headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"}
    )
    assert headers["__set_cookie_count"] == "1"
    assert headers["set-cookie"].startswith(f"bial_app={SBX_KEY};")


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"},
        {"Sec-Fetch-Mode": "cors"},
        {},  # a client that sends no Sec-Fetch-Mode at all
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
    _, got, _ = router.request(f"/a/{SBX_KEY}/logo.png", headers=headers)
    assert got["__set_cookie_count"] == "0"
    assert "set-cookie" not in got


def test_the_keyless_arm_never_writes_the_cookie(router: Router) -> None:
    """Only the keyed arm knows a key that came from the ADDRESS. Letting the keyless arm
    write one would let a resolved-from-cookie request re-affirm its own guess forever."""
    _, got, _ = router.request(
        "/page",
        headers={"Cookie": f"bial_app={SBX_KEY}", "Sec-Fetch-Mode": "navigate"},
    )
    assert got["__set_cookie_count"] == "0"


# --------------------------------------------------------------------------------------
# U2 — the boot guard. A malformed input must be a REFUSED BOOT, not a config that loads.
# --------------------------------------------------------------------------------------

_GOOD_ENV = {
    "PORT": "8080",
    "DNS_RESOLVER": "127.0.0.11",
    "BACKEND_URL": "http://backend:8000",
    "APPS_DOMAIN": APPS_DOMAIN,
    "APPS_HOSTNAME": APPS_HOSTNAME,
    "PORTAL_ORIGIN": PORTAL_ORIGIN,
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
# U2 — the operator's only way to tell the two 404s apart
# --------------------------------------------------------------------------------------


def test_the_access_log_separates_no_such_app_from_a_dead_app(
    images: None, docker_network: str
) -> None:
    """The router answers a flat 404 for BOTH an unknown key and a live app that stopped
    listening, because naming the upstream to a browser would disclose the environment's naming
    convention. That makes the log the ONLY place an operator can tell "the link is stale" from
    "the app crashed" — and DEPLOYMENT-FACTS.md now sends them here to do it, so it is pinned.

    The discriminating field is `$upstream_addr`, NOT `$upstream_status`. That is measured, and
    it is the opposite of the intuitive reading: a refused connect reports `upstream_status=502`
    even though nothing ever answered, so only the presence of a resolved address separates
    them.
    """
    import subprocess
    import uuid as _uuid

    ghost = "sbx-" + "deadbeefdeadbeefdeadbeefdead"  # never given a DNS alias
    listening_elsewhere = "sbx-" + "1111111111111111111111111111"

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
         "-e", "BACKEND_URL=http://backend-not-used:8000",
         "-e", f"APPS_DOMAIN={APPS_DOMAIN}", "-e", f"APPS_HOSTNAME={APPS_HOSTNAME}",
         "-e", f"PORTAL_ORIGIN={PORTAL_ORIGIN}", ROUTER_IMAGE],
        timeout=120,
    )  # fmt: skip
    try:
        r = Router(port=port, container=router)
        _wait_for_router(r, router)
        assert r.request(f"/a/{ghost}/")[0] == 404
        assert r.request(f"/a/{listening_elsewhere}/")[0] == 404
        logs = subprocess.run(
            ["docker", "logs", router], capture_output=True, timeout=60
        ).stdout.decode()
    finally:
        _run(["docker", "rm", "-f", router, dead], timeout=90)

    ghost_line = next(ln for ln in logs.splitlines() if ghost in ln)
    dead_line = next(ln for ln in logs.splitlines() if listening_elsewhere in ln)
    assert "upstream=-" in ghost_line, ghost_line
    assert "upstream=-" not in dead_line, dead_line
    assert ":443" in dead_line, dead_line
    # And the field that looks like it should discriminate does not — pinned so nobody
    # "simplifies" the log format down to it.
    assert "upstream_status=502" in dead_line, dead_line
