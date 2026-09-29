# ADR-0032: The Control Plane Reads the Directory to Share With People Who Have Not Signed In

## Context

An owner shares a project with a colleague by picking them from a search. That search covered only
the platform's own users, and a user exists only after their first sign-in. A colleague who had
never opened the platform could not be found, so the owner had to ask them to sign in once and come
back to share afterwards.

Every employee already exists in the organisation's directory. Sign-in identifies a person by their
directory object id, not by email, so a user record created from the directory is the same record
that person's first sign-in resolves to.

Sign-in cannot provide the lookup. Its registration is a public client with no secret, and it only
ever sees the person signing in. The only credential able to read other people from the directory
is the control plane's own managed identity, and granting it that permission is the tenant's
decision.

## Decision

**The control plane reads the directory as its own identity, with the narrowest application
permission that allows it: reading users' basic profiles.** It reads names, email addresses and
object ids, and it never writes to the directory. The identity is the control plane's own, never
the one handed to generated applications, so no generated code can make this call.

**Our own users are searched first, and the directory only fills what they leave.** A directory
match that is already a user here is dropped, as are guests. Results carry either a user id or a
directory id, and whether the person has ever signed in.

**Picking someone from the directory creates their user record and shares in one transaction.**
The request carries only the object id. The control plane reads the person back from the directory
rather than trusting anything the browser sent. It inserts the record as not yet signed in and
never updates an existing one. Creating the record is audited separately from the share
(ADR-0005). The person's first sign-in resolves to that record, and the share is already there.

**The directory is optional at runtime.** There is no setting. Every failure is logged and reads as
no directory results: a missing permission, an unreachable directory, a slow answer or a malformed
one. A host with no managed identity skips the directory entirely. A hard time budget bounds how
long a search can wait. Search falls back to exactly what it did before, and a pick that cannot
reach the directory is refused with a plain message.

**People who have never signed in are visible as such.** A marker on each user record, set by
sign-in and never cleared, lets the owner's share list and the administrators' user list say
"Not signed in yet".

## Consequences

- Owners can share with anyone in the organisation. The share is waiting when the colleague first
  signs in, and nobody is told until then: the owner tells them.
- Any signed-in user can look up colleagues by the start of a name or email. They see only the name
  and the part of the address before the @, a page at a time. The search rate limit is the only
  friction, the same as the address book every employee already has.
- The basic-profile permission cannot tell an active employee from a disabled account or a shared
  mailbox, so those can appear. Sharing with one of them is inert, because nobody signs in as them.
- A pick needs no prior search: a caller who knows an object id can create a record for that
  person. The share route's rate limit and the audit trail bound this.
- Records created this way are not reconciled with the directory afterwards. Sign-in refreshes the
  profile, and nothing removes records whose shares were revoked.
- Until the tenant grants the permission, search behaves as it did before, and each attempt logs a
  warning.

## Rejected alternatives

- **Delegated access through the signed-in user's own token.** Sign-in discards the directory's
  tokens and is a public client with no refresh token, so keeping a token for lookups would reverse
  both decisions for the sake of one search.
- **The full-profile permission.** It could hide disabled accounts and guests on the directory's
  side, but it reads every profile in full. Disabled accounts are a small share of the directory,
  and guests are filtered here.
- **A setting to switch the directory on.** Failing soft already gives the off state. A switch
  would be one more value to keep right in every environment.
