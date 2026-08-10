#!/usr/bin/env node
/**
 * db-migrate.mjs — apply every pending migration under ./drizzle.
 *
 * PLATFORM-OWNED. `services/deploy/context.py` writes this over the app's own copy when it
 * packs the build context, so every app — including ones built before this file existed —
 * gets a migrator that can fail.
 *
 * TWO MODES, and the difference is the entire point of this file:
 *
 *   default (`npm run dev` in the sandbox) — NEVER take the app down. Every failure is
 *     caught and printed, a hung migration is abandoned, and the process always exits 0.
 *     That is correct there: the build harness decides the app is up only when `next dev`
 *     prints its ready line, so a migrator that fails hard or hangs makes the harness
 *     report "the dev server did not become ready" and sends the build agent hunting a
 *     rendering bug that does not exist.
 *
 *   `--strict` (the published container's CMD) — FAIL LOUDLY. A partially-applied schema
 *     that exits 0 gives you a container that passes its probe, serves traffic, and lets
 *     users write against a half-migrated database. There is no worse outcome available
 *     here, so in production a failed migration must stop the container from starting.
 *
 * The strict path deliberately has NO "give up and start anyway" timer. It has a deadline
 * that FAILS, which is a different thing: the non-strict timer calls process.exit(0) while
 * the migration is still running mid-DDL, and repeating that against a live database is
 * how you get a schema nobody can reason about.
 */

import path from "node:path";
import { fileURLToPath } from "node:url";

const STRICT = process.argv.includes("--strict");

/** Non-strict: long enough for a cold connection plus a handful of DDL statements, short
 *  enough to leave the dev server most of its readiness budget. */
const DEV_TIMEOUT_MS = 20_000;
/** Strict: a real ceiling on unattended DDL. Generous, because the alternative to waiting
 *  is a half-applied migration; bounded, because a container that never starts is at least
 *  visible. Enforced server-side too, via statement_timeout. */
const STRICT_TIMEOUT_MS = 600_000;

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const migrationsFolder = path.join(projectRoot, "drizzle");

/** Drizzle wraps the driver error, and the useful part ("relation already exists",
 *  "connection refused") is on `.cause` — print the whole chain or the diagnostic is
 *  useless. */
function describe(error) {
  const parts = [];
  for (let e = error; e; e = e.cause) parts.push(e.message ?? String(e));
  return parts.join(" ← ");
}

function fail(message) {
  console.error(`[db] ${message}`);
  process.exit(STRICT ? 1 : 0);
}

async function main() {
  const connectionString = process.env.BIAL_DATABASE_URL;
  if (!connectionString) {
    // In the sandbox this is a normal, survivable state (the platform may not have
    // provisioned a database yet). In a published container it is a misconfiguration:
    // the app is about to serve traffic with no database at all.
    if (STRICT) fail("BIAL_DATABASE_URL is not set — refusing to start without a database.");
    console.log(
      "[db] BIAL_DATABASE_URL is not set — skipping migrations. The app will still start; " +
        "anything that queries the database will fail until the platform injects it.",
    );
    return;
  }

  const { default: pg } = await import("pg");
  const { drizzle } = await import("drizzle-orm/node-postgres");
  const { migrate } = await import("drizzle-orm/node-postgres/migrator");

  const timeoutMs = STRICT ? STRICT_TIMEOUT_MS : DEV_TIMEOUT_MS;
  const timer = setTimeout(() => {
    if (STRICT) {
      // Exit 1, NOT 0. The container does not start, and an operator sees why.
      console.error(`[db] migrations still running after ${timeoutMs}ms — aborting.`);
      process.exit(1);
    }
    console.error(
      `[db] migrations still running after ${timeoutMs}ms — starting the app without them.`,
    );
    process.exit(0);
  }, timeoutMs);

  // One connection. `statement_timeout` bounds a single wedged statement server-side, so a
  // lock wait cannot burn the whole budget above and leave nothing for the rest.
  const pool = new pg.Pool({
    connectionString,
    max: 1,
    connectionTimeoutMillis: 10_000,
    ...(STRICT ? { options: `-c statement_timeout=${STRICT_TIMEOUT_MS}` } : {}),
  });
  try {
    await migrate(drizzle(pool), { migrationsFolder });
    console.log("[db] migrations up to date.");
  } finally {
    clearTimeout(timer);
    await pool.end().catch(() => {});
  }
}

try {
  await main();
} catch (error) {
  fail(`migrations failed: ${describe(error)}`);
}
// Reached only on success (every failure path above already exited).
process.exit(0);
