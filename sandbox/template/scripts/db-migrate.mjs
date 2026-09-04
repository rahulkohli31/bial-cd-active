#!/usr/bin/env node
/**
 * db-migrate.mjs — apply every pending migration under ./drizzle, and NEVER take the app down.
 *
 * `npm run dev` runs this before `next dev` so a fresh sandbox boots with its schema already in
 * place. That makes it load-bearing that it always exits 0 and always exits quickly:
 *
 *   - The build harness decides the app is up ONLY when `next dev` prints "Ready in", inside a
 *     ~30s readiness budget. A migrate step that fails hard, or hangs, means that line never
 *     appears — and the harness then reports "the dev server did not become ready", which sends
 *     the build agent hunting a rendering bug that does not exist.
 *   - So: every failure is caught and printed, a hung migration is abandoned after a timeout, and
 *     the process always exits 0. `package.json` also chains with `;` rather than `&&`, so even a
 *     crash in this file cannot stop the dev server.
 *
 * A migration that did not apply is still a real problem — it is just one the app should report
 * as a query error at runtime, not as a mysterious startup failure.
 *
 * Running it BY HAND is not the sanctioned channel. `npm run db:migrate` after
 * `npx drizzle-kit generate` is the two-step sequence `apply_schema_change` replaced: both halves
 * can print a failure and still exit 0 (this file is the reason the second one does), so run
 * separately they look like they worked. Edit `db/schema.ts` and call
 * `apply_schema_change(what_changed="…")` instead — see `db/schema.ts`'s header. Nothing refuses
 * a hand-run; it is simply the version that cannot tell you it failed.
 */

import path from "node:path";
import { fileURLToPath } from "node:url";

/** Long enough for a cold connection + a handful of DDL statements, short enough to leave the
 *  dev server most of its readiness budget. */
const TIMEOUT_MS = 20_000;

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const migrationsFolder = path.join(projectRoot, "drizzle");

async function main() {
  const connectionString = process.env.BIAL_DATABASE_URL;
  if (!connectionString) {
    console.log(
      "[db] BIAL_DATABASE_URL is not set — skipping migrations. The app will still start; " +
        "anything that queries the database will fail until the platform injects it.",
    );
    return;
  }

  const { default: pg } = await import("pg");
  const { drizzle } = await import("drizzle-orm/node-postgres");
  const { migrate } = await import("drizzle-orm/node-postgres/migrator");

  const timer = setTimeout(() => {
    console.error(
      `[db] migrations still running after ${TIMEOUT_MS}ms — starting the app without them.`,
    );
    process.exit(0);
  }, TIMEOUT_MS);

  // One connection, and give up on an unreachable server well before the timer above fires.
  const pool = new pg.Pool({ connectionString, max: 1, connectionTimeoutMillis: 10_000 });
  try {
    await migrate(drizzle(pool), { migrationsFolder });
    console.log("[db] migrations up to date.");
  } finally {
    clearTimeout(timer);
    await pool.end().catch(() => {});
  }
}

/** Drizzle wraps the driver error, and the useful part ("relation already exists", "connection
 *  refused") is on `.cause` — so print the whole chain or the diagnostic is useless. */
function describe(error) {
  const parts = [];
  for (let e = error; e; e = e.cause) parts.push(e.message ?? String(e));
  return parts.join(" ← ");
}

try {
  await main();
} catch (error) {
  // Never rethrow: see the header. The message is printed so the agent can actually fix it.
  console.error(`[db] migrations failed — starting the app anyway: ${describe(error)}`);
}
process.exit(0);
