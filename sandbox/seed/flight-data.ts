import { BlobServiceClient } from '@azure/storage-blob'
import { ManagedIdentityCredential } from '@azure/identity'
import { parquetReadObjects } from 'hyparquet'
import { compressors } from 'hyparquet-compressors'

// ── The connection ───────────────────────────────────────────────────────────────────────────
//
// Two variables, injected when the connector is on. Never hardcode either; never send either to
// the browser. This whole file is server-only — importing it into a client component leaks a
// credential.

// Read them through a check rather than with `!`. A missing variable otherwise surfaces as
// `getaddrinfo ENOTFOUND undefined.blob.core.windows.net` several layers down, which reads like a
// network fault and is really "the connector is switched off for this project".
function required(name: string): string {
  const value = process.env[name]
  if (!value) {
    throw new Error(
      `${name} is not set. The flight-data connector is not switched on for this project — ` +
        `turn it on under DATA in the project rail, then restart the dev server.`,
    )
  }
  return value
}

/** Where the lake is: the blob service endpoint, the one container, the folder inside it. */
export type LakeAddress = { endpoint: string; container: string; prefix: string }

/**
 * Split the one injected URL into the three things the Azure client wants.
 *
 * The connector hands you the account, the container AND the folder in a single string —
 * `https://<account>.blob.core.windows.net/<container>/<prefix>/` — so there is one value to get
 * wrong instead of three, and it is copy-pasteable into Storage Explorer as-is.
 *
 * The trailing slash on the prefix is added back deliberately, and it is not cosmetic. A listing
 * prefix is matched as a plain string, so `flights` also returns everything under
 * `flights-archive/` and `flights_v2/` — sibling folders whose names merely START with it. Those
 * files have the same name pattern and would silently join your window. `flights/` cannot match
 * them. An empty prefix stays empty: the container root needs no separator.
 */
export function splitLakeUrl(url: string): LakeAddress {
  const parsed = new URL(url)
  const [container = '', ...rest] = parsed.pathname.replace(/^\/+/, '').split('/')
  const prefix = rest.join('/').replace(/\/+$/, '')
  return { endpoint: parsed.origin, container, prefix: prefix === '' ? '' : `${prefix}/` }
}

const LAKE = splitLakeUrl(required('BIAL_DICE_URL'))
const CLIENT_ID = required('BIAL_DICE_CLIENT_ID') // the managed identity's CLIENT id

// MISTAKE 1 — using DefaultAzureCredential.
// It silently reads AZURE_CLIENT_ID from the environment, which in this container belongs to a
// DIFFERENT identity (the platform's own). The call then fails with a permissions error that
// sends you looking at role assignments for a day. Name the identity explicitly.
const credential = new ManagedIdentityCredential({ clientId: CLIENT_ID })

const container = new BlobServiceClient(LAKE.endpoint, credential).getContainerClient(
  LAKE.container,
)

// ── Finding the files ────────────────────────────────────────────────────────────────────────

export type LakeFile = { name: string; size: number; loadDate: Date }

/**
 * What one entry in a flat listing turned out to be.
 *
 * Three outcomes, not two, because the two kinds of zero-length entry need opposite treatment:
 * a directory is nothing (there are four of them and they are not news), while a zero-byte file
 * IS news — it is a day of flights you are not going to get.
 */
export type Listed =
  | { kind: 'file'; file: LakeFile }
  | { kind: 'not-a-flight-file' }
  | { kind: 'empty-stub'; name: string }

// ANCHORED AT BOTH ENDS OF THE FILE NAME, and the left anchor is the load-bearing half.
// Without `(?:^|\/)` this also matches `old_tb_flight_fact_report_20260901.parquet` and
// `backup_..._20260901.parquet` as SUFFIXES — so an archived or hand-copied file silently joins
// your window, and the numbers it carries are counted twice. The platform's own selector
// (`backend/src/services/lake/window.py`) anchors the same way; if you change one, change both.
const FILENAME = /(?:^|\/)tb_flight_fact_report_(\d{4})(\d{2})(\d{2})\.parquet$/

