import { defineConfig } from "drizzle-kit";

/**
 * drizzle-kit config — how `generate` and `migrate` find your schema and your database.
 *
 * The connection string is READ FROM THE ENVIRONMENT and never written down here: whatever this
 * file contains is committed and rides the snapshot bundle, so an inlined value would be a
 * credential in version control.
 *
 * The empty fallback is deliberate and has a defined meaning: `drizzle-kit generate` only reads
 * `./db/schema.ts` and works fine with no database at all, so it must not fail to load this file
 * when the platform has not injected a connection string yet. `migrate` does need one, and reports
 * a plain connection error when it is empty.
 */
export default defineConfig({
  schema: "./db/schema.ts",
  out: "./drizzle",
  dialect: "postgresql",
  dbCredentials: { url: process.env.BIAL_DATABASE_URL ?? "" },
  strict: true,
  verbose: true,
});
