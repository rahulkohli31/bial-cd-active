"""One-off DEV cleanup: merge the duplicate anant.gupta@rvaiglobal.com user rows (DH / U9).

Investigation (independently verified against the dev DB, 2026-07-24). Two rows share the
email — `users.email` is deliberately NON-unique (ADR-0004 scopes by `user_id`, not email):

  ORPHAN  (the .mythos test-harness identity)   canonical=False
    id   019f4bc9-58ec-74ba-9f0d-044ce94addf3
    oid  'mythos-synth:...'  — a SYNTHETIC cookie-injection login oid, never a real Entra GUID
    owns the BULK of content (61 projects, ALL conversations + messages + attachments).

  CANONICAL  (Anant's genuine Entra sign-in)     canonical=True
    id   019f4f3d-6ced-7727-9f0c-afefb98b8021
    oid  '932efc26-17db-4251-b90b-e85042b1dffb'  — a real Entra Object-ID GUID, re-upserted on
    every browser sign-in.

Why CANONICAL is the REAL row, not the one with more data: the Entra callback upsert is
idempotent on `azure_oid` (`api/v1/auth/router.py`), so the NEXT genuine browser login resolves
to the REAL row. If the synthetic row were kept, that login would re-create the REAL row empty
and re-split everything. Canonical must be the oid future sign-ins land on. Root cause = the test
harness seeding a synthetic oid with a real email — NOT a live-auth bug, so no code guard is
warranted (investigate-then-cleanup, per the plan).

THE CASCADE TRAP. `OwnedByUserMixin.user_id` is `ON DELETE CASCADE`, so a *missed* owned table
is not left orphaned — deleting ORPHAN would silently CASCADE-DELETE its rows. So this script
reassigns EVERY one of the nine ON DELETE CASCADE owned tables (introspected + pinned below) from
ORPHAN to CANONICAL BEFORE the delete, and proves ORPHAN owns zero rows before deleting it. The
three UNIQUE-constrained tables MERGE rather than blind-UPDATE. One transaction; dry-run by
default; the `.mythos/walkthrough-e2e/backups/` dump is the rollback.

  DRY RUN (default):  uv run python -m scripts.merge_duplicate_user_rows --confirm-env dev
  EXECUTE:            uv run python -m scripts.merge_duplicate_user_rows --confirm-env dev \
                        --i-am-the-account-owner --keep-limit <default|real|synth> \
                        --refresh-tokens <delete|reassign> --execute
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.base import async_session_factory

# The two rows, pinned so the script can NEVER target the wrong users (verified at runtime:
# both must exist, share the expected email, and ORPHAN's oid must be the synthetic prefix).
CANONICAL_ID = UUID("019f4f3d-6ced-7727-9f0c-afefb98b8021")
ORPHAN_ID = UUID("019f4bc9-58ec-74ba-9f0d-044ce94addf3")
EXPECTED_EMAIL = "anant.gupta@rvaiglobal.com"
SYNTH_OID_PREFIX = "mythos-synth:"

# The nine ON DELETE CASCADE owned tables (introspected from pg_constraint on users.id).
# Plain reassign is a straight UPDATE ... SET user_id; the three UNIQUE-constrained tables are
# handled specially below. `feedback`/`messages`/`conversations`/`projects`/`app_registry` reassign
# by user_id; `app_registry` ALSO carries a bare (no-FK) `approved_by` uuid that must be rewritten
# or governance metadata would point at a deleted user.
PLAIN_REASSIGN_TABLES = ("projects", "conversations", "messages", "feedback")

# token_usage columns to SUM on a (user_id, usage_date) collision (uq_token_usage_user_date):
_TOKEN_COLS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

# Every owned table, for the "delete cascades NOTHING" proof (ORPHAN owns 0 rows before delete).
_ALL_OWNED_TABLES = (
    "projects",
    "conversations",
    "messages",
    "app_registry",
    "attachments",
    "token_usage",
    "user_limits",
    "refresh_tokens",
    "feedback",
)


async def _scalar(db: AsyncSession, sql: str, **params: object) -> int:
    result = await db.execute(text(sql), params)
    return int(result.scalar_one())


async def _dml(db: AsyncSession, sql: str, **params: object) -> int:
    """Run an UPDATE/DELETE and return the affected row count. AsyncSession.execute is typed as
    returning Result, but Core DML yields a CursorResult carrying .rowcount — cast once, here."""
    result = await db.execute(text(sql), params)
    return cast("CursorResult[Any]", result).rowcount


async def _verify_identities(db: AsyncSession) -> None:
    """Positive same-human / correct-target checks BEFORE any write. Email is non-unique, so a
    shared email is not proof — we additionally require ORPHAN to be the synthetic harness oid and
    CANONICAL to be a non-synthetic (real Entra) oid, and both to carry the expected email."""
    rows = (
        await db.execute(
            text("select id, email, azure_oid from users where id in (:canon, :orphan)"),
            {"canon": CANONICAL_ID, "orphan": ORPHAN_ID},
        )
    ).all()
    by_id = {r.id: r for r in rows}
    if CANONICAL_ID not in by_id or ORPHAN_ID not in by_id:
        raise SystemExit(f"FATAL: expected both rows to exist; found {sorted(by_id)}")
    canon, orphan = by_id[CANONICAL_ID], by_id[ORPHAN_ID]
    if canon.email != EXPECTED_EMAIL or orphan.email != EXPECTED_EMAIL:
        raise SystemExit(f"FATAL: email mismatch (canon={canon.email!r} orphan={orphan.email!r})")
    if not orphan.azure_oid.startswith(SYNTH_OID_PREFIX):
        raise SystemExit(
            f"FATAL: ORPHAN oid {orphan.azure_oid!r} is not the synthetic harness identity — "
            "refusing to merge a row that may be a real second account."
        )
    if canon.azure_oid.startswith(SYNTH_OID_PREFIX):
        raise SystemExit(
            f"FATAL: CANONICAL oid {canon.azure_oid!r} is synthetic — wrong direction."
        )
    print(
        f"  identity check OK: ORPHAN oid={orphan.azure_oid!r} → CANONICAL oid={canon.azure_oid!r}"
    )


async def _merge_token_usage(db: AsyncSession) -> None:
    """uq_token_usage_user_date collides where both users logged usage on the same day. Sum the
    orphan's day into the canonical row, drop its colliding rows, then reassign the rest."""
    sums = ", ".join(f"{c} = token_usage.{c} + o.{c}" for c in _TOKEN_COLS)
    merged = await _dml(
        db,
        f"""
        UPDATE token_usage
           SET {sums}
          FROM token_usage o
         WHERE token_usage.user_id = :canon
           AND o.user_id = :orphan
           AND o.usage_date = token_usage.usage_date
        """,
        canon=CANONICAL_ID,
        orphan=ORPHAN_ID,
    )
    dropped = await _dml(
        db,
        """
        DELETE FROM token_usage
         WHERE user_id = :orphan
           AND usage_date IN (SELECT usage_date FROM token_usage WHERE user_id = :canon)
        """,
        canon=CANONICAL_ID,
        orphan=ORPHAN_ID,
    )
    reassigned = await _dml(
        db,
        "UPDATE token_usage SET user_id = :canon WHERE user_id = :orphan",
        canon=CANONICAL_ID,
        orphan=ORPHAN_ID,
    )
    print(
        f"  token_usage: summed {merged} colliding day(s) into canonical, "
        f"dropped {dropped} orphan collision(s), reassigned {reassigned} rest"
    )


