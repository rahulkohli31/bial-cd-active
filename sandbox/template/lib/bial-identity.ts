/**
 * bial-identity.ts — the ONE named accessor for "who is using this app" (issue #92,
 * R11, R12, R13). Call it in the server-side data path of EVERY page and in EVERY
 * Server Action — a page's own check does not cover the Server Actions it defines,
 * and framework middleware is not a security boundary on its own.
 *
 * The assertion is verified HERE, against the platform's published public key
 * (`GET /auth/jwks`) — never trust a header/cookie value positionally, and never
 * add another way to prove identity. This file behaves IDENTICALLY regardless of
 * which plane (preview or deployed) delivered the assertion (R13): it reads
 * whichever carrier is present (an explicit token, a request header, or a cookie)
 * and verifies the same way every time. The two planes differ only in HOW the token
 * gets into one of those carriers — that difference lives in platform-authored
 * files (`bial-identity-client.tsx` for preview, the launch middleware for
 * deployed), never here and never in your feature code.
 *
 * SERVER-ONLY. Never import this from a Client Component — it reads `next/headers`.
 */

import { cookies, headers } from "next/headers";
import { createRemoteJWKSet, jwtVerify } from "jose";

/** The verified identity of the person using this app right now, or nothing. */
export type BialIdentity = {
  /** The Entra object id — the ONLY stable key (R5). Never key your own tables on email. */
  entraObjectId: string;
  /** For display/contact use only — NOT a stable key (an address can change). */
  email: string;
  displayName: string | null;
};

// The header a same-origin client request carries the assertion on (set by
// bial-identity-client.tsx's fetch wrapper, preview plane) and the cookie the
// deployed-plane launch flow sets (first-party, standalone — not subject to the
// cross-site cookie restriction that rules out a cookie for the FRAMED preview, R9).
const ASSERTION_HEADER = "x-bial-assertion";
const ASSERTION_COOKIE = "bial_identity";

let jwks: ReturnType<typeof createRemoteJWKSet> | null = null;

function jwksForPortal(): ReturnType<typeof createRemoteJWKSet> {
  // Built once and cached (module scope survives across requests, reset on HMR
  // reload same as db/index.ts's pool) — createRemoteJWKSet itself already caches
  // the fetched key set, this just avoids re-constructing the fetcher per call.
  if (jwks) return jwks;
  const portalOrigin = process.env.BIAL_PORTAL_ORIGIN;
  if (!portalOrigin) {
    throw new Error("BIAL_PORTAL_ORIGIN is not set — cannot resolve the platform's JWKS endpoint.");
  }
  jwks = createRemoteJWKSet(new URL("/api/v1/auth/jwks", portalOrigin));
  return jwks;
}

/**
 * The verified identity of the person using this app, or `null` when there is
 * none — never throws for an absent/invalid assertion (fail closed, not fail loud).
 *
 * `assertionOverride` exists for Server Actions: a Server Action has no ambient
 * access to a header your OWN client code attached to some other fetch, so the
 * client passes the in-memory assertion (`useBialAssertion()` from
 * `bial-identity-client.tsx`) as an explicit argument. Route Handlers and Server
 * Components need no override — they read the header/cookie directly.
 */
export async function getBialIdentity(assertionOverride?: string): Promise<BialIdentity | null> {
  const appId = process.env.BIAL_APP_ID;
  if (!appId) return null; // no app identity configured -> nothing to bind the assertion to

  let assertion = assertionOverride;
  if (!assertion) {
    const hdrs = await headers();
    assertion = hdrs.get(ASSERTION_HEADER) ?? undefined;
  }
  if (!assertion) {
    const jar = await cookies();
    assertion = jar.get(ASSERTION_COOKIE)?.value;
  }
  if (!assertion) return null;

  try {
    const { payload } = await jwtVerify(assertion, jwksForPortal(), { audience: appId });
    if (typeof payload.sub !== "string" || typeof payload.email !== "string") return null;
    const name = payload.name;
    return {
      entraObjectId: payload.sub,
      email: payload.email,
      displayName: typeof name === "string" ? name : null,
    };
  } catch {
    // ANY verification failure (bad signature, expired, wrong audience, malformed)
    // is "no identity" — never partial trust, never a thrown error the caller must
    // remember to catch.
    return null;
  }
}

/**
 * Build the URL to send a signed-out visitor to (issue #92, R10). There is no
 * platform-enforced gate (R1) — YOUR code decides an app needs a sign-in and
 * redirects here, typically from a Route Handler or a Server Component:
 *
 *   const identity = await getBialIdentity();
 *   if (!identity) redirect(getBialLaunchUrl("/records/42"));
 *
 * `nextPath` is preserved through sign-in (AE4) — the visitor lands back exactly
 * where they were headed, not the app root. Only ever a same-origin path; do not
 * pass a full URL here.
 */
export function getBialLaunchUrl(nextPath: string): string {
  const portalOrigin = process.env.BIAL_PORTAL_ORIGIN;
  const appId = process.env.BIAL_APP_ID;
  if (!portalOrigin || !appId) {
    throw new Error("BIAL_PORTAL_ORIGIN/BIAL_APP_ID are not set — cannot build a launch URL.");
  }
  const url = new URL("/api/v1/auth/launch", portalOrigin);
  url.searchParams.set("app_id", appId);
  url.searchParams.set("next", nextPath);
  return url.toString();
}
