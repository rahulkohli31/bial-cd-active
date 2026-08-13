/**
 * app/api/bial-launch/route.ts — the deployed plane's half of the identity handshake
 * (issue #92, R10). Platform-authored; you should not need to edit this file.
 *
 * The platform's `/auth/launch` redirects the browser here after Entra sign-in,
 * carrying a short-lived, single-purpose exchange CODE — never the identity
 * assertion itself (R10: "assertion never in the URL, browser history, referrer
 * headers, or server access logs"). This route trades that code for the real
 * assertion over a server-to-server call, sets it as a first-party cookie on THIS
 * app's own origin (safe here: a deployed app is never framed, unlike the preview
 * plane, so none of R9's cross-site cookie restrictions apply), and redirects to
 * the originally requested path with the code gone from the URL.
 */

import { NextResponse, type NextRequest } from "next/server";

const ASSERTION_COOKIE = "bial_identity";

export async function GET(request: NextRequest): Promise<NextResponse> {
  const code = request.nextUrl.searchParams.get("bial_code");
  const next = request.nextUrl.searchParams.get("bial_next") || "/";
  // Same-origin-relative only — mirrors the platform's own `_safe_next_path` guard,
  // defense in depth against an open redirect even though the platform already
  // validated this once when it minted the code.
  const safeNext = next.startsWith("/") && !next.startsWith("//") ? next : "/";

  if (!code) {
    return NextResponse.redirect(new URL(safeNext, request.url));
  }

  const portalOrigin = process.env.BIAL_PORTAL_ORIGIN;
  if (!portalOrigin) {
    return NextResponse.redirect(new URL(safeNext, request.url));
  }

  let assertion: string | null = null;
  try {
    const resp = await fetch(new URL("/api/v1/auth/app-assertion/exchange", portalOrigin), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
      cache: "no-store",
    });
    if (resp.ok) {
      ({ assertion } = await resp.json());
    }
  } catch {
    // Network/parse failure — fall through with no assertion (fail closed: the
    // visitor lands on `next` unauthenticated, exactly as if they had no code).
  }

  const response = NextResponse.redirect(new URL(safeNext, request.url));
  if (assertion) {
    response.cookies.set(ASSERTION_COOKIE, assertion, {
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      path: "/",
      // No explicit maxAge/expires: a session cookie. The assertion inside has its
      // own hard `exp` (R16) that the accessor checks on every read regardless.
    });
  }
  return response;
}
