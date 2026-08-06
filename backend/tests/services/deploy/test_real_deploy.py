"""`deploy_app` against REAL Azure infrastructure — no stubbing anywhere in this file.
This is `deploy_app`'s side of the "manual E2E round" `portal/e2e/real-sandbox.spec.ts`
is for the interactive build path: OPT-IN, needs the full real substrate wired into
the backend's `.env` (`SANDBOX__*`: ACA env + ACR; `OBJECT_STORE__*`: per-app blob),
and mirrors the `integration` pytest marker's existing shape rather than inventing a
new opt-in mechanism (the same rationale `real-sandbox.spec.ts` states for reusing
this marker instead of a bespoke env flag).

Skips cleanly (not a hang, not a hard failure) when `settings.sandbox` is unset — the
normal state for every environment except one that's had this real substrate wired up
by hand, exactly as `real-sandbox.spec.ts` required for the sandbox side.

WHAT THIS PROVES, if it passes: a real Container App gets created from the golden
image, the approved submission's ACTUAL git bundle (built for real via the local `git`
binary below, not fake header bytes) restores onto it for real via the supervisor's
`/exec`+`/files` HTTP API, `next dev` actually starts, and the app actually becomes
reachable at the ACA-issued URL — the full `deploy_app` contract, once, for real.

WHAT THIS DOES NOT COVER: the `deploy-reconcile` endpoint's OWN logic (selection
query, kill switch, report aggregation) — that's exercised with fakes in
`tests/api/v1/admin/test_deploy_reconcile.py`, deliberately, so a real-Azure run is
only ever needed to validate the ONE seam that can't be faked into confidence: whether
the real supervisor actually accepts this module's restore call. `RealDeployRuntime`
was already caught getting the create→restore→start ordering wrong once by design
review alone (see `provision.py`'s history) — this test exists because that class of
bug is exactly what fakes cannot catch.

TIMEOUTS ARE ESTIMATES, not measurements — nobody has run this against real Azure
yet. `real-sandbox.spec.ts`'s own comment is the cautionary tale: its FIRST estimate
(1-3 minutes) was wrong by an order of magnitude once a real monitored run (2026-07-29)
actually happened. Treat every number below the same way: correct it from a real run's
actual timing, don't trust the guess.

CLEANS UP the Container App it creates — this provisions real, billable infrastructure,
and a raised assertion must not leak it. The teardown runs in `finally`, unconditionally.
"""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest

from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.services.deploy.provision import DeployRuntimeError, RealDeployRuntime, deploy_app
from src.services.sandbox.aca import create_aca_control_plane
from src.services.sandbox.client import create_sandbox
from src.services.storage import get_app_container_store, get_storage, submission_key
from tests.factories import AppRegistryFactory, UserFactory

pytestmark = pytest.mark.integration

# A real monitored run (2026-07-29) of the INTERACTIVE sandbox path measured ~19s for
# provisioning alone (`real-sandbox.spec.ts`'s summary). This path also restores a
# bundle and boots `next dev` before the app is reachable — closer to that spec's
# measured ~14-minute FIRST-boot ceiling than to the 19s provisioning number alone, so
# start there rather than re-guessing. CORRECT THIS FROM A REAL RUN, do not trust it.
_DEPLOY_TIMEOUT_S = 15 * 60


