# ADR-0006: UUIDv7 for Primary Keys; Secure-Random for Tokens

## Context

Two distinct identifier needs are easy to conflate:

1. **Internal database identity** — primary keys, foreign keys, resource ids that appear
   in API paths. These want to be sortable, for index locality and natural chronological
   ordering, and non-enumerable.
2. **User-facing secrets** — refresh tokens, shareable links, API credentials. These want
   to be unguessable; sortability and any embedded structure are liabilities, not
   features.

The mistake to avoid is reusing a UUID as a secret: a UUIDv7 embeds a timestamp and is not
a cryptographic secret, so displaying one where a secret is intended leaks structure and
invites enumeration.

## Decision

**UUIDv7 for database primary keys**, generated application-side and backed by
PostgreSQL's own native UUIDv7 generation as the column's server default, so a raw SQL
insert also gets a time-sortable key.

- Time-sortable: ordering by id is chronological without a separate timestamp index.
- B-tree friendly: sequential inserts keep indexes compact, unlike random UUIDs.
- Non-enumerable and URL-safe: usable directly in a resource path without leaking counts
  or internal state.
- UUIDv7 is the default for synthetic surrogate keys, not a mandate to wrap every natural
  key — an external identifier, such as an Entra object id, stays itself.

**Cryptographically-secure random values for anything that functions as a secret**: a
standard-library secure token generator for refresh tokens, shareable app keys, CSRF
nonces, and generated-database passwords. A raw UUID is never displayed as a secret.
Long-lived secrets, such as refresh tokens, are stored only as a hash, never as the raw
value (ADR-0007).

The line: UUIDv7 is internal, sortable identity; secure-random is a user-facing secret.

## Consequences

- Compact, sortable, non-enumerable primary keys across the schema; foreign keys and
  joins are ordinary UUID operations.
- A UUID key costs twice the storage of a bigint, accepted for the sortability and
  security benefit.
- Secrets carry no timestamp or structure to attack; rotation and revocation are
  independent of identity.
- One question settles which scheme applies: is this value ever shown to a user as a
  credential? If yes, it is generated as a secret, never as a UUID.
