// Mock platform data-service for acceptance (a) — the C6 §3 wire shape, dict-backed.
//
// The golden template's CRUD fetch runs in the HOST BROWSER (records/page.tsx is `use client`,
// lib/bial-data.ts fetches via window.__BIAL_CONFIG), and the framed app's origin (the container
// port) differs from this mock's origin (a different host port) — so every non-simple request is
// a genuine CROSS-ORIGIN fetch carrying a custom `X-App-Key` header, which triggers a CORS
// PREFLIGHT. This mock therefore answers OPTIONS + reflects the request Origin (= the preview
// origin) on every response, incl. the 401 (plan D7).
//
// Routes are mounted at  /v1/apps/{appId}/records...  because lib/bial-data.ts builds the URL by
// raw concat: BIAL_DATA_BASE_URL('…/v1') + '/apps/' + appId + '/records' + suffix (C6).
//
// Response envelopes are ASYMMETRIC exactly as the real records_router returns them (C6 §3):
//   create → BARE RecordOut, 201   |  list → {records}  |  get/update → {record}  |  delete → {ok:true}

import http from "node:http";
import { randomUUID } from "node:crypto";

/**
 * @param {{ appKey: string, appId: string }} cfg
 * @returns {Promise<{ url: string, port: number, store: Map<string, object>, requests: object[], stop: () => Promise<void> }>}
 */
export function startMockDataService({ appKey, appId }) {
  /** @type {Map<string, any>} */
  const store = new Map(); // id -> RecordOut
  /** @type {{method:string,path:string,status:number}[]} */
  const requests = []; // a small audit trail for the driver to assert against
  const recordsPath = `/v1/apps/${appId}/records`;

  const nowIso = () => new Date().toISOString();

  function setCors(req, res) {
    // Reflect the framed app's origin (= the preview origin) — never '*', matching the real
    // cross-origin posture. X-App-Key is a custom header, so it MUST be in Allow-Headers.
    res.setHeader("Access-Control-Allow-Origin", req.headers.origin || "*");
    res.setHeader("Vary", "Origin");
    res.setHeader("Access-Control-Allow-Headers", "x-app-key, content-type, authorization");
    res.setHeader("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS");
    res.setHeader("Access-Control-Max-Age", "600");
  }

  function send(res, status, body) {
    const payload = body === undefined ? "" : JSON.stringify(body);
    res.writeHead(status, { "content-type": "application/json" });
    res.end(payload);
  }

  function makeRecord(collection, data) {
    const id = randomUUID();
    const ts = nowIso();
    return {
      id,
      collection,
      data: data ?? {},
      createdBy: null,
      createdInDraft: false,
      createdAt: ts,
      updatedAt: ts,
    };
  }

  async function readBody(req) {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    if (!chunks.length) return {};
    try {
      return JSON.parse(Buffer.concat(chunks).toString("utf8"));
    } catch {
      return {};
    }
  }

  const server = http.createServer((req, res) => {
    setCors(req, res);
    const method = req.method || "GET";
    const url = new URL(req.url || "/", "http://mock.local");
    const pathname = url.pathname;
    const record = (status) => requests.push({ method, path: pathname, status });

    // CORS preflight — answer before auth (a preflight carries no X-App-Key).
    if (method === "OPTIONS") {
      record(204);
      return send(res, 204);
    }

    // Every data call must present the app credential as X-App-Key (C6 / C9). 401 → the client
    // maps this to "Please sign in to use this app." (bial-data.ts hardcodes that message on 401).
    if (req.headers["x-app-key"] !== appKey) {
      record(401);
      return send(res, 401, {
        error: { message: "Please sign in to use this app.", code: "unauthorized" },
      });
    }

    // Only the records surface exists in the interim (C6: records-only).
    if (pathname !== recordsPath && !pathname.startsWith(recordsPath + "/")) {
      record(404);
      return send(res, 404, { error: { message: "not found", code: "not_found" } });
    }
    const id = pathname === recordsPath ? "" : pathname.slice(recordsPath.length + 1);

    // --- collection root: create (POST) | list (GET) ------------------------------------------
    if (id === "") {
      if (method === "POST") {
        return readBody(req).then((body) => {
          const rec = makeRecord(body.collection ?? "items", body.data ?? {});
          store.set(rec.id, rec);
          record(201);
          return send(res, 201, rec); // BARE record, 201 (no wrapper) — C6
        });
      }
      if (method === "GET") {
        const collection = url.searchParams.get("collection");
        const limit = Number(url.searchParams.get("limit") || "0");
        let records = [...store.values()];
        if (collection) records = records.filter((r) => r.collection === collection);
        if (limit > 0) records = records.slice(0, limit);
        record(200);
        return send(res, 200, { records }); // {records} envelope — C6
      }
      record(405);
      return send(res, 405, { error: { message: "method not allowed", code: "bad_method" } });
    }

    // --- /{id}: get (GET) | update (PATCH) | delete (DELETE) -----------------------------------
    if (method === "GET") {
      const rec = store.get(id);
      record(rec ? 200 : 404);
      return rec
        ? send(res, 200, { record: rec })
        : send(res, 404, { error: { message: "no such record", code: "not_found" } });
    }
    if (method === "PATCH") {
      return readBody(req).then((body) => {
        const rec = store.get(id);
        if (!rec) {
          record(404);
          return send(res, 404, { error: { message: "no such record", code: "not_found" } });
        }
        rec.data = { ...rec.data, ...(body.data ?? {}) }; // shallow last-write-wins merge — C6
        rec.updatedAt = nowIso();
        store.set(id, rec);
        record(200);
        return send(res, 200, { record: rec }); // {record} envelope — C6
      });
    }
    if (method === "DELETE") {
      const existed = store.delete(id);
      record(existed ? 200 : 404);
      return existed
        ? send(res, 200, { ok: true }) // {ok:true}, 200 — C6
        : send(res, 404, { error: { message: "no such record", code: "not_found" } });
    }
    record(405);
    return send(res, 405, { error: { message: "method not allowed", code: "bad_method" } });
  });

  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      resolve({
        url: `http://127.0.0.1:${port}/v1`,
        port,
        store,
        requests,
        stop: () => new Promise((r) => server.close(() => r())),
      });
    });
  });
}
