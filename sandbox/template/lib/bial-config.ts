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
  /**
   * `BIAL_ENTRA_TENANT_ID` — the shared Entra app registration's tenant id. Reference-only:
   * the platform performs sign-in in front of this app, not the app itself — never use this
   * to build your own OAuth/OIDC flow.
   */
  entraTenantId?: string;
  /**
   * `BIAL_ENTRA_CLIENT_ID` — the shared Entra app registration's client id (a public PKCE
   * client — no secret exists to inject). Reference-only, same rule as `entraTenantId`.
   */
  entraClientId?: string;
};

declare global {
  interface Window {
    __BIAL_CONFIG?: BialConfig;
  }
}
