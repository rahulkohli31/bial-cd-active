// Acceptance (a) — the golden template ACTUALLY runs a CRUD round-trip on the REAL image (plan
// U13, R1). The first of the two program risks: docker build → `next dev` (via the supervisor) →
// a create→list→edit→delete round-trip against a mock data-service, driven through a real Chromium.
//
// Topology (all on the host loopback; distinct ports = distinct origins):
//   container :appPort   Caddy → supervisor(:9000) + next dev(:3000)   — the framed app's origin
//   mock      :mockPort  the C6-wire-shape data-service                — a DIFFERENT origin
//   portal    :portalPort  frames the app with the C8 sandbox= tokens  — the framing parent
//
// The CRUD fetch runs in the browser (records/page.tsx is `use client`), app-origin → mock-origin
// is cross-origin, and it carries X-App-Key → a CORS PREFLIGHT. So BIAL_DATA_BASE_URL must be
// host-browser-reachable (http://127.0.0.1:mockPort/v1, NOT host.docker.internal), and the mock
// answers CORS (see mock-data-service.mjs). We drive INSIDE the real C8 iframe sandbox so the test
// also proves the cross-origin fetch works under `allow-same-origin` (the production posture).
//
// Playwright is self-contained here (tests/acceptance_a/node_modules) so it does not depend on
// portal/ being installed (R8 forbids portal edits). Run: `npm run setup` once, then `node run.mjs`.

import net from "node:net";
import http from "node:http";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { chromium } from "playwright";
import { startMockDataService } from "./mock-data-service.mjs";

const IMAGE = process.env.BIAL_SANDBOX_IMAGE || "bial-sandbox-test";
const SANDBOX_DIR = fileURLToPath(new URL("../../", import.meta.url)); // sandbox/
const TOKEN = "acceptance-a-supervisor-token";
const APP_ID = "app-acceptance-a";
const APP_KEY = "bial_acceptance_a_key";
const SANDBOX_ATTR = "allow-scripts allow-same-origin allow-forms allow-downloads"; // C8 §4 (frozen)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = [];
async function step(name, fn) {
  try {
    await fn();
    results.push([name, true]);
    console.log("PASS  " + name);
  } catch (e) {
    results.push([name, false]);
    console.log("FAIL  " + name + " — " + (e && e.message ? e.message : e));
    throw e;
  }
}
function expect(cond, msg) {
  if (!cond) throw new Error(msg);
}

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => {
      const p = s.address().port;
      s.close(() => resolve(p));
    });
  });
}

function ensureImage(tag) {
  if (spawnSync("docker", ["image", "inspect", tag], { stdio: "ignore" }).status === 0) return;
  console.log(`building image ${tag} (first build is slow — bakes node_modules)…`);
  const b = spawnSync("docker", ["build", "-f", "Dockerfile.sandbox", "-t", tag, "."], {
    cwd: SANDBOX_DIR,
    stdio: "inherit",
  });
  if (b.status !== 0) throw new Error("docker build failed");
}

function dockerRun(name, appPort, env) {
  const args = ["run", "-d", "--name", name, "-p", `127.0.0.1:${appPort}:8080`];
  for (const [k, v] of Object.entries(env)) args.push("-e", `${k}=${v}`);
  args.push(IMAGE);
  const r = spawnSync("docker", args, { encoding: "utf8" });
  if (r.status !== 0) throw new Error("docker run failed: " + r.stderr);
}
function dockerLogs(name) {
  const r = spawnSync("docker", ["logs", name], { encoding: "utf8" });
  return (r.stdout || "") + (r.stderr || "");
}
function dockerRm(name) {
  spawnSync("docker", ["rm", "-f", name], { stdio: "ignore" });
}

const auth = { Authorization: `Bearer ${TOKEN}` };

async function waitSupervisorHealth(base, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`${base}/_sup/health`);
      if (r.ok && (await r.json()).ok === true) return;
    } catch {
      /* not up yet */
    }
    await sleep(500);
  }
  throw new Error("supervisor never became healthy");
}