async def _merge_user_limits(db: AsyncSession, keep: str) -> None:
    """uq_user_limits_user: exactly one row per user. BOTH duplicate rows are non-default overrides
    (REAL=10M/day, SYNTH=200M/day, set during load/E2E testing) above the 1M DAILY_TOKEN_LIMIT
    default, so 'admin-set wins' does not disambiguate — the operator chooses:
      keep='default' (recommended) — drop BOTH override rows so the merged user falls back to
                     the global 1M default (no user_limits row → settings.DAILY_TOKEN_LIMIT);
      keep='real'    — drop the orphan's override; the canonical keeps its own (10M);
      keep='synth'   — copy the orphan's override onto the canonical (200M), then drop orphan's."""
    if keep == "synth":
        await _dml(
            db,
            """
            UPDATE user_limits c
               SET daily_token_limit = o.daily_token_limit,
                   context_soft_limit = o.context_soft_limit,
                   context_hard_limit = o.context_hard_limit
              FROM user_limits o
             WHERE c.user_id = :canon AND o.user_id = :orphan
            """,
            canon=CANONICAL_ID,
            orphan=ORPHAN_ID,
        )
    if keep == "default":
        # Drop the CANONICAL's override too, so the merged user has no row → the global 1M default.
        canon_dropped = await _dml(
            db, "DELETE FROM user_limits WHERE user_id = :canon", canon=CANONICAL_ID
        )
        print(f"  user_limits: dropped the canonical's override too ({canon_dropped} row) → 1M")
    dropped = await _dml(db, "DELETE FROM user_limits WHERE user_id = :orphan", orphan=ORPHAN_ID)
    print(f"  user_limits: kept {keep.upper()}, dropped orphan's override ({dropped} row)")


async def _handle_refresh_tokens(db: AsyncSession, mode: str) -> None:
    """refresh_tokens are ephemeral session credentials. Default DELETE avoids moving a captured
    synth-session token across the identity boundary; reassign is offered but discouraged."""
    if mode == "delete":
        n = await _dml(db, "DELETE FROM refresh_tokens WHERE user_id = :orphan", orphan=ORPHAN_ID)
        print(f"  refresh_tokens: deleted {n} orphan session credential(s)")
    else:
        n = await _dml(
            db,
            "UPDATE refresh_tokens SET user_id = :canon WHERE user_id = :orphan",
            canon=CANONICAL_ID,
            orphan=ORPHAN_ID,
        )
        print(f"  refresh_tokens: reassigned {n} (NOTE: session creds crossed identity)")


