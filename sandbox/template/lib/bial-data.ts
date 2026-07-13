/**
 * bial-data.ts — THE single swappable data-access module (C6 §3, decision D4).
 *
 * This is an HTTP client to the EXISTING platform data-service (the interim `data_records`
 * plane), NOT Drizzle / Prisma / any ORM and NOT a direct DB client. It reproduces the wire
 * shape of `backend/src/services/appserving/assets/bial_data_client.js` verbatim — the same
 * shape the deployed runner already speaks — retargeted at `/v1/apps/{appId}/records`.
 *
 * Swapping the interim data-service for the LAST-stage per-app database later means replacing
 * THIS ONE FILE and nothing else (C6). The generated feature code only ever imports `bialData`.
 *
 * Config source (C6 §4 / C9): the app's identity + credential + base URL arrive as the three
 * env-vars BIAL_APP_ID / BIAL_APP_CREDENTIAL / BIAL_DATA_BASE_URL — chosen so none ends in
 * _TOKEN/_SECRET/_KEY and they survive the supervisor's child-env scrub (D5). On the server
 * (`next dev`) they are read from `process.env`; in the browser they arrive via the
 * `window.__BIAL_CONFIG` bootstrap that `app/layout.tsx` injects at request time — mirroring
 * the deployed runner's `window.__BIAL_CONFIG`, so the CRUD screen fetches the data-service
 * directly with `X-App-Key` (C9 accepts this bounded, app_id-scoped credential exposure §5).
 *
 * IMPORTANT base-URL note: BIAL_DATA_BASE_URL MUST already include the `/v1` prefix, because
 * the path is built by raw concat — `baseUrl + '/apps/' + appId + '/records'` — landing on the
 * mounted route `/v1/apps/{app_id}/records`. No trailing slash on the base URL.
 */

// ---- the server projection the data-service returns (never leaks app_id/bytes/search_text)
export type RecordOut = {
  id: string; // uuid
  collection: string;
  data: Record<string, unknown>;
  createdBy: string | null; // uuid, camelCased on the wire (CamelModel)
  createdInDraft: boolean;
  createdAt: string; // ISO-8601
  updatedAt: string; // ISO-8601
};

