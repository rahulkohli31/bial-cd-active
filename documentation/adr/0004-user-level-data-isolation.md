# ADR-0004: User-Level Data Isolation (No Multi-Tenancy)

## Context

The platform serves a single organization: every user authenticates against one Azure
Entra ID tenant, and there is no multi-org or multi-tenant dimension in the product. Data
belonging to one user — conversations, attachments, generated app code — must never be
visible to another.

The isolation boundary is therefore the user, not an organization. A second, distinct
boundary exists for generated apps: each project gets its own database and scoped database
role (ADR-0028), so one generated app can never read another project's data.

## Decision

All control-plane queries are scoped by the owning `user_id`. There is no `org_id`
anywhere in the schema.

- Every model that holds user data carries a `user_id` foreign key, or reaches its owner
  through a join when the table's ownership anchor is a related row rather than the row
  itself. Every read and write filters on the current user's id; no query may return
  another user's rows.
- The scoping predicate is load-bearing security, not a style choice: a dropped
  `WHERE user_id = ...` clause is a cross-user data leak, and it is treated as one in
  review.
- Object-store keys are owner-scoped with literal prefixes: a user's attachments live
  under `att/{user_id}/{attachment_id}`, and a generated app's files live under
  `apps/{app_id}/{file_id}`. Conversations are control-plane database rows, not
  object-store entries.
- Super-admins (ADR-0005) may legitimately read across users. That access always goes
  through an explicit, audited permission check — never by omitting the scope.

Generated apps additionally get per-project database isolation: one PostgreSQL database
and one scoped role per project (ADR-0028), provisioned when the project is created. That
is a separate boundary from control-plane user scoping, and it applies to the app's own
runtime data — the schema and content inside that database belong to the app, not the
platform.

## Consequences

- A single control-plane database keeps operations simple; isolation lives entirely in the
  query layer.
- A missing scope clause leaks data. This is mitigated by review, and could be further
  hardened with PostgreSQL Row-Level Security on user-owned tables so the database itself
  enforces the predicate — not currently enabled.
- Cross-user access is always an explicit, role-gated, audited action, never an accident.
- No tenancy machinery to build or operate; the model matches a single-organization
  product exactly.
