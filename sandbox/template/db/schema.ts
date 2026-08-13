/**
 * db/schema.ts — this app's database schema, in Drizzle.
 *
 * THIS IS A STARTING POINT, NOT A FROZEN MODULE. Rename these tables, reshape the columns, add
 * your own, or delete every table below and write the schema your app actually needs. Nothing
 * here is required by the platform — the platform owns the database's lifecycle, the app owns
 * everything inside it (ADR-0028).
 *
 * The workflow after ANY edit to this file:
 *   1. `npx drizzle-kit generate`   → writes a new versioned .sql file under ./drizzle
 *   2. `npm run db:migrate`         → applies the pending migrations to the database
 * Never use drizzle-kit's `push` command: it mutates the database with no migration file, so the
 * next snapshot restores code that expects tables the database does not have.
 */

import { boolean, index, jsonb, pgEnum, pgTable, text, timestamp, uuid } from "drizzle-orm/pg-core";

/**
 * A tiny append-only trail of who did what. Useful in almost every internal app, and a working
 * reference for the column types you will reach for most (uuid PK, timezone-aware timestamps,
 * a JSONB bag for the free-form bits). Drop it if your app has no use for it.
 */
export const auditEvents = pgTable(
  "audit_events",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    occurredAt: timestamp("occurred_at", { withTimezone: true }).notNull().defaultNow(),
    /** Who acted — a name or email the app already knows; there is no user table by default. */
    actor: text("actor"),
    /** What happened, e.g. "item.created". */
    action: text("action").notNull(),
    /** What it happened to — usually another row's id, kept as text so it can point anywhere. */
    subject: text("subject"),
    /** Anything else worth keeping, unshaped. */
    detail: jsonb("detail").$type<Record<string, unknown>>(),
  },
  (table) => [index("audit_events_occurred_at_idx").on(table.occurredAt)],
);

export type AuditEvent = typeof auditEvents.$inferSelect;
export type NewAuditEvent = typeof auditEvents.$inferInsert;

/**
 * The SANCTIONED ROSTER SHAPE (issue #92, R15, R19) — only present if your app has a sign-in.
 * A roster is expected, not tolerated: it differs from a conventional users table in exactly two
 * ways — nothing in it is checkable (no password, no credential, no session token), and rows
 * appear from a VERIFIED identity (`getBialIdentity()`, `@/lib/bial-identity`) rather than from a
 * sign-up form your app never has. Extend it with your own columns (role, department, whatever
 * your app needs) — the shape here is a starting point, not a frozen module.
 *
 * `entraObjectId` is the ONLY identity key (R5) — never key on `email`, which can change when
 * someone's address does. `email`/`displayName` are cached here purely for display and search;
 * re-derive them from the verified assertion on every sign-in rather than trusting a stale copy
 * for anything that matters.
 */
export const appUsers = pgTable("app_users", {
  entraObjectId: text("entra_object_id").primaryKey(),
  email: text("email").notNull(),
  displayName: text("display_name"),
  /** An example role flag — replace with whatever roles/permissions your app actually needs. */
  isApprover: boolean("is_approver").notNull().default(false),
  firstSeenAt: timestamp("first_seen_at", { withTimezone: true }).notNull().defaultNow(),
  lastSeenAt: timestamp("last_seen_at", { withTimezone: true }).notNull().defaultNow(),
});

export type AppUser = typeof appUsers.$inferSelect;
export type NewAppUser = typeof appUsers.$inferInsert;

/**
 * A worked example of the shape most app tables want: a native enum for a closed set of states,
 * created/updated timestamps, and an index on the column the list screen sorts by. Copy the
 * pattern for your real tables — and delete `items` itself, it is not your feature.
 *
 * To point one table at another, add a reference column:
 *   itemId: uuid("item_id").notNull().references(() => items.id, { onDelete: "cascade" })
 */
export const itemStatus = pgEnum("item_status", ["open", "in_progress", "done"]);

export const items = pgTable(
  "items",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    title: text("title").notNull(),
    status: itemStatus("status").notNull().default("open"),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (table) => [index("items_created_at_idx").on(table.createdAt)],
);

export type Item = typeof items.$inferSelect;
export type NewItem = typeof items.$inferInsert;
