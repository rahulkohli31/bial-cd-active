/**
 * bial-data.ts — an EDITABLE starter client for the platform data-service (R21). In the
 * open-sandbox model you may edit, extend, or replace this file; it is no longer frozen.
 *
 * This is an HTTP client to the platform data-service (the interim `data_records` plane), NOT
 * Drizzle / Prisma / any ORM and NOT a direct DB client. It reproduces the wire shape the
 * deployed runner already speaks, retargeted at `/v1/apps/{appId}/records`. If you extend it,
 * KEEP that wire shape — the platform data-service expects it (X-App-Key, the `/v1/apps/{id}/
 * records` path, and the response envelopes below).
 *
 * Config source (C6 §4 / C9): the app's identity + credential + base URL arrive as
 * BIAL_APP_ID / BIAL_APP_CREDENTIAL / BIAL_DATA_BASE_URL — chosen so none ends in
 * _TOKEN/_SECRET/_KEY and they survive the supervisor's child-env scrub (D5). On the server
 * (`next dev`) they are read from `process.env`; in the browser they arrive via the
 * `window.__BIAL_CONFIG` bootstrap that `app/layout.tsx` injects at request time, so client
 * components fetch the data-service directly with `X-App-Key` (C9 accepts this bounded,
 * app_id-scoped credential exposure §5). The write-capable BIAL_BLOB_SAS is a real secret — use it
 * only server-side (Route Handlers / Server Actions), never in the browser.
 *
 * IMPORTANT base-URL note: BIAL_DATA_BASE_URL MUST already include the `/v1` prefix, because
 * the path is built by raw concat — `baseUrl + '/apps/' + appId + '/records'` — landing on the
 * mounted route `/v1/apps/{app_id}/records`. No trailing slash on the base URL.
 *
 * Every response the data-service returns is PARSED with zod at the boundary (fail-first-
 * typescript.md): shape drift fails loud here instead of threading `undefined` into the UI.
 * The TS types are inferred from those schemas with `z.infer`, so callers are unaffected.
 */

import { z } from "zod";

// ---- the server projection the data-service returns (never leaks app_id/bytes/search_text) ----
const recordOutSchema = z.object({
  id: z.string(), // uuid
  collection: z.string(),
  data: z.record(z.string(), z.unknown()),
  createdBy: z.string().nullable(), // uuid, camelCased on the wire (CamelModel)
  createdInDraft: z.boolean(),
  createdAt: z.string(), // ISO-8601
  updatedAt: z.string(), // ISO-8601
});
export type RecordOut = z.infer<typeof recordOutSchema>;

const searchResultSchema = z.object({
  items: z.array(recordOutSchema),
  total: z.number(),
  page: z.number(),
  pageSize: z.number(),
  totalPages: z.number(),
});
export type SearchResult = z.infer<typeof searchResultSchema>;

// ---- response envelope schemas (the wrapper shapes each endpoint returns) ----------------------
const recordEnvelopeSchema = z.object({ record: recordOutSchema.optional() }); // get / update → {record}
const recordsEnvelopeSchema = z.object({ records: z.array(recordOutSchema).optional() }); // list → {records}
const valuesEnvelopeSchema = z.object({ values: z.array(z.unknown()).optional() }); // distinct → {values}
const okEnvelopeSchema = z.object({ ok: z.literal(true) }); // remove → {ok:true}
const errorBodySchema = z.object({
  error: z.object({ message: z.string().optional(), code: z.string().optional() }).optional(),
});

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
const TIMED_OUT = "The data service took too long to respond — please try again.";
const REQUEST_TIMEOUT_MS = 15000; // abort a CRUD fetch that hangs, so the UI never sticks in a loading state.

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

/** The one HTTP leg. Builds the URL from the ALREADY-validated config (post `ready()` guard,
 *  finding #7), fetches with a hard timeout (finding #6), and `.parse()`s the body against the
 *  caller's zod schema before returning (finding #2). Returns null only for an unready GET or a
 *  204 — the wrappers absorb that into their empty state. */
async function call<T>(
  suffix: string,
  method: HttpMethod,
  schema: z.ZodType<T>,
  body?: unknown,
): Promise<T | null> {
  const cfg = getConfig();
  if (!ready(cfg)) {
    // Config not injected yet: reads resolve empty (the app shows its empty state); writes
    // reject so a Save is never silently dropped. Re-renders with real data once config lands.
    if (method !== "GET") throw new Error(NOT_READY);
    return null;
  }
  // Past the readiness guard: `ready(cfg)` narrowed cfg to carry appId/appKey/baseUrl, so the URL
  // + header builders receive a genuinely-validated config — no unchecked cast (finding #7).
  const target = recordsUrl(cfg, suffix);
  const headers = baseHeaders(cfg);
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let res: Response;
  try {
    res = await fetch(target, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (e) {
    // A timed-out/aborted request gets a distinct user-facing message so the caller's
    // try/catch/finally clears its loading/saving state (finding #6). Every OTHER failure
    // (network down, DNS, etc.) propagates unchanged — never swallowed.
    if (e instanceof DOMException && (e.name === "TimeoutError" || e.name === "AbortError")) {
      throw new Error(TIMED_OUT);
    }
    throw e;
  }

  if (res.status === 401) {
    throw new Error("Please sign in to use this app.");
  }
  if (!res.ok) {
    let message = "Request failed (" + res.status + ").";
    try {
      const err = errorBodySchema.parse(await res.json());
      if (err.error?.message) message = err.error.message;
    } catch {
      // non-JSON / unexpected error body — keep the generic message
    }
    throw new Error(message);
  }
  if (res.status === 204) return null;
  const json: unknown = await res.json();
  return schema.parse(json);
}

function encode(v: string | number): string {
  return encodeURIComponent(String(v));
}

async function save(collection: string, data: Record<string, unknown>): Promise<RecordOut> {
  const out = await call("", "POST", recordOutSchema, { collection, data });
  // A create always returns its 201 body; a null here would mean the config went unready
  // mid-flight — reject rather than hand back a phantom record (fail-first).
  if (out === null) throw new Error(NOT_READY);
  return out;
}

async function list(collection?: string, opts?: { limit?: number }): Promise<RecordOut[]> {
  const params: string[] = [];
  if (collection) params.push("collection=" + encode(collection));
  if (opts?.limit) params.push("limit=" + encode(opts.limit));
  const suffix = params.length ? "?" + params.join("&") : "";
  const out = await call(suffix, "GET", recordsEnvelopeSchema);
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
  const out = await call(suffix, "GET", searchResultSchema);
  return out ?? { items: [], total: 0, page: 1, pageSize: 25, totalPages: 0 };
}

async function distinct(collection: string | undefined, field: string): Promise<unknown[]> {
  const params = ["field=" + encode(field)];
  if (collection) params.unshift("collection=" + encode(collection));
  const out = await call("/distinct?" + params.join("&"), "GET", valuesEnvelopeSchema);
  return out?.values ?? [];
}

async function get(_collection: string | undefined, id: string): Promise<RecordOut | null> {
  const out = await call("/" + encode(id), "GET", recordEnvelopeSchema);
  return out?.record ?? null;
}

async function update(
  _collection: string | undefined,
  id: string,
  data: Record<string, unknown>,
): Promise<RecordOut | null> {
  const out = await call("/" + encode(id), "PATCH", recordEnvelopeSchema, { data });
  return out?.record ?? null;
}

async function remove(_collection: string | undefined, id: string): Promise<{ ok: true } | null> {
  return call("/" + encode(id), "DELETE", okEnvelopeSchema);
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
