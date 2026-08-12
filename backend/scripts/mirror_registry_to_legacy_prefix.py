"""THE ROLLBACK FOR THE R22 REGISTRY PREFIX CUTOVER — mirror the new keys back onto the old shape.

WHY THIS EXISTS. The cutover ships FORWARD safely: `read_registry` reads both prefixes and
migrates a legacy hash it finds, so a fleet registered before the deploy stays visible. There is
no matching path BACKWARD. Roll the release back — for any reason, including one that has nothing
to do with reclamation — and the previous image reads only `bial:sandbox:registry:{user}`, which is
exactly where the records no longer are. Every container live at that moment becomes invisible to
`sweep_all` AND to the Azure inventory at once, and the registry hash is the one key family with no
TTL, so nothing expires and nothing cleans up. That is the entire orphan class ADR-0029 exists to
collect, manufactured wholesale by a rollback that looked routine.

A rollback plan whose first step is "write a script" is not a rollback plan. This is the script.

WHAT IT DOES. For every `bial:{ENVIRONMENT}:sandbox:registry:*`, COPY the hash to the bare
`bial:sandbox:registry:{user}` key. Non-destructive by construction: it only ever WRITES the
legacy shape and never touches the current one, so running it against a deployment that is not
rolling back costs a few kilobytes and changes no behaviour — the forward image reads the current
prefix first and never looks.

It is safe to run BEFORE the rollback (recommended: the window where records are missing is then
zero) and idempotent, so run it as often as you like.

WHAT IT DELIBERATELY DOES NOT DO:

* It does not delete anything, ever. Nothing here can lose a record.
* It does not mirror `lock`, `heartbeat` or `lease`. Those are short-TTL keys that re-establish
  themselves within ninety seconds of a build resuming, and a lock mirrored under a prefix the
  rolled-back image writes with a different TTL is a way to lock somebody out for fifteen minutes.
  The registry hash is the one that cannot rebuild itself.
* It does not skip records that already exist under the legacy prefix — it OVERWRITES them,
  because the environment-scoped record is by definition the newer claim. A stale legacy hash
  naming a container that has since been replaced is precisely the "teardown pointed at the wrong
  container" failure the migration-on-read path guards against.

  DRY RUN (default):  uv run python -m scripts.mirror_registry_to_legacy_prefix
  APPLY:              uv run python -m scripts.mirror_registry_to_legacy_prefix --apply

Read `docs/engineering/deployment/` for where this sits in the release-B checklist.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write the legacy keys (default: report what would be written)",
    )
    args = parser.parse_args()

    from src.config import settings
    from src.services.redis import get_redis
    from src.services.redis.keys import (
        FAMILY_REGISTRY,
        KEY_ROOT,
        LEGACY_KEY_PREFIX,
        REGISTRY_FIELD_APP_NAME,
        key_prefix,
    )

    if settings.redis is None:
        print("REFUSING: this deployment has no Redis configured.", file=sys.stderr)
        return 2

    redis = get_redis()
    current_pattern = f"{key_prefix()}{FAMILY_REGISTRY}:*"
    print(f"environment : {settings.ENVIRONMENT}")
    print(f"reading     : {current_pattern}")
    print(f"writing     : {LEGACY_KEY_PREFIX}{FAMILY_REGISTRY}:<user_id>")
    print(f"mode        : {'APPLY' if args.apply else 'dry run'}")
    print("-" * 72)

    mirrored = skipped = 0
    async for raw_key in redis.scan_iter(match=current_pattern):
        key = str(raw_key)
        user_id = key.rsplit(":", 1)[-1]
        # A key we did not write has no business being copied under a prefix the previous image
        # trusts. Cheap sanity rather than a full UUID parse: the shape is ours or it is skipped.
        if not key.startswith(KEY_ROOT) or not user_id:
            skipped += 1
            continue
        record = await redis.hgetall(key)
        if not record:
            skipped += 1
            continue
        legacy = f"{LEGACY_KEY_PREFIX}{FAMILY_REGISTRY}:{user_id}"
        readable = {str(k): str(v) for k, v in record.items()}
        print(f"  {user_id}  ->  {readable.get(REGISTRY_FIELD_APP_NAME, '?')}")
        if args.apply:
            # Inline comprehension, not the `readable` variable above: redis-py types `mapping` as
            # `Mapping[FieldT, EncodableT]` whose KEY parameter is invariant, so a named
            # `dict[str, str]` fails the type gates while the identical inline literal passes.
            # The same workaround `locks._adopt_a_pre_cutover_record` carries, for the same reason.
            await redis.hset(legacy, mapping={str(k): str(v) for k, v in record.items()})
        mirrored += 1

    print("-" * 72)
    print(
        f"{mirrored} record(s) {'mirrored' if args.apply else 'would be mirrored'}"
        f"{f', {skipped} skipped' if skipped else ''}"
    )
    if not args.apply:
        print("\nNothing was written. Re-run with --apply to perform the mirror.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