def _real_git_bundle() -> bytes:
    """A GENUINE git bundle the real restore script's `git fetch`/`git checkout` can
    actually read — unlike every other test in this codebase, which fakes the bundle
    as header-shaped bytes that only `parse_bundle_head_sha`'s own parser needs to
    accept. This one has to survive a REAL `git fetch` against a REAL repo."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        (repo / "README.md").write_text("# real-deploy integration probe\n")
        env = {"GIT_AUTHOR_NAME": "bial-test", "GIT_AUTHOR_EMAIL": "test@bial.example.com"}
        env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"]
        env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"]
        run = lambda *args: subprocess.run(  # noqa: E731
            ["git", *args], cwd=repo, env=env, check=True, capture_output=True
        )
        run("init", "-q", "-b", "main")
        run("add", "-A")
        run("commit", "-q", "-m", "real-deploy integration probe")
        bundle_path = Path(tmp) / "app.bundle"
        run("bundle", "create", str(bundle_path), "HEAD")
        return bundle_path.read_bytes()


@pytest.fixture
def skip_unless_real_sandbox_configured() -> None:
    if settings.sandbox is None:
        pytest.skip(
            "settings.sandbox is not configured — wire SANDBOX__* (ACA env + ACR) into "
            "backend/.env to run this against real Azure, per real-sandbox.spec.ts's setup."
        )
    if settings.object_store is None:
        pytest.skip("settings.object_store is not configured — wire OBJECT_STORE__* to run this.")


async def test_deploy_app_against_real_azure(
    skip_unless_real_sandbox_configured, db_session, monkeypatch
) -> None:
    assert settings.sandbox is not None  # narrowed for the type checker; the fixture already gated

    owner = await UserFactory.create(db_session)
    submission_id = uuid.uuid4()
    app = await AppRegistryFactory.create(
        db_session,
        user_id=owner.id,
        status=AppStatus.APPROVED,
        approved_submission_id=submission_id,
        approved_commit_sha="0" * 40,  # not asserted on; the real bundle carries the real SHA
    )
    await db_session.commit()

    # Per-project database provisioning (ADR-0028, `APP_DB__*`) is its OWN already-tested
    # subsystem, orthogonal to what this test exists to validate (ACA provisioning + the real
    # restore script). Stubbing just this one step — same technique the unit tests in
    # test_provision.py already use — keeps everything else in `deploy_app` (storage, the
    # credential mint, ACA, the supervisor HTTP layer) 100% real. A syntactically valid but
    # unreachable DSN is fine: the golden template's baked demo app does not need a live DB
    # connection to boot `next dev` and answer its root route.
    import src.services.deploy.provision as provision_module
    from src.db.models.project_database import ProjectDatabase

    db_session.add(
        ProjectDatabase(
            project_id=app.project_id,
            db_name="db_probe",
            role_name="role_probe",
            password_encrypted="not-decrypted-in-this-test",
            db_ready=True,
        )
    )
    await db_session.commit()
    monkeypatch.setattr(
        provision_module,
        "sandbox_dsn",
        lambda record: "postgresql://probe:probe@127.0.0.1:5432/db_probe?sslmode=disable",
    )

    storage = get_storage()
    container_store = get_app_container_store()
    assert container_store is not None  # narrowed; gated above

    bundle = _real_git_bundle()
    await storage.put(
        submission_key(app.id, submission_id), bundle, content_type="application/x-git"
    )

    aca = create_aca_control_plane(settings.sandbox)
    sandbox_client = create_sandbox(settings.sandbox)
    runtime = RealDeployRuntime(aca, sandbox_client)

    try:
        ok = await deploy_app(
            app.id,
            db=db_session,
            storage=storage,
            container_store=container_store,
            runtime=runtime,
        )

        row = await db_session.get(AppRegistry, app.id)
        assert ok is True, f"deploy_app failed: {row.last_deploy_error}"
        assert row.deployed_url is not None
        assert row.deployed_url.startswith("https://")

        # THE REAL REFLOW CLAIM, not a proxy for it (matching real-sandbox.spec.ts's own
        # standard) — an actual HTTP GET against the actual deployed URL, not just "the
        # ACA API call didn't raise".
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(row.deployed_url)
        assert resp.status_code == 200, (
            f"deployed app at {row.deployed_url} did not answer 200 "
            f"(got {resp.status_code}) — check Azure Log Analytics container logs for "
            f"the real restore/dev-start output, the same way real-sandbox.spec.ts's "
            f"root-causing did, rather than re-guessing at the timeout."
        )
    except DeployRuntimeError as exc:
        pytest.fail(
            f"deploy_app raised against real Azure: {exc}. Check the container's own "
            f"logs (Azure Log Analytics) before adjusting any timeout in this file — "
            f"real-sandbox.spec.ts's own history is that guessed timeouts were wrong, "
            f"not that the platform was slow."
        )
    finally:
        # Real, billable infrastructure — must not leak past this test regardless of
        # pass/fail. Best-effort: a teardown failure is surfaced but must not mask
        # whatever the test itself already found.
        try:
            from src.services.deploy.provision import deploy_app_name

            await aca.delete_app(name=deploy_app_name(app.id))
        finally:
            await aca.aclose()
            if hasattr(sandbox_client, "aclose"):
                await sandbox_client.aclose()
