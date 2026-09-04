/**
 * db/schema.ts — this app's database schema, in Drizzle.
 *
 * Starts EMPTY on purpose — there is no demonstration data model to work around or delete. Add
 * the tables your app actually needs; nothing here is required by the platform, the platform
 * owns the database's lifecycle, the app owns everything inside it (ADR-0028).
 *
 * After ANY edit to this file, call `apply_schema_change(what_changed="…")`. It generates the
 * migration and applies it in ONE step — do not run `drizzle-kit generate` or `db:migrate` by
 * hand. The sandbox has no terminal to answer drizzle-kit's interactive rename resolver, and the
 * hand-driven pair is what left a schema half-applied.
 *
 * NOTHING STOPS YOU RUNNING THEM BY HAND — that is why this instruction matters. This paragraph
 * used to end "the composite is refused-by-guard for the same reason, so a raw call will not work
 * anyway", and no such guard exists: the destructive-SQL sentinel explicitly EXEMPTS the
 * sanctioned pair (they carry no SQL text in argv), and `_still_doing_it_the_hard_way` only
 * COUNTS a raw `drizzle-kit generate` for the adoption metric. Both commands can print a failure
 * and still exit 0, so run by hand they look like they worked; the composite is the only thing
 * that reads what they printed and tells you which step actually failed.
 *
 * Make ONE kind of schema change per call — mixing a rename into the same diff as an add is what
 * wakes the rename resolver.
 *
 * Never use drizzle-kit's `push` command: it mutates the database with no migration file, so the
 * next snapshot restores code that expects tables the database does not have.
 */

export {};