/**
 * Decide what a listed blob is, from its name and its length.
 *
 * MISTAKE 3 — treating every zero-byte entry as a broken file.
 * This is a hierarchical-namespace account, so a flat listing returns the DIRECTORIES too, each
 * with a length of 0. Filtering on size alone makes you skip four folders and, worse, hides the
 * files that really are empty. Match the filename FIRST, then look at the size — the order of
 * those two checks is the whole difference between "four directories, no news" and "a day of
 * flights is missing and nobody said so".
 */
export function classify(name: string, size: number): Listed {
  const match = name.match(FILENAME)
  if (!match) return { kind: 'not-a-flight-file' }

  // A genuinely zero-byte file is the load having failed and left a stub. An existence check
  // passes; the parquet reader throws "file is too short". Skip these — do not crash the page
  // over one bad day, and do not silently pretend the day had no flights either.
  if (size === 0) return { kind: 'empty-stub', name }

  const [, y, m, d] = match
  return { kind: 'file', file: { name, size, loadDate: new Date(`${y}-${m}-${d}T00:00:00Z`) } }
}

// MISTAKE 2 — building a path instead of listing.
// The folder is the UPPERCASE MONTH NAME of the day the load RAN, not of the data inside it. The
// 31 August file lives under `2026/SEPTEMBER/`, because the job ran the next morning. Construct
// `2026/08/…` and you get nothing back, which reads exactly like a permissions failure and is not
// one. Always list.
export async function listFlightFiles(): Promise<LakeFile[]> {
  const files: LakeFile[] = []

  for await (const blob of container.listBlobsFlat({ prefix: LAKE.prefix })) {
    const listed = classify(blob.name, blob.properties.contentLength ?? 0)
    if (listed.kind === 'file') files.push(listed.file)
    else if (listed.kind === 'empty-stub') {
      console.warn(`[flight-data] skipping empty file (upstream load failed): ${listed.name}`)
    }
  }

  return files.sort((a, b) => a.loadDate.getTime() - b.loadDate.getTime())
}

/**
 * The newest day you can actually read, or null if the listing is empty.
 *
 * DERIVED FROM THE LISTING, never computed from `new Date()`. The lake runs a day behind — a
 * day's flights are loaded the following morning — and it is tempting to write
 * `yesterday = new Date(Date.now() - 86_400_000)` and be done. Do not. "A day behind" is a
 * CEILING, not a promise: loads fail and leave zero-byte stubs (which `classify` has already
 * dropped by the time you get here), and the calendar is sparse — there are days with no file at
 * all. A computed yesterday therefore names a file that may not exist, and your page renders an
 * empty chart with no error to explain it. The listing knows; arithmetic does not.
 *
 * Use it to label a dashboard ("data through 7 September") and to anchor a default window, so
 * both say the same thing as the files behind them.
 */
export function newestReadableDay(files: readonly LakeFile[]): Date | null {
  let newest: Date | null = null
  for (const file of files) if (!newest || file.loadDate > newest) newest = file.loadDate
  return newest
}

// ── Reading rows ─────────────────────────────────────────────────────────────────────────────

/**
 * The bytes of a Buffer, and nothing else.
 *
 * A Node Buffer is a VIEW over a larger pooled ArrayBuffer. Passing `buffer.buffer` straight in
 * hands the reader megabytes of unrelated memory and it fails to locate the parquet footer.
 * Slice to the view's own bounds.
 *
 * The cast is deliberate. Node types `Buffer.buffer` as `ArrayBuffer | SharedArrayBuffer`, and
 * the reader accepts only the former. A Buffer from the Azure SDK is never shared-backed, so
 * this is safe — but without the cast `npm run lint` fails under strict mode.
 */
export function ownBytes(buffer: Buffer): ArrayBuffer {
  return buffer.buffer.slice(
    buffer.byteOffset,
    buffer.byteOffset + buffer.byteLength,
  ) as ArrayBuffer
}

