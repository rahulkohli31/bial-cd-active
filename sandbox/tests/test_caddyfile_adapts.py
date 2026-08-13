"""The shipped Caddyfile must ADAPT. This is the cheapest test in the repo and it would have
caught a total outage of the build sandbox.

WHY IT EXISTS. v1.6.12 shipped a `log` directive nested inside a `handle` block. `log` is not an
ordered HTTP handler, so `caddy adapt` rejects the file outright — on the pinned 2.8.4 and on
every later version. In the container the consequence is not a warning: `entrypoint.sh`
backgrounds Caddy, so its non-zero exit is never seen by `set -eu`, nothing binds :8080, and the
ACA startup probe (30 × 1s against `/_sup/health` on 8080) fails until the revision is abandoned.
Every provision fails, and nothing in the build or the test suite says why.

WHY NOTHING CAUGHT IT. Every existing Caddy assertion lives in the `integration` lane, which is
opt-in, needs Docker, needs a ~10-minute image bake, and is absent from CI. The default lane
never touched the Caddyfile at all. So this test is deliberately NOT an integration test: it
shells out to a tiny official Caddy image and asks one question in about a second. If Docker is
missing it skips, exactly like the rest of the harness.

It also pins the two properties the fix depends on, because "it adapts" alone would still pass
if someone moved the logger back under `handle` for the app block and dropped `/_sup` logging by
accident:

  * the site has a logger at all (delete it and reclamation reads every container as idle), and
  * `/_sup/*` is excluded from it (delete `log_skip` and the platform's own 1-second startup
    probes get counted as user traffic, so an idle container looks busy forever — the exact
    failure mode the R14 signal was built to avoid).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# The Caddyfile lives one level up from tests/ (sandbox/Caddyfile).
CADDYFILE = Path(__file__).resolve().parent.parent / "Caddyfile"

# The version the image pins, plus the version it is moving to. BOTH must adapt: the fix must not
# depend on the bump, or a Caddy rollback would silently reintroduce the outage.
CADDY_VERSIONS = ("2.8.4", "2.11.4")


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


def _adapt(version: str) -> subprocess.CompletedProcess[str]:
    """Run `caddy adapt` over the real Caddyfile in an official image."""
    return subprocess.run(
        [
            "docker", "run", "--rm", "--platform", "linux/amd64",
            "-v", f"{CADDYFILE}:/etc/caddy/Caddyfile:ro",
            f"caddy:{version}-alpine",
            "caddy", "adapt", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )  # fmt: skip


@pytest.fixture(scope="module", autouse=True)
def _needs_docker() -> None:
    if not _docker_available():
        pytest.skip("Docker is not available — Caddyfile adapt check skipped")


@pytest.mark.parametrize("version", CADDY_VERSIONS)
def test_the_shipped_caddyfile_adapts(version: str) -> None:
    result = _adapt(version)
    assert result.returncode == 0, (
        f"sandbox/Caddyfile does not adapt on Caddy {version}. Caddy would fail to start, "
        f"nothing would bind :8080, and every ACA provision would die at the startup probe.\n"
        f"{result.stderr.strip()}"
    )


def test_the_site_logger_exists_and_skips_the_control_plane() -> None:
    """The access log must cover the app and exclude `/_sup/*`.

    Asserted on the ADAPTED JSON rather than on the Caddyfile text, so it survives any spelling
    of the same config and cannot be satisfied by a comment.
    """
    result = _adapt(CADDY_VERSIONS[-1])
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)

    server = next(iter(config["apps"]["http"]["servers"].values()))
    assert "logs" in server, (
        "the site has no access logger — request accounting would read every container as "
        "never-used, which fails toward reclaiming a container that is in active use"
    )
    # `log_skip` adapts to a `skip` entry somewhere in the route tree; assert on the serialized
    # server so this does not depend on Caddy's internal nesting, which differs between versions.
    assert "skip" in json.dumps(server), (
        "`/_sup/*` is not excluded from the access log — the platform's own 1-second startup "
        "probes would be counted as user traffic and an idle container would look busy forever"
    )
