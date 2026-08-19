/**
 * bial-config.ts — the shape of the runtime identity the platform injects, plus the
 * `window.__BIAL_CONFIG` declaration the error-capture shim publishes it through.
 *
 * This is types + one global declaration only: no fetching, no client, no I/O. The app's DATA
 * lives in its own PostgreSQL database now (ADR-0028) and is reached with Drizzle from server
 * code (`db/index.ts`) — the browser never gets a data credential, so nothing here is secret.
 *
 * Read the real values server-side from `process.env` (see `.env.example` for the manifest).
 * `BIAL_DATABASE_URL` is deliberately ABSENT from this type: it is a server-only secret and
 * publishing it to `window` would hand every visitor the app's database.
 */

export type BialConfig = {
  /** `BIAL_APP_ID` — this app's id. */
  appId?: string;
  /** `BIAL_PORTAL_ORIGIN` — the portal origin the error relay posts to (never `'*'`). */
  portalOrigin?: string;
};

declare global {
  interface Window {
    __BIAL_CONFIG?: BialConfig;
  }
}

/**
 * The portal origin to `postMessage` against — NEVER `'*'` (C8 §3). Shared by every framed-postMessage
 * channel (the error-capture relay, the issue #92 identity handshake) so there is exactly one place
 * that resolves it. Prefers the injected config; falls back to `document.referrer`'s origin for the
 * brief window before `window.__BIAL_CONFIG` has been set (see error-capture.tsx's render-phase note).
 * Client-side only — returns `null` outside a browser or when no origin can be determined (fail closed).
 */
export function resolveBialPortalOrigin(): string | null {
  const injected = typeof window !== "undefined" ? window.__BIAL_CONFIG?.portalOrigin : undefined;
  if (injected) return injected;
  if (typeof document !== "undefined" && document.referrer) {
    try {
      return new URL(document.referrer).origin;
    } catch {
      return null;
    }
  }
  return null;
}