// MISTAKE 4 — reading all 408 columns.
//
// This is a MEMORY bug wearing a performance costume, and the costume is why it ships.
//
// Measured: 8 columns across a 30-day window peaks near 200 MB of heap. All 408 columns cost
// about 120 MB of heap PER FILE. The sandbox container you are building in has 2 GiB; a
// PUBLISHED container has 1 GiB. So a wide read builds green right here, passes every check you
// can run, and then YOUR PUBLISHED APP WILL BE KILLED — an OOM kill from the outside, so there
// is no exception, no stack trace and nothing in your logs except the app restarting.
//
// The timing is real too and much less important: on one ordinary day 8 columns took 11 ms and
// 27 MB, all 408 took 138 ms and 120 MB — 12.7x slower for data you then throw away.
//
// Name your columns. Always.
export async function readColumns<T>(file: LakeFile, columns: string[]): Promise<T[]> {
  const buffer = await container.getBlobClient(file.name).downloadToBuffer()
  return (await parquetReadObjects({ file: ownBytes(buffer), columns, compressors })) as T[]
}

// ── The two rules that decide whether your numbers are right ──────────────────────────────────

// MISTAKE 5, and the expensive one — assuming a file's date is its flights' date.
//
// It is not. The filename carries the LOAD date. `LAST_UPDATE_DATE_TIME` always matches it. The
// flights inside can be from any time: one measured file, loaded on a single day in August 2026,
// carried flights scheduled between July 2022 and October 2026.
//
// So "the last 30 days of flights" is NOT "the last 30 files". You read the files, then you
// filter the ROWS on a scheduled-time column.
//
// And there is a second trap inside the first. `SCHEDULED_OFF_BLOCK_TIME_SOBT` is the departure
// time and is NULL on every arrival row — about half the table. Filter on it and you silently
// drop every arrival and report half the traffic, with no error anywhere.
//
// `SIBT_SOBT_TIME` is the merged scheduled time and is one of the few columns that is never null.
// It is the column to filter dates on.

export const FLIGHT_TIME = 'SIBT_SOBT_TIME'
export const LOAD_TIME = 'LAST_UPDATE_DATE_TIME'
export const FLIGHT_KEY = 'AODB_AFTTAB_PK_URNO'

type Row = Record<string, unknown>

const asDate = (value: unknown): Date => (value instanceof Date ? value : new Date(Number(value)))

/**
 * Collapse amendment rows to one row per flight.
 *
 * The same flight is re-stated in later load files as its actual times get filled in. Measured
 * over a 30-day window: 75,551 rows collapse to 67,147 flights, so a query that unions the files
 * and counts reports 11.1% more flights than the airport actually operated — and every average
 * computed over those rows is wrong in the same direction.
 *
 * Keep the row with the highest LAST_UPDATE_DATE_TIME for each key. That is the current record.
 */
export function currentRecordsOnly<T extends Row>(rows: T[]): T[] {
  const latest = new Map<string, { stamp: number; row: T }>()

  for (const row of rows) {
    const key = String(row[FLIGHT_KEY])
    const stamp = asDate(row[LOAD_TIME]).getTime()
    const held = latest.get(key)
    if (!held || stamp > held.stamp) latest.set(key, { stamp, row })
  }

  return [...latest.values()].map((entry) => entry.row)
}