export type SearchResult = {
  items: RecordOut[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
};

export interface BialData {
  // CRUD — names mirror bial_data_client.js exactly (the builder-prompt vocabulary).
  save(collection: string, data: Record<string, unknown>): Promise<RecordOut>; // create → POST, 201 bare record
  list(collection?: string, opts?: { limit?: number }): Promise<RecordOut[]>; // list → GET → {records}
  query(
    collection: string | undefined,
    opts?: {
      q?: string;
      page?: number;
      pageSize?: number;
      sort?: string;
      order?: "asc" | "desc";
      filter?: Record<string, unknown>;
    },
  ): Promise<SearchResult>; // search → GET /search
  distinct(collection: string | undefined, field: string): Promise<unknown[]>; // GET /distinct → {values}
  get(collection: string | undefined, id: string): Promise<RecordOut | null>; // GET /{id} → {record}
  update(
    collection: string | undefined,
    id: string,
    data: Record<string, unknown>,
  ): Promise<RecordOut | null>; // PATCH /{id} → {record}
  remove(collection: string | undefined, id: string): Promise<{ ok: true } | null>; // DELETE /{id} → {ok:true}, 200

  // convenience (mirrors the JS client; kept so seed/idempotent flows port unchanged)
  seedFromUpload(
    collection: string,
    rows: Record<string, unknown>[],
    opts?: { dedupeKey?: string },
  ): Promise<{ seeded: number; skipped: boolean }>;
}

export type BialConfig = {
  appId?: string;
  appKey?: string;
  baseUrl?: string;
  portalOrigin?: string;
};

declare global {
  interface Window {
    __BIAL_CONFIG?: BialConfig;
    __BIAL_TOKEN?: string | null;
  }
}

const NOT_READY = "The data service is still starting up — please try again in a moment.";

/** Resolve config lazily per call (matches the JS client's `getConfig()`): browser reads the
 *  injected `window.__BIAL_CONFIG`; server reads `process.env`. Never throws — an unready
 *  config makes reads resolve empty and writes reject, so a Save is never silently dropped. */
function getConfig(): BialConfig {
  if (typeof window !== "undefined" && window.__BIAL_CONFIG) {
    return window.__BIAL_CONFIG;
  }
  return {
    appId: process.env.BIAL_APP_ID,
    appKey: process.env.BIAL_APP_CREDENTIAL,
    baseUrl: process.env.BIAL_DATA_BASE_URL,
    portalOrigin: process.env.BIAL_PORTAL_ORIGIN,
  };
}

function ready(cfg: BialConfig): cfg is Required<Pick<BialConfig, "appId" | "appKey" | "baseUrl">> & BialConfig {
  return Boolean(cfg.appId && cfg.appKey && cfg.baseUrl);
}

function recordsUrl(cfg: Required<Pick<BialConfig, "appId" | "baseUrl">>, suffix = ""): string {
  // Verbatim from bial_data_client.js — raw concat; baseUrl already carries the /v1 prefix.
  return cfg.baseUrl + "/apps/" + cfg.appId + "/records" + suffix;
}

function baseHeaders(cfg: Required<Pick<BialConfig, "appKey">>): Record<string, string> {
  const headers: Record<string, string> = { "X-App-Key": cfg.appKey };
  // The optional Bearer leg is carried for parity with the deployed runner; unused in the
  // interim (the builder app is login_required=false, so X-App-Key alone authorizes CRUD).
  const token = typeof window !== "undefined" ? window.__BIAL_TOKEN : null;
  if (token) headers["Authorization"] = "Bearer " + token;
  return headers;
}

type HttpMethod = "GET" | "POST" | "PATCH" | "DELETE";

async function call(url: string, method: HttpMethod, body?: unknown): Promise<unknown> {
  const cfg = getConfig();
  if (!ready(cfg)) {
    // Config not injected yet: reads resolve empty (the app shows its empty state); writes
    // reject so a Save is never silently dropped. Re-renders with real data once config lands.
    if (method !== "GET") throw new Error(NOT_READY);
    return null;
  }
  const headers = baseHeaders(cfg);
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const res = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (res.status === 401) {
    throw new Error("Please sign in to use this app.");
  }
  if (!res.ok) {
    let message = "Request failed (" + res.status + ").";
    try {
      const err = (await res.json()) as { error?: { message?: string; code?: string } };
      if (err?.error?.message) message = err.error.message;
    } catch {
      // non-JSON error body — keep the generic message
    }
    throw new Error(message);
  }
  if (res.status === 204) return null;
  return res.json();
}

function url(suffix = ""): string {
  const cfg = getConfig();
  // Guarded by call()'s ready() check; when unready these fields are absent and call() short
  // -circuits before the URL is fetched, so the cast is only reached on the ready path.
  return recordsUrl(cfg as Required<Pick<BialConfig, "appId" | "baseUrl">>, suffix);
}

function encode(v: string | number): string {
  return encodeURIComponent(String(v));
}

async function save(collection: string, data: Record<string, unknown>): Promise<RecordOut> {
  return (await call(url(), "POST", { collection, data })) as RecordOut;
}

async function list(collection?: string, opts?: { limit?: number }): Promise<RecordOut[]> {
  const params: string[] = [];
  if (collection) params.push("collection=" + encode(collection));
  if (opts?.limit) params.push("limit=" + encode(opts.limit));
  const suffix = params.length ? "?" + params.join("&") : "";
  const out = (await call(url(suffix), "GET")) as { records?: RecordOut[] } | null;
  return out?.records ?? [];
}

async function query(
  collection: string | undefined,
  opts: {
    q?: string;
    page?: number;
    pageSize?: number;
    sort?: string;
    order?: "asc" | "desc";
    filter?: Record<string, unknown>;
  } = {},
): Promise<SearchResult> {
  const params: string[] = [];
  if (collection) params.push("collection=" + encode(collection));
  if (opts.q) params.push("q=" + encode(opts.q));
  if (opts.page) params.push("page=" + encode(opts.page));
  if (opts.pageSize) params.push("pageSize=" + encode(opts.pageSize));
  if (opts.sort) params.push("sort=" + encode(opts.sort));
  if (opts.order) params.push("order=" + encode(opts.order));
  if (opts.filter) params.push("filter=" + encode(JSON.stringify(opts.filter)));
  const suffix = "/search" + (params.length ? "?" + params.join("&") : "");
  const out = (await call(url(suffix), "GET")) as SearchResult | null;
  return out ?? { items: [], total: 0, page: 1, pageSize: 25, totalPages: 0 };
}

async function distinct(collection: string | undefined, field: string): Promise<unknown[]> {
  const params = ["field=" + encode(field)];
  if (collection) params.unshift("collection=" + encode(collection));
  const out = (await call(url("/distinct?" + params.join("&")), "GET")) as { values?: unknown[] } | null;
  return out?.values ?? [];
}

async function get(_collection: string | undefined, id: string): Promise<RecordOut | null> {
  const out = (await call(url("/" + encode(id)), "GET")) as { record?: RecordOut } | null;
  return out?.record ?? null;
}

async function update(
  _collection: string | undefined,
  id: string,
  data: Record<string, unknown>,
): Promise<RecordOut | null> {
  const out = (await call(url("/" + encode(id)), "PATCH", { data })) as { record?: RecordOut } | null;
  return out?.record ?? null;
}

async function remove(_collection: string | undefined, id: string): Promise<{ ok: true } | null> {
  return (await call(url("/" + encode(id)), "DELETE")) as { ok: true } | null;
}

async function seedFromUpload(
  collection: string,
  rows: Record<string, unknown>[],
  opts: { dedupeKey?: string } = {},
): Promise<{ seeded: number; skipped: boolean }> {
  if (!Array.isArray(rows) || rows.length === 0) return { seeded: 0, skipped: true };
  const existing = await list(collection, { limit: 500 });
  if (opts.dedupeKey) {
    const key = opts.dedupeKey;
    const seen = new Set<unknown>(existing.map((r) => r.data[key]).filter((v) => v !== undefined));
    const fresh = rows.filter((row) => !seen.has(row[key]));
    for (const row of fresh) await save(collection, row);
    return { seeded: fresh.length, skipped: false };
  }
  if (existing.length > 0) return { seeded: 0, skipped: true }; // already seeded
  for (const row of rows) await save(collection, row);
  return { seeded: rows.length, skipped: false };
}

export const bialData: BialData = {
  save,
  list,
  query,
  distinct,
  get,
  update,
  remove,
  seedFromUpload,
};