async def _reassign_rest(db: AsyncSession) -> None:
    """The plain reassigns + attachments (no attachment_id collision, verified) + app_registry's
    user_id AND its bare no-FK `approved_by` governance pointer."""
    for table in (*PLAIN_REASSIGN_TABLES, "attachments", "app_registry"):
        n = await _dml(
            db,
            f"UPDATE {table} SET user_id = :canon WHERE user_id = :orphan",
            canon=CANONICAL_ID,
            orphan=ORPHAN_ID,
        )
        print(f"  {table}: reassigned {n} row(s)")
    # approved_by has NO foreign key (it is not in the CASCADE set), so the delete would leave it
    # dangling — rewrite it explicitly so governance metadata follows the canonical user.
    n = await _dml(
        db,
        "UPDATE app_registry SET approved_by = :canon WHERE approved_by = :orphan",
        canon=CANONICAL_ID,
        orphan=ORPHAN_ID,
    )
    print(f"  app_registry.approved_by: rewrote {n} dangling approver pointer(s)")


async def _assert_orphan_owns_nothing(db: AsyncSession) -> None:
    """The CASCADE-trap guard: after every reassignment, ORPHAN must own zero rows across all nine
    owned tables (and be no one's approver), so the delete cascades NOTHING."""
    leftover = {}
    for table in _ALL_OWNED_TABLES:
        n = await _scalar(
            db, f"select count(*) from {table} where user_id = :orphan", orphan=ORPHAN_ID
        )
        if n:
            leftover[table] = n
    approver = await _scalar(
        db, "select count(*) from app_registry where approved_by = :orphan", orphan=ORPHAN_ID
    )
    if approver:
        leftover["app_registry.approved_by"] = approver
    if leftover:
        raise SystemExit(
            f"ABORT: orphan still owns rows (a delete would CASCADE-LOSE them): {leftover}"
        )
    print("  ✓ orphan owns 0 rows across all nine owned tables — the delete will cascade NOTHING")


def _resolve_and_gate(args: argparse.Namespace) -> None:
    """Code-enforced dev-only gate: refuse in production AND require the operator to name the env,
    matching the resolved DB host, so a mis-pointed DATABASE_URL fails before any write."""
    if settings.is_production:
        raise SystemExit(
            "FATAL: settings.is_production is True — this dev cleanup refuses to run."
        )
    dsn = settings.DATABASE_URL.get_secret_value()
    host_is_local = "@localhost" in dsn or "@127.0.0.1" in dsn
    if args.confirm_env != "dev" or not host_is_local:
        raise SystemExit(
            f"FATAL: env gate — pass --confirm-env dev AND point DATABASE_URL at a local host "
            f"(got confirm_env={args.confirm_env!r}, local_host={host_is_local})."
        )
    if args.execute and not args.i_am_the_account_owner:
        raise SystemExit(
            "FATAL: --execute requires --i-am-the-account-owner (positive same-human attestation: "
            "email is non-unique, so the operator must confirm both rows are their own identity)."
        )


async def _run(args: argparse.Namespace) -> None:
    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"=== merge_duplicate_user_rows [{mode}] ===")
    print(f"    ORPHAN {ORPHAN_ID} → CANONICAL {CANONICAL_ID}")
    async with async_session_factory() as db:
        await _verify_identities(db)
        await _merge_token_usage(db)
        await _merge_user_limits(db, args.keep_limit)
        await _handle_refresh_tokens(db, args.refresh_tokens)
        await _reassign_rest(db)
        await _assert_orphan_owns_nothing(db)
        if args.execute:
            deleted = await _dml(db, "DELETE FROM users WHERE id = :orphan", orphan=ORPHAN_ID)
            if deleted != 1:
                raise SystemExit(f"ABORT: orphan delete affected {deleted} rows, expected 1")
            await db.commit()
            print("  ✓ orphan user deleted; transaction COMMITTED.")
        else:
            await db.rollback()
            print("  DRY RUN complete — transaction ROLLED BACK. Re-run with --execute to commit.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-env", required=True, help="must be 'dev' (env gate)")
    parser.add_argument(
        "--execute", action="store_true", help="commit (default: dry-run + rollback)"
    )
    parser.add_argument(
        "--i-am-the-account-owner", action="store_true", help="same-human attestation"
    )
    parser.add_argument(
        "--keep-limit",
        choices=("default", "real", "synth"),
        default="default",
        help="which daily-token-limit to keep: 'default' (recommended) drops both overrides → the "
        "global 1M default; 'real' keeps 10M; 'synth' keeps 200M",
    )
    parser.add_argument("--refresh-tokens", choices=("delete", "reassign"), default="delete")
    args = parser.parse_args()
    _resolve_and_gate(args)
    try:
        asyncio.run(_run(args))
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