// MISTAKE 7 — grouping on a text column without trimming it.
// Several text columns carry the same value twice, once padded with trailing spaces.
// AIRLINE_NAME holds BOTH "AKASA AIR" and "AKASA AIR                     ", so a straight
// group-by renders Akasa Air as two bars on the chart — and Star Air as two more. GROUND_HANDLER
// has the same problem with "Indigo ". Some columns also use an EMPTY STRING where you would
// expect null, which `!= null` happily lets through.
//
// Use this for any text value you group, compare or display.
export function label(value: unknown): string | null {
  if (typeof value !== 'string') return value == null ? null : String(value)
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

/** Keep the flights actually scheduled inside the window the user asked for. */
export function flightsBetween<T extends Row>(rows: T[], from: Date, to: Date): T[] {
  return rows.filter((row) => {
    const when = asDate(row[FLIGHT_TIME])
    return when >= from && when <= to
  })
}

// ── Putting it together ──────────────────────────────────────────────────────────────────────

/**
 * Append a whole chunk to an array.
 *
 * MISTAKE 6 — `rows.push(...chunk)`. Spreading passes every element as a SEPARATE ARGUMENT, and a
 * backfill file holds 200,000+ rows — enough to overflow the call stack and crash the page with
 * `RangeError: Maximum call stack size exceeded`. It works fine on an ordinary day and fails only
 * when a backfill lands, which is the worst possible time to find out.
 */
export function appendAll<T>(target: T[], chunk: readonly T[]): void {
  for (const row of chunk) target.push(row)
}

/**
 * Every flight scheduled between two dates, deduped, with only the columns you asked for.
 *
 * Measured cost of a 30-day window from outside Azure: 30 files, 26 MB, 75,551 rows, ~6.7 s
 * end to end, ~200 MB peak heap. Most of that is download latency and it is far lower from
 * inside the region. If it still feels slow, fetch the files concurrently rather than in the
 * sequential loop below — but read the caching note underneath first.
 */
export async function flightsScheduledBetween<T extends Row>(
  from: Date,
  to: Date,
  columns: string[],
): Promise<T[]> {
  // Always include the three columns the correctness rules need, whatever the caller asked for.
  const needed = [...new Set([...columns, FLIGHT_KEY, LOAD_TIME, FLIGHT_TIME])]

  const files = await listFlightFiles()

  // WHICH FILES COULD HOLD A FLIGHT IN THIS WINDOW? ALL OF THEM — THERE IS NO ARITHMETIC ON THE
  // LOAD DATE THAT SAFELY RULES ONE OUT. The load-date trap above has the measurement: one file
  // loaded on a single day in August 2026 carried flights scheduled from July 2022 to October
  // 2026. A load date bounds neither end of the flights inside it, so the only honest filter is
  // the ROW filter, and it runs on `SIBT_SOBT_TIME` in `flightsBetween` below.
  //
  // This loop used to run over `files.filter((f) => f.loadDate >= from - 2 days)`. It emptied any
  // window starting more than two days after the newest load — every forward-looking question
  // returned `[]` with no error — and for a backward window it dropped a flight whose only
  // surviving record lived in an older load. Reading all of them is the honest cost of a right
  // answer; guessing costs a wrong one silently.
  const rows: T[] = []
  for (const file of files) appendAll(rows, await readColumns<T>(file, needed))

  return flightsBetween(currentRecordsOnly(rows), from, to)
}

// ── What you do with the rows once you have them ──────────────────────────────────────────────
//
// `hyparquet` hands back plain JavaScript objects — one per row, one key per column you asked
// for. There is no dataframe here and none is wanted: grouping and aggregating a few tens of
// thousands of plain objects is a `Map` and a `for` loop, which this file already demonstrates
// twice (`currentRecordsOnly` groups by key; `label` is the normaliser you call while you group).
// A dashboard that counts flights per airline per day is about six lines:
//
//   const perAirline = new Map<string, number>()
//   for (const row of rows) {
//     const airline = label(row.AIRLINE_NAME) ?? 'Unknown'
//     perAirline.set(airline, (perAirline.get(airline) ?? 0) + 1)
//   }
//
// If a future app genuinely needs pivots or multi-key joins over hundreds of thousands of rows,
// the pure-JS answer is `arquero` — install it in that app and nowhere else. DuckDB-wasm is
// faster still and was considered and rejected for this pass: it is a native binary in a stack
// that has none, on an image built by a Windows host, which is exactly the class of difference
// that only shows up after deployment.

// ── Caching ──────────────────────────────────────────────────────────────────────────────────
//
// Do NOT reach for an external cache. The files are small and the reads are effectively free
// (measured: $0.0000006 per read; a whole build session hitting one file 200 times costs
// $0.00012). What you want is Next's own caching, so a dashboard re-render does not re-download:
//
//   import { unstable_cache } from 'next/cache'
//
//   export const getFlights = unstable_cache(
//     async (from: string, to: string) =>
//       flightsScheduledBetween(new Date(from), new Date(to), ['AIRLINE_NAME', 'ARR_DEP_FLG_ADID']),
//     ['flights'],
//     { revalidate: 3600 },
//   )
//
// Keep the revalidate window short — an hour, not a week. A later load can amend a flight from
// months ago, so a long-lived cache serves a superseded record with no way to know it.
