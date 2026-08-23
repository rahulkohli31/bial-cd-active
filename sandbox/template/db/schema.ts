/**
 * db/schema.ts — this app's database schema, in Drizzle.
 *
 * Starts EMPTY on purpose — there is no demonstration data model to work around or delete. Add
 * the tables your app actually needs; nothing here is required by the platform, the platform
 * owns the database's lifecycle, the app owns everything inside it (ADR-0028).
 *
 * The workflow after ANY edit to this file:
 *   1. `npx drizzle-kit generate --name <what_changed>` → writes a new versioned .sql file under
 *      ./drizzle. Make ONE kind of schema change per generate — mixing a rename into the same
 *      diff as an add is what wakes drizzle-kit's interactive rename resolver, and nothing in
 *      this sandbox can answer it.
 *   2. `npm run db:migrate`                            → applies the pending migrations to the
 *      database
 * Never use drizzle-kit's `push` command: it mutates the database with no migration file, so the
 * next snapshot restores code that expects tables the database does not have.
 */

export {};
