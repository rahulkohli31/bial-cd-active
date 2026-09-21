# ADR-0025: Per-User Daily Token Quota and Usage Metering

## Context

The platform proxies billable model calls on a citizen's behalf. Usage must be bounded per user
per day so that no single account can run up unbounded cost or be used to abuse the platform.

## Decision

**Effective daily limit** is a per-user override, when one is set for that user, falling back to
a global default otherwise. Clearing a user's override returns them to the global default rather
than leaving them without a limit.

**Pre-request check:** before proxying a model call, the platform compares the user's usage so far
today against their effective limit. At or over the limit, the request is refused with a `429`
response carrying the limit and the amount already used, so the caller can render a clear,
specific message rather than a generic failure.

**Post-response metering:** after each response completes, the platform records that response's
token usage against the user for the current day, upserting into an existing day's total rather
than overwriting it. Usage is metered as **cost-weighted spend**, not a raw token sum: fresh input
and output tokens count at face value, tokens served from a cache read count at a fraction of
that, and tokens written to a cache count at the weight of whatever retention tier bought that
write — because those classes carry meaningfully different real cost, and treating them as
interchangeable would misstate what a session actually spent. Usage toward a citizen's own daily
cap counts only the spend attributable to their own build activity; other billable work the
platform does on an app's behalf, such as a pre-publish review, is recorded for its own
attribution but is never charged against the citizen's cap.

**Admin controls:** an administrator sets the global default and per-user overrides through
permission-gated, audited endpoints (ADR-0005). A change takes effect on the very next request —
there is no deploy or cache-invalidation step in between.

**Day boundary:** usage is bucketed by the Indian Standard Time calendar date (a fixed UTC+5:30
offset, with no daylight-saving adjustment), and a user's limit resets at the next IST midnight.
This is the one canonical timezone for the daily quota; every other clock in the system is
irrelevant to it.

**Posture:** enforcement is best-effort, not a hard transactional cap. A request that is already
in flight when the limit is crossed can push usage marginally over it before the next request is
refused. This is accepted: the goal is to bound cost within a reasonable margin, not to operate a
billing-grade ledger with exact real-time enforcement.

## Consequences

- API cost is bounded per user; no single account can run the platform's model spend up without
  bound.
- The `429` contract is specific and testable: callers get back both the limit and how much of it
  has been used, not just a refusal.
- Per-user limits can be tuned by an administrator without a deploy, and every change is audited.
- There is no platform-mediated spend on a generated app's own behalf to meter separately — the
  per-user quota described here is the only token quota the system enforces.

## Related

- ADR-0005 (the permission-gated, audited admin endpoints that set the global default and
  per-user overrides)
- ADR-0006 (the primary-key convention used by the usage-accounting records this quota reads and
  writes)
- ADR-0007 (authentication; this quota is enforced per authenticated request)