async function waitDevReady(base, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    const r = await fetch(`${base}/_sup/dev/status`, { headers: auth });
    last = await r.json();
    if (last.running && last.ready) return last;
    await sleep(1000);
  }
  // Bounded-wait diagnostic (plan U13): the frozen READY_MARKERS=("Ready in",) was calibrated on
  // Next 14/15; if Next 16's non-TTY banner ever differs, this dumps the actual text to fix it via
  // the C1-change-doc-first path (U14 owns the C1 edit).
  const lr = await fetch(`${base}/_sup/dev/logs?since=0`, { headers: auth });
  const lines = (await lr.json()).lines || [];
  throw new Error(
    `ready marker not seen within ${timeoutMs}ms — check the \`next dev\` banner text.\n` +
      `last status=${JSON.stringify(last)}\n--- dev logs ---\n${lines.join("\n")}`,
  );
}

async function getRecordsFrame(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const f = page.frames().find((fr) => fr.url().includes("/records"));
    if (f && f.url() !== "about:blank") return f;
    await sleep(250);
  }
  throw new Error("the /records frame never appeared");
}

async function main() {
  ensureImage(IMAGE);

  const [appPort, portalPort] = await Promise.all([freePort(), freePort()]);
  const base = `http://127.0.0.1:${appPort}`;
  const portalOrigin = `http://127.0.0.1:${portalPort}`;
  const name = `bial-accept-a-${randomUUID().slice(0, 8)}`;

  const mock = await startMockDataService({ appKey: APP_KEY, appId: APP_ID });
  let portalServer = null;
  let browser = null;

  try {
    // --- provision the sandbox container -----------------------------------------------------
    dockerRun(name, appPort, {
      SUPERVISOR_TOKEN: TOKEN,
      BIAL_APP_ID: APP_ID,
      BIAL_APP_CREDENTIAL: APP_KEY,
      BIAL_DATA_BASE_URL: mock.url, // host-browser-reachable http://127.0.0.1:<mockPort>/v1
      BIAL_PORTAL_ORIGIN: portalOrigin, // so the app's Caddy frame-ancestors allows our portal
    });
    await waitSupervisorHealth(base, 60_000);

    // --- start next dev + prove the Next 16 ready marker is seen on the non-TTY pipe ---------
    // Send an empty JSON body: DevStartBody's fields all default, but FastAPI still requires the
    // body to be present + typed application/json (a bodiless POST 422s).
    const ds = await fetch(`${base}/_sup/dev/start`, {
      method: "POST",
      headers: { ...auth, "Content-Type": "application/json" },
      body: "{}",
    });
    if (!ds.ok) throw new Error(`/dev/start failed: ${ds.status}`);
    await step("dev server reaches ready (Next 16 marker seen on the supervisor's non-TTY pipe)", () =>
      waitDevReady(base, 120_000),
    );

    // --- frame the preview /records with the frozen C8 sandbox tokens -------------------------
    const previewUrl = `${base}/records`;
    portalServer = http.createServer((_req, res) => {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(
        `<!doctype html><meta charset="utf-8"><title>portal</title><body style="margin:0">` +
          `<iframe id="preview" src="${previewUrl}" sandbox="${SANDBOX_ATTR}" ` +
          `style="width:100vw;height:100vh;border:0"></iframe>`,
      );
    });
    await new Promise((r) => portalServer.listen(portalPort, "127.0.0.1", r));

    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    page.on("console", (m) => {
      if (m.type() === "error" || process.env.DEBUG_ACCEPT) console.log(`  [browser:${m.type()}] ${m.text()}`);
    });
    page.on("pageerror", (e) => console.log(`  [pageerror] ${e.message}`));
    page.on("requestfailed", (r) => console.log(`  [reqfailed] ${r.url()} — ${r.failure()?.errorText}`));
    await page.goto(portalOrigin, { waitUntil: "load" });
    const frame = await getRecordsFrame(page, 30_000);
    if (process.env.DEBUG_ACCEPT) {
      await sleep(4000);
      console.log("  [debug] __BIAL_CONFIG:", await frame.evaluate(() => JSON.stringify(window.__BIAL_CONFIG)));
      console.log("  [debug] frame body:", (await frame.evaluate(() => document.body.innerText)).slice(0, 300));
      console.log("  [debug] mock.requests so far:", JSON.stringify(mock.requests));
    }

    // --- the round-trip ----------------------------------------------------------------------
    await step("golden template compiles + the CRUD screen renders in the cross-origin frame", async () => {
      await frame.getByRole("button", { name: "New record" }).waitFor({ timeout: 90_000 });
      await frame.getByText("Records").first().waitFor({ timeout: 5_000 });
    });

    await step("empty state renders (list → {records: []}, cross-origin GET after CORS preflight)", () =>
      frame.getByText("No records yet.").waitFor({ timeout: 30_000 }),
    );

    await step("create → POST 201 bare RecordOut → the row appears in the list", async () => {
      await frame.getByRole("button", { name: "New record" }).click();
      await frame.getByLabel("Title").fill("Runway inspection");
      await frame.getByLabel("Note").fill("check the approach lights");
      await frame.getByRole("button", { name: "Create" }).click();
      await frame.getByRole("cell", { name: "Runway inspection" }).waitFor({ timeout: 15_000 });
      expect(mock.store.size === 1, `expected 1 record in the mock store, got ${mock.store.size}`);
      const rec = [...mock.store.values()][0];
      expect(rec.data.title === "Runway inspection", "stored title mismatch");
      expect(rec.collection === "items", "stored collection should be 'items'");
    });

    await step("edit → PATCH {record} → the UI reflects the update", async () => {
      await frame.getByRole("button", { name: "Edit" }).click();
      await frame.getByLabel("Title").fill("Runway inspection v2");
      await frame.getByRole("button", { name: "Save changes" }).click();
      await frame.getByRole("cell", { name: "Runway inspection v2" }).waitFor({ timeout: 15_000 });
      const rec = [...mock.store.values()][0];
      expect(rec.data.title === "Runway inspection v2", "stored title not updated by PATCH");
    });

    await step("delete → {ok:true} → the row disappears (empty state returns)", async () => {
      await frame.getByRole("button", { name: "Delete" }).click();
      await frame.getByText("No records yet.").waitFor({ timeout: 15_000 });
      expect(mock.store.size === 0, `expected 0 records after delete, got ${mock.store.size}`);
    });

    await step("401 (wrong X-App-Key) → the client surfaces 'Please sign in to use this app.'", async () => {
      await frame.evaluate(() => {
        if (window.__BIAL_CONFIG) window.__BIAL_CONFIG.appKey = "wrong-key-not-authorized";
      });
      await frame.getByRole("button", { name: "New record" }).click();
      await frame.getByLabel("Title").fill("unauthorized attempt");
      await frame.getByRole("button", { name: "Create" }).click();
      await frame.getByText("Please sign in to use this app.").waitFor({ timeout: 10_000 });
      expect(mock.store.size === 0, "a 401 create must not persist");
    });

    await step("a cross-origin CORS preflight (OPTIONS) was served for the custom X-App-Key header", () => {
      expect(mock.requests.some((r) => r.method === "OPTIONS"), "no OPTIONS preflight was seen");
    });

    await step("the mock observed the full asymmetric wire path (201 create, 200 list/patch/delete, 401)", () => {
      const seen = (m, s) => mock.requests.some((r) => r.method === m && r.status === s);
      expect(seen("POST", 201), "no 201 create");
      expect(seen("GET", 200), "no 200 list");
      expect(seen("PATCH", 200), "no 200 update");
      expect(seen("DELETE", 200), "no 200 delete");
      expect(seen("POST", 401), "no 401 on the bad-key create");
    });

    await page.close();
  } catch (err) {
    // On any failure, dump container logs to make a ready-timeout / compile error diagnosable.
    console.error("\n--- container logs (tail) ---\n" + dockerLogs(name).slice(-4000));
    throw err;
  } finally {
    if (browser) await browser.close();
    if (portalServer) await new Promise((r) => portalServer.close(r));
    await mock.stop();
    dockerRm(name);
  }
}

main()
  .then(() => {
    const failed = results.filter(([, ok]) => !ok);
    console.log(`\n${results.length - failed.length}/${results.length} acceptance-(a) checks passed`);
    if (failed.length) process.exit(1);
    console.log("OK — the golden template builds and does a full CRUD round-trip on the real image (R1).");
  })
  .catch((err) => {
    console.error("\nFATAL", err && err.stack ? err.stack : err);
    process.exit(1);
  });
