/**
 * db/index.ts — the SERVER-ONLY database client.
 *
 * Import this from Server Components, Route Handlers, and Server Actions ONLY. It must never be
 * imported into a Client Component: the connection string is a real credential, and anything a
 * `"use client"` module imports is shipped to the browser. The `window` guard below turns that
 * mistake into a loud error instead of a leak.
 *
 * Usage — `yourTable` is whatever you defined in `db/schema.ts`, which starts empty:
 *   import { getDb } from "@/db";
 *   import { yourTable } from "@/db/schema";
 *   const rows = await getDb().select().from(yourTable);
 */

import { drizzle, type NodePgDatabase } from "drizzle-orm/node-postgres";
import { Pool } from "pg";

import * as schema from "./schema";

/**
 * The pool size is PINNED and small on purpose — do not raise it.
 *
 * Every generated app connects DIRECTLY to a shared PostgreSQL server with a fixed
 * `max_connections` ceiling, and the budget is:
 *
 *     Σ(app pools) + sandbox pools + control plane  ≤  server user-connection ceiling
 *
 * One app widening its pool to "make things faster" spends everyone else's headroom and the
 * failure lands on a different app as a connection-refused error. If a query is slow, fix the
 * query or add an index.
 */
const POOL_MAX = 4;
const POOL_IDLE_TIMEOUT_MS = 30_000;
/** Fail a connect attempt rather than let a request hang on an unreachable server. */
const POOL_CONNECTION_TIMEOUT_MS = 10_000;

if (typeof window !== "undefined") {
  throw new Error("db/index.ts is server-only — import it from server code, never a Client Component.");
}

/**
 * `next dev` re-evaluates modules on every hot reload, so a module-level pool would leak a new
 * pool per edit and exhaust the budget above within a session. Cache it on `globalThis`, which
 * survives HMR.
 */
const globalForDb = globalThis as unknown as {
  __bialPool?: Pool;
  __bialDb?: NodePgDatabase<typeof schema>;
};

/**
 * The Drizzle client, built on first use.
 *
 * Lazy on purpose: `process.env.BIAL_DATABASE_URL` is read INSIDE the request path, not at module
 * scope. A module-scope read is evaluated while Next compiles the module graph, which can capture
 * an empty value and then cache it for the life of the process.
 */
export function getDb(): NodePgDatabase<typeof schema> {
  if (globalForDb.__bialDb) return globalForDb.__bialDb;

  const connectionString = process.env.BIAL_DATABASE_URL;
  if (!connectionString) {
    throw new Error(
      "BIAL_DATABASE_URL is not set — this app has no database in this environment. " +
        "It is injected by the platform into server processes only.",
    );
  }

  const pool =
    globalForDb.__bialPool ??
    new Pool({
      connectionString,
      max: POOL_MAX,
      idleTimeoutMillis: POOL_IDLE_TIMEOUT_MS,
      connectionTimeoutMillis: POOL_CONNECTION_TIMEOUT_MS,
    }).on("error", (err) => {
      // An idle client the SERVER terminates (platform disable/teardown, a failover)
      // surfaces here, not on a query. Without a listener, the 'error' event is an
      // uncaught exception that kills the whole app process; the next query after this
      // fires gets a fresh connection attempt and a normal query-level error instead.
      console.error("[db] idle client error:", err.message);
    });
  globalForDb.__bialPool = pool;

  const db = drizzle(pool, { schema });
  globalForDb.__bialDb = db;
  return db;
}
