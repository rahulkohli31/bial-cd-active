# ADR-0005: Role-Based Access Control, with Audit

## Context

Every privileged action needs access control that is enforced server-side and never
trusted from the frontend.

## Decision

There are two roles, computed on every request rather than stored:

- **citizen** — a platform user: owns and operates their own projects, apps, and
  conversations. This is the default for every authenticated user; nothing needs to grant
  it.
- **super_admin** — reviews and approves app submissions, sets usage quotas (ADR-0025),
  and runs platform operations: deploy, user management, system configuration. Approval
  authority and platform-operations authority are the same role.

A user's role is derived from their authenticated Entra email against a configured
allowlist of super-admin addresses. There is no `role` column and no permission table, so
there is nothing to drift, migrate, or leave stale when a user is offboarded. An email not
on the list is a citizen; the check fails closed.

Every privileged endpoint declares a dependency that gates the request to super-admins and
returns a plain, non-leaking 403 to anyone else. RBAC is enforced entirely at the backend;
the frontend may hide controls it knows the user cannot use, but the backend is the source
of truth.

Every gated action writes an audit-log row — who, what, when, and the target — inside the
same transaction as the action it accompanies, so a rolled-back action leaves no orphan
trail. The action vocabulary is an open string rather than a fixed enum, so a new gated
action does not require a schema migration to become auditable. Audit rows never carry the
resource's own content, only identifiers, counts, and structured metadata about the act
itself — recording who did what, never what the subject's data contained.

Cross-user reads by a super-admin (ADR-0004) flow through this same gate and are audited;
there is no silent scope bypass.

Because approval and platform-operations authority are fused into one role, a super-admin
can approve their own submissions — the system has no concept of a second, independent
approver. Rather than forbid this outright, which would leave a lone super-admin unable to
publish their own work, a self-approval is recorded distinguishably in the audit trail so
it can always be found and reviewed, instead of being indistinguishable from an ordinary
approval.

## Consequences

- The enforcement code is uniform and easy to test: one predicate and one dependency, with
  the allowlist injected so tests can override it without mutating shared configuration.
- Every sensitive action is attributable, and audit coverage is testable per gated path.
- Changing who is a super-admin is a configuration change and a restart, not a data
  migration — a deliberate trade for a single-tenant tool with a small, named group of
  admins.
- There is no separation of duties at the approval gate: one role holds both approval and
  platform-operations authority. This is accepted for a platform with a small admin group;
  the self-approval audit trail is the compensating control, not a substitute for a second
  approver.
