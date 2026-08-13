"use client";

/**
 * bial-identity-client.tsx — the PREVIEW plane's half of the identity handshake
 * (issue #92, R7, R8, R9). Mounted once in app/layout.tsx, exactly like
 * BialErrorCapture. Platform-authored; you should not need to edit this file.
 *
 * The frame requests identity over `postMessage` (never an Entra redirect inside
 * the frame — the sandboxed iframe cannot reach login.microsoftonline.com and must
 * not try, C8); the portal relays the request to the control-plane (using the
 * PORTAL's own session — the citizen developer viewing the builder), mints an
 * assertion scoped to THIS app and the "preview" plane, and posts it back.
 *
 * R9: the assertion is held ONLY in memory (a module-level variable, not
 * localStorage/sessionStorage/a cookie) and re-requested over the same channel
 * before it expires — a cross-site framed cookie is refused outright by Safari and
 * partitioned by Firefox, so a cookie was never an option here. Renewal costs one
 * message and never reloads the app.
 *
 * Deployed apps run standalone (never framed), so this module's `postMessage`
 * handshake simply never fires there — the deployed plane's identity arrives via a
 * first-party cookie set by the platform's launch flow instead (see the accessor's
 * own docstring, `bial-identity.ts`). Your app code never checks which plane it is
 * running in either way (R13).
 */

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { resolveBialPortalOrigin } from "@/lib/bial-config";

const REQUEST_TYPE = "bial:identity:request";
const ASSERTION_TYPE = "bial:identity:assertion";
const ASSERTION_HEADER = "x-bial-assertion";

// In-memory only (R9) — module scope so it survives a re-render but NOT a full page
// reload (a reload re-requests from scratch, which is correct: nothing persists).
let currentAssertion: string | null = null;
let fetchPatched = false;

/** Read the unverified `exp` claim to schedule renewal — NEVER used to trust the
 * token's contents; the client does not (and should not be able to) verify the
 * signature. The server-side accessor is the only place trust is established. */
function unverifiedExpiry(token: string): number | null {
  try {
    const [, payloadB64] = token.split(".");
    const json = JSON.parse(atob(payloadB64.replace(/-/g, "+").replace(/_/g, "/")));
    return typeof json.exp === "number" ? json.exp : null;
  } catch {
    return null;
  }
}

/** Attach the current in-memory assertion to same-origin requests exactly once
 * (idempotent — layout.tsx can re-render without double-patching). Covers plain
 * `fetch()` calls from your Route Handlers AND Server Action dispatches, both of
 * which Next.js sends over `fetch` under the hood. */
function ensureFetchIsPatched(): void {
  if (fetchPatched || typeof window === "undefined") return;
  fetchPatched = true;
  const original = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    if (!currentAssertion) return original(input, init);
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const isSameOrigin = url.startsWith("/") || url.startsWith(window.location.origin);
    if (!isSameOrigin) return original(input, init);
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    headers.set(ASSERTION_HEADER, currentAssertion);
    return original(input, { ...init, headers });
  };
}

const BialIdentityContext = createContext<string | null>(null);

/** The current in-memory assertion — pass this explicitly into a Server Action call
 * (a Server Action has no ambient access to a header your client code set on some
 * unrelated fetch). `null` before the handshake completes or when not framed. */
export function useBialAssertion(): string | null {
  return useContext(BialIdentityContext);
}

export function BialIdentityProvider({ children }: { children: ReactNode }) {
  const [assertion, setAssertion] = useState<string | null>(null);
  const renewalTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    ensureFetchIsPatched();

    const framed = typeof window !== "undefined" && window.self !== window.top;
    if (!framed) return; // deployed plane: nothing to request, the accessor reads the launch cookie

    const appId = window.__BIAL_CONFIG?.appId;

    function requestAssertion(): void {
      const portalOrigin = resolveBialPortalOrigin();
      if (!portalOrigin || !appId) return; // fail closed — never post to '*' or with no app id
      window.parent.postMessage({ type: REQUEST_TYPE, appId }, portalOrigin);
    }

    function onMessage(event: MessageEvent): void {
      const portalOrigin = resolveBialPortalOrigin();
      if (!portalOrigin || event.origin !== portalOrigin) return; // origin-restricted (R7)
      if (event.data?.type !== ASSERTION_TYPE || typeof event.data.assertion !== "string") return;

      const token: string = event.data.assertion;
      currentAssertion = token;
      setAssertion(token);

      if (renewalTimer.current) clearTimeout(renewalTimer.current);
      const exp = unverifiedExpiry(token);
      if (exp) {
        // Renew at 80% of the remaining lifetime, floored so a very short TTL still
        // renews at least a couple of seconds ahead of expiry rather than at it.
        const msRemaining = exp * 1000 - Date.now();
        const renewInMs = Math.max(2000, Math.floor(msRemaining * 0.8));
        renewalTimer.current = setTimeout(requestAssertion, renewInMs);
      }
    }

    window.addEventListener("message", onMessage);
    requestAssertion();

    return () => {
      window.removeEventListener("message", onMessage);
      if (renewalTimer.current) clearTimeout(renewalTimer.current);
    };
  }, []);

  return <BialIdentityContext.Provider value={assertion}>{children}</BialIdentityContext.Provider>;
}
