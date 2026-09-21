# ADR-0008: Native PostgreSQL Enum Types for Closed-Set Columns

## Context

Several columns hold a fixed, closed set of values — an app's lifecycle status
(`draft`/`pending`/`approved`/`rejected`/`disabled`), a deployment's outcome
(`running`/`succeeded`/`failed`), and others like them. Enforcing that only at the
application layer gives Postgres no opinion on what the column may hold: a wrong-case
value or an off-list string is accepted silently, and the bug surfaces later as a
confusing read-time failure rather than an error the writer sees immediately — whether
the writer is application code, a seed script, a backfill, or generated code.

Three ways to close the set:

1. Application-only validation — no database guarantee.
2. A `VARCHAR` column with a `CHECK` constraint — database-enforced and easy to widen,
   but stores full strings and is a weaker type signal.
3. A native PostgreSQL `ENUM` type — the strongest typing available, case-sensitive, and
   compact in storage.

## Decision

Closed-set columns use native PostgreSQL `ENUM` types, enforced at the database, the
ORM, and the API schema layer.

- **ORM:** the column is declared with `sa.Enum(SomeEnum, name="...",
  values_callable=lambda e: [m.value for m in e])`. The `values_callable` argument is
  mandatory — without it SQLAlchemy stores the enum member's *name* (`APPROVED`) rather
  than its lowercase *value* (`approved`), which is the string the rest of the codebase
  reads and writes everywhere else.
- **Migrations:** each migration defines its enum with literal labels rather than
  importing the application's enum class, so the migration file stays a self-contained
  snapshot of what it created, independent of later code changes. The model-side type is
  declared with `create_type=False`; the migration owns `CREATE TYPE` in `upgrade()` and
  `DROP TYPE` in `downgrade()` explicitly, because dropping a table does not drop its
  type.
- **Schemas:** request and response models use the enum type directly. Because the enum
  is a `StrEnum`, it serializes to its plain string value, so the JSON contract is
  identical to what a plain string column would produce.
- **Queries:** a statement that compares one of these columns goes through the mapped ORM
  column, so the driver is told the parameter's type. The database driver will not cast a
  string to an enum type implicitly, so a predicate built from a bare string — a literal
  `WHERE status = :value` against an unmapped column, for instance — fails at execution
  rather than at review. Comparing through the mapped column is the habit that avoids it;
  where a raw statement is unavoidable, the cast is written explicitly. This is the part of
  this decision easiest to skip and cheapest to honour.

Not every closed-set-looking column becomes an enum. Two stay plain strings,
deliberately: the audit log's `action` and `resource_type` columns, because the audit
log is polymorphic by design and records an open, growing vocabulary across every
domain — a new gated action must not require a schema migration just to become
loggable; and a deployment's `step` field (the pipeline phase, e.g. "packing",
"building"), because adding a display phase is a much smaller and more frequent change
than adding a real status, and tying it to the same migration discipline as `status`
would only slow it down for no safety benefit.

## Consequences

- Postgres rejects an invalid or wrong-case value at write time regardless of who is
  writing — application code, a seed script, a backfill, or AI-generated code — closing
  the class of bug where a bad value is silently accepted and only surfaces on read.
- Storage is compact, and the schema is self-documenting: the set of valid values lives
  in the database, not only in application code or in reviewers' heads.
- Adding a new value to an existing enum needs an `ALTER TYPE ... ADD VALUE` migration,
  and PostgreSQL does not allow adding and using a new label within the same
  transaction. Every new status or category ships with a migration rather than a
  code-only change — accepted as the cost of the stronger guarantee.

## Related

- ADR-0013 (the migration tooling that owns each enum type's lifecycle)
