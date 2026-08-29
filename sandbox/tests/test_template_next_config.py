"""The golden template's Next config must EVALUATE to the right object, not merely read right.

WHY IT EXISTS. This file had no coverage at all, and it is the only place three separate
production properties are decided: the base path the platform routes to, the origin a Server
Action will accept, and the dev-origin glob whose narrowing once shipped a preview that returned
200 and never rendered. Every one of those fails SILENTLY — as a blank frame, a CSRF abort, or a
hydration stall — and none of them fails at build time.

WHY IT EVALUATES RATHER THAN GREPS, exactly as `test_caddyfile_adapts.py` asserts on Caddy's
ADAPTED JSON rather than on the Caddyfile text: a text assertion is satisfied by a comment, and
it breaks on any reformatting that changes nothing. Running the real evaluator and asserting on
the resulting object survives any spelling of the same config and cannot be fooled.

It is deliberately NOT an integration test. `sandbox/template` has no `node_modules` checked
out, and it does not need one: the file's only import is `import type`, which type-stripping
erases, so a throwaway official Node image evaluates it in about a second. Docker missing means
a clean skip, exactly like the rest of this harness.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

CONFIG = Path(__file__).resolve().parent.parent / "template" / "next.config.ts"

# The Node major the template pins in its own `engines` field. Type-stripping is what makes an
# unbuilt `.ts` config evaluable, and it is stable from Node 22 on.
NODE_IMAGE = "node:24-alpine"

# A real key shape: `/a/` plus the container app's own name (`sbx-`/`pub-` + 28 lowercase hex).
BASE_PATH = "/a/sbx-1a2b3c4d5e6f70819a2b3c4d5e6f"
APPS_HOSTNAME = "citizenapps.bialairport.com"


def _docker_available() -> bool:
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):  # fmt: skip  # ruff py314 strips parens
        return False
    return proc.returncode == 0


@pytest.fixture(scope="module", autouse=True)
def _needs_docker() -> None:
    if not _docker_available():
        pytest.skip("Docker is not available — template next.config.ts check skipped")


def _evaluate(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Evaluate the real template config under `env` and return the exported object."""
    args = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "-v",
        f"{CONFIG}:/w/next.config.ts:ro",
    ]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    args += [
        NODE_IMAGE, "node", "--experimental-strip-types", "-e",
        'import("/w/next.config.ts").then(m=>console.log(JSON.stringify(m.default)))',
    ]  # fmt: skip
    proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, (
        f"sandbox/template/next.config.ts does not evaluate. `next dev` reads this file at "
        f"start, so this is a sandbox that never serves.\n{proc.stderr[-3000:]}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_the_template_config_evaluates_at_all() -> None:
    assert isinstance(_evaluate(), dict)


def test_an_injected_base_path_becomes_basePath() -> None:  # noqa: N802 - names the Next key
    """R10. The platform assigns the path; the app's own code does not get a say."""
    cfg = _evaluate({"BIAL_BASE_PATH": BASE_PATH})
    assert cfg["basePath"] == BASE_PATH


def test_no_injected_base_path_leaves_the_app_at_the_root() -> None:
    """The local dev loop, and every existing test, must be unaffected. An ABSENT key, not an
    empty one: Next does not treat `basePath: ""` the same as no `basePath` everywhere."""
    cfg = _evaluate()
    assert "basePath" not in cfg


def test_the_apps_hostname_becomes_a_server_action_allowed_origin() -> None:
    """`allowedDevOrigins` does NOT cover Server Actions — they compare the browser `Origin`
    against the forwarded host and abort on a mismatch, which is guaranteed once traffic arrives
    through the router. Without this every form post in every generated app fails its CSRF
    check, and the harness steers app code toward Server Actions as a mainstream data path."""
    cfg = _evaluate({"BIAL_APPS_HOSTNAME": APPS_HOSTNAME})
    assert cfg["experimental"]["serverActions"]["allowedOrigins"] == [APPS_HOSTNAME]


def test_no_apps_hostname_declares_no_server_action_origins() -> None:
    cfg = _evaluate()
    assert "experimental" not in cfg


def test_both_injected_together_is_what_a_real_sandbox_gets() -> None:
    cfg = _evaluate({"BIAL_BASE_PATH": BASE_PATH, "BIAL_APPS_HOSTNAME": APPS_HOSTNAME})
    assert cfg["basePath"] == BASE_PATH
    assert cfg["experimental"]["serverActions"]["allowedOrigins"] == [APPS_HOSTNAME]


def test_base_path_carries_no_trailing_slash() -> None:
    """Measured, not stylistic. Next redirects `/<base>/` to `/<base>` with a 308, so a trailing
    slash would make the readiness probe read a redirect instead of the app — and Next rejects a
    `basePath` ending in `/` outright."""
    cfg = _evaluate({"BIAL_BASE_PATH": BASE_PATH})
    assert not cfg["basePath"].endswith("/")
    assert cfg["basePath"].startswith("/")


@pytest.mark.parametrize(
    "origin", ["**.bialairport.com", "**.azurecontainerapps.io", "127.0.0.1", "localhost"]
)
def test_dev_origins_keep_every_entry_and_the_double_star_glob(origin: str) -> None:
    """REGRESSION, and the sharpest one in this file. A real ACA FQDN is MULTI-LABEL and a
    single `*` does not span label dots, so narrowing `**.` to `*.` made `next dev` reject the
    HMR upgrade and hydration never ran — a preview that returned 200 and rendered nothing.
    `**.bialairport.com` is matched at the apex deliberately: the exact subdomain is BIAL's to
    choose, and pinning a guess would bake a wrong value into the image."""
    assert origin in _evaluate()["allowedDevOrigins"]


def test_type_errors_stay_hard() -> None:
    """The self-heal loop depends on the app's own type errors being real failures."""
    assert _evaluate()["typescript"]["ignoreBuildErrors"] is False


def test_the_dev_indicator_badge_stays_off() -> None:
    """It is shown to a non-technical BIAL employee who reads its red counter as "my app is
    broken" and cannot act on it. The errors themselves still reach the platform by another
    path, so this costs no signal."""
    assert _evaluate()["devIndicators"] is False
