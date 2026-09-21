# ADR-0007: Authentication — Backend-Owned Entra ID OIDC, Internal JWT Session

## Context

The platform is single-tenant: every user signs in with their existing Microsoft Entra ID
corporate identity, and there is no active-org concept to carry in the session.

## Decision

**Identity is Microsoft Entra ID.** The backend itself runs the OIDC Authorization Code
flow with PKCE, against the tenant-specific discovery document — never the generic
`common`/`organizations` issuer, whose templated `iss` claim would defeat an exact match.
It validates the returned identity fail-closed: the object id and subject claims must be
present and the token's tenant claim must match the configured tenant, or no session is
created. The Entra tokens are discarded once validated; the backend mints and trusts only
its own session. There is no trusted proxy-asserted identity — the backend is the OIDC
relying party.

### Identity and provisioning

On first valid sign-in the backend upserts the user keyed by the stable Entra Object ID,
never by email — a provider-asserted email is not proof of ownership. Email and display
name are stored as profile data, not as the identity key.

The user's role (ADR-0005) is derived independently on every request from the live user
record; it is not carried in the session token, so a role change takes effect on the very
next request rather than waiting for a session to expire.

### Session

- The internal access token is a signed JWT with a short lifetime, carrying only what
  authentication needs: the user id and a token-version counter — no email or role claim,
  since either would go stale. The signing algorithm is pinned on decode, so a token
  forged with no algorithm, or a different one, is rejected before its claims are read.
- It is stored in an HTTP-only, secure, same-site cookie — never in browser storage,
  which is reachable by any script running on the page.
- The refresh token is an opaque, cryptographically-random value (ADR-0006), stored only
  as its hash, never as the raw token. It rotates on every use: each refresh consumes the
  presented token and issues a successor in the same family, and presenting an
  already-used or revoked token is treated as reuse and revokes the entire family — fail
  closed, with no grace window for a replay. The whole family is additionally bound to a
  hard absolute lifetime, independent of activity, past which no rotation succeeds and a
  fresh sign-in is required.
- A token-version counter on the user row is the instant-revocation lever: bumping it, on
  logout or a forced deactivation, invalidates every outstanding session and refresh
  token at once.
- State-changing requests are protected by a signed, session-bound CSRF double-submit
  token: a value in a non-HTTP-only cookie must be echoed in a request header, and the
  token itself is an HMAC over the user id and token version, so it is unforgeable without
  the session secret and dies the moment the session is revoked.

## Consequences

- The backend is the authentication source of truth, independent of any frontend
  framework; it can serve every client uniformly.
- HTTP-only cookies plus hashed, rotating, version-revocable refresh tokens give real
  server-side revocation with minimal stored state.
- No tenancy machinery lives in the token — the single-tenant model keeps the session
  lean.
- Because refresh rotation is an atomic claim on first use, a replayed refresh token is
  detected and its whole family revoked rather than silently accepted.
- Authentication endpoints are not currently covered by the platform's rate limiter;
  brute-force and credential-stuffing protection at that layer is a known gap.
