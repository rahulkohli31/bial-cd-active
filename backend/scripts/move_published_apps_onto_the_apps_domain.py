"""Move already-published apps onto the shared apps hostname — REPORT FIRST, REWRITE SECOND.

WHY THIS EXISTS. Every published app's address used to be its own container's Azure Container
Apps FQDN. BIAL's environment is internal, so that name has no public DNS and never resolved
from an employee's desk. Those addresses are recorded in TWO independently-written places and
have been shared outside the platform, so a link already in circulation is affected.

WHAT SELF-HEALS AND WHAT DOES NOT. `deployments.url` is written by the deploy pipeline's own
success terminal, which now records the public address — so an app that is republished corrects
that column by itself, with no backfill. `app_registry.deployed_url` does NOT: it is the manual
go-live runbook's field, written only by an admin, and nothing republishes it.

THE ORDER IS THE SAFETY. An image built before the base path shipped serves at `/`, so pointing
a live link at `/a/pub-<key>/` makes the app answer 404 — turning today's honest
name-resolution failure into a page that reads to the person who followed it as "the platform
broke my app". This script therefore REFUSES to rewrite an app's recorded address until that
app's platform address has already moved, which is the observable proof a republished image
exists. The gate lives in `src/services/deploy/backfill.py` and is unit-tested there; this file
is only the driver.

THE OPERATOR PROCEDURE, in order:

  1. Run this script with no flags. It writes nothing and prints, per app, whether it is already
     moved, waiting on a republish, or deliberately left alone.
  2. For every app it lists as WAITING, publish it again through the ordinary deploy path — the
     same button a citizen uses. That is what produces an image carrying the base path. Nothing
     here can do it for you: a republish re-runs the classification gate, and this script has no
     business bypassing it.
  3. Run this script again with `--execute`. It rewrites only the apps whose republish it can
     see, and it is idempotent — a second run writes nothing.

  DRY RUN (default):  uv run python -m scripts.move_published_apps_onto_the_apps_domain
  EXECUTE:            uv run python -m scripts.move_published_apps_onto_the_apps_domain --execute
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.base import async_session_factory
from src.db.models.app_registry import AppRegistry
from src.db.models.deployment import Deployment, DeploymentStatus
from src.services.deploy.backfill import Action, AddressState, AppAddresses, decide

_PRE_IMAGE_DIR = Path("/tmp/bial-apps-domain-backfill")


async def _load(db: AsyncSession) -> list[AppAddresses]:
    """Every app that has ever recorded an address, with both of its addresses.

    The platform address is the LATEST SUCCESSFUL deployment's, not any deployment's: a failed
    or running row says nothing about what is live, and reading one would let a half-finished
    publish open the rewrite gate.
    """
    # ONE ROW PER APP, and the `rn == 1` filter is what makes that true. A window function does
    # NOT collapse rows — `first_value(...) OVER (PARTITION BY app_id)` computes the right VALUE
    # on every row and still returns one row per deployment, so an app with a redeploy history
    # would be joined once per deploy: counted twice in the report, and written twice by
    # `--execute`. The value was never wrong; the row count was, which is exactly the kind of
    # defect a report reads past.
    #
    # `id` breaks a `created_at` tie. UUIDv7 primary keys are time-sortable, so it agrees with
    # `created_at` rather than fighting it, and it makes the ordering total instead of merely
    # usually-unique.
    ranked = (
        sa.select(
            Deployment.app_id,
            Deployment.url,
            sa.func.row_number()
            .over(
                partition_by=Deployment.app_id,
                order_by=(Deployment.created_at.desc(), Deployment.id.desc()),
            )
            .label("rn"),
        )
        .where(Deployment.status == DeploymentStatus.SUCCEEDED)
        .subquery()
    )
    latest = sa.select(ranked.c.app_id, ranked.c.url).where(ranked.c.rn == 1).subquery()
    rows = (
        await db.execute(
            sa.select(AppRegistry.id, AppRegistry.deployed_url, latest.c.url)
            .outerjoin(latest, latest.c.app_id == AppRegistry.id)
            .where(sa.or_(AppRegistry.deployed_url.is_not(None), latest.c.url.is_not(None)))
        )
    ).all()
    return [
        AppAddresses(app_id=app_id, platform_url=platform, recorded_url=recorded)
        for app_id, recorded, platform in rows
    ]


def _report(actions: list[Action]) -> None:
    buckets: dict[str, list[Action]] = {"MOVE": [], "WAITING": [], "DONE": [], "LEFT ALONE": []}
    for a in actions:
        if a.rewrite_recorded_to is not None:
            buckets["MOVE"].append(a)
        elif a.blocked_reason is not None:
            buckets["WAITING"].append(a)
        elif a.recorded is AddressState.ALREADY_MOVED:
            buckets["DONE"].append(a)
        else:
            buckets["LEFT ALONE"].append(a)

    for label, group in buckets.items():
        print(f"\n{label}: {len(group)}")
        for a in group:
            print(f"  {a.app_id}  platform={a.platform.value}  recorded={a.recorded.value}")
            if a.rewrite_recorded_to:
                print(f"      -> {a.rewrite_recorded_to}")
            if a.blocked_reason:
                print(f"      held: {a.blocked_reason}")


async def _run(execute: bool) -> None:
    base = settings.APPS_BASE_URL
    print(f"apps base URL: {base}")
    async with async_session_factory() as db:
        actions = [decide(a, apps_base_url=base) for a in await _load(db)]
        _report(actions)

        moves = [a for a in actions if a.rewrite_recorded_to is not None]
        if not execute:
            print(f"\nDRY RUN — nothing written. {len(moves)} app(s) would move.")
            print("Re-run with --execute once you are satisfied with the list above.")
            return
        # THE PRE-IMAGE, WRITTEN BEFORE THE OVERWRITE. `deployed_url` is a field a human typed,
        # and this is the one writer that replaces it without a person in the loop — every other
        # writer is an admin action with an audit row behind it. An overwrite with no record of
        # what was there is not reversible, and "we can work it out from the app id" stops being
        # true the moment one of these rows turns out to have been right.
        pre_image = _PRE_IMAGE_DIR / f"deployed-url-preimage-{len(moves)}-rows.tsv"
        pre_image.parent.mkdir(parents=True, exist_ok=True)
        pre_image.write_text(
            "app_id\told_deployed_url\tnew_deployed_url\n"
            + "".join(
                f"{a.app_id}\t{a.recorded_before}\t{a.rewrite_recorded_to}\n" for a in moves
            ),
            encoding="utf-8",
        )
        print(f"\nPre-image written to {pre_image} — keep it until the move is confirmed.")

        for a in moves:
            await db.execute(
                sa.update(AppRegistry)
                .where(AppRegistry.id == a.app_id)
                .values(deployed_url=a.rewrite_recorded_to)
            )
        await db.commit()
        print(f"WROTE {len(moves)} recorded address(es).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="write the rewrites. Without it the script only reports.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_run(execute=args.execute))
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        sys.exit(130)


if __name__ == "__main__":
    main()
