"""The scheduled sweep's own wiring: the dev allowlist, the kill switch, and the write-back gate.

Extracted from the reclamation janitor's test file when that janitor was removed — this sweep
was always the OTHER reaper, the one doing almost all of the deleting, and it keeps its own
protections regardless of what else on the worker ships or does not."""

from __future__ import annotations

import uuid

import pytest
import redis.asyncio as aioredis

from src.services.build_sessions.destroy import may_destroy_on_this_control_plane


def test_only_production_may_destroy() -> None:
    assert may_destroy_on_this_control_plane("production") is True
    assert may_destroy_on_this_control_plane("development") is False
    assert may_destroy_on_this_control_plane("staging") is False


class _Settings:
    """Just enough of the settings surface for `_off_duty_because`: an environment, a
    configured coordination store, and the sweep's own kill switch."""

    def __init__(self, environment: str, *, sweep: bool = True) -> None:
        self.ENVIRONMENT = environment
        self.sandbox = _SandboxFlags(sweep=sweep)
        # The scheduled sweep's off-duty check reads this before anything else; a `None` here
        # would answer "unconfigured" and hide whatever the test was actually asking about.
        self.redis = object()


class _SandboxFlags:
    def __init__(self, *, sweep: bool = True) -> None:
        self.sweep_enabled = sweep


class _Fleet:
    """A bare fleet fake: only what `get_sandbox()` must return for the sweep to reach
    `sweep_all`, never itself inspected once there — every test here monkeypatches `sweep_all`
    directly."""

    async def list_sandbox_fleet(self):  # noqa: ANN201
        return []


def test_the_sweep_does_not_stop_because_a_sibling_pass_is_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE UPGRADE THAT SILENTLY STOPS REAPING. `sweep_all` predates every other scheduled pass
    on this worker — an unflagged `while True` doing almost all of the deleting. Gating it on a
    sibling pass's own flag would have stopped reaping everywhere the moment that flag shipped
    off, with only the Azure bill as a symptom.

    Mutation-check: point `_off_duty_because` at a flag other than `sweep_enabled` and this goes
    red."""
    from src.workers import sandbox_reap

    monkeypatch.setattr(sandbox_reap, "settings", _Settings("production", sweep=True))
    assert sandbox_reap._off_duty_because() is None

    monkeypatch.setattr(sandbox_reap, "settings", _Settings("production", sweep=False))
    assert sandbox_reap._off_duty_because() == "flag_off"


async def test_the_scheduled_sweep_deletes_nothing_off_production(
    monkeypatch: pytest.MonkeyPatch, fake_redis: aioredis.Redis
) -> None:
    """THE SAME STANDING DIRECTIVE EVERY DESTRUCTIVE PASS IS UNDER: the dev subscription runs
    containers people use to validate this feature, and an unattended timer must not delete from
    it. Scoped to the SCHEDULED sweep only — `POST /v1/internal/reap` still sweeps anywhere
    (superadmin, audited), and reconcile-on-start still collects a developer's own stale
    sandbox on their next build.

    Mutation-check: drop the `may_destroy_on_this_control_plane` check from `_off_duty_because`
    and this goes red."""
    from src.workers import sandbox_reap

    swept: list[object] = []

    async def _spy_sweep(*args: object, **kwargs: object) -> object:
        swept.append(kwargs)
        raise AssertionError("the scheduled sweep must not run off production")

    async def _owning() -> dict[str, uuid.UUID]:
        return {}

    # Redis, the control plane and the owner map are all AVAILABLE on purpose: the sweep must
    # reach `sweep_all` and fail on the spy, not go green by tripping over an unconfigured dep.
    monkeypatch.setattr("src.services.build_sessions.reaper.sweep_all", _spy_sweep)
    monkeypatch.setattr(sandbox_reap, "_owning_app_ids", _owning)
    monkeypatch.setattr("src.services.sandbox.get_sandbox", lambda: _Fleet())

    for environment in ("development", "staging"):
        monkeypatch.setattr(sandbox_reap, "settings", _Settings(environment))
        await sandbox_reap.reap_abandoned_sandboxes()

    assert swept == []
    monkeypatch.setattr(sandbox_reap, "settings", _Settings("production"))
    assert sandbox_reap._off_duty_because() is None


async def test_the_scheduled_sweep_hands_the_owning_app_ids_to_the_gate(
    monkeypatch: pytest.MonkeyPatch, fake_redis: aioredis.Redis
) -> None:
    """WITHOUT THE MAP THE GATE IS OFF ON THIS PATH. `reap_user` only consults
    the write-back only when it is handed an `app_id`, and this sweep handed it nothing — so the
    durable-copy gate protected the rare orphan a by-hand reap collects and not the
    claimed-but-expired population, which is where the deletions actually happen.

    Mutation-check: drop `app_ids_by_name=await _owning_app_ids()` from the sweep call and this
    goes red (the sweep is handed `None`, which is indistinguishable from opting out)."""
    from src.services.build_sessions.reaper import SweepResult
    from src.workers import sandbox_reap

    app_id = uuid.uuid4()
    seen: dict[str, object] = {}

    async def _spy_sweep(redis, client, *, live_users, app_ids_by_name=None):  # noqa: ANN001
        seen["map"] = app_ids_by_name
        return SweepResult(reaped=0, failed=0)

    async def _owning() -> dict[str, uuid.UUID]:
        return {"sbx-x": app_id}

    monkeypatch.setattr("src.services.build_sessions.reaper.sweep_all", _spy_sweep)
    monkeypatch.setattr(sandbox_reap, "_owning_app_ids", _owning)
    monkeypatch.setattr("src.services.sandbox.get_sandbox", lambda: _Fleet())
    monkeypatch.setattr(sandbox_reap, "settings", _Settings("production"))

    await sandbox_reap.reap_abandoned_sandboxes()

    assert seen["map"] == {"sbx-x": app_id}
