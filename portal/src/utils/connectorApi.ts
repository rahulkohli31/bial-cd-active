/**
 * What one application reads from the connector catalogue: the switch, and the days.
 *
 * Mirrors `marketplaceApi.ts` deliberately — `authFetch` + `readApiError` + a narrowing parse
 * of an `unknown` body, relative paths only, no base-URL resolution. The edge rewrites
 * `/api/*` to `/v1/*`, so the paths below are the browser-visible ones.
 *
 * NO `zod`, AND THAT IS A DECISION RATHER THAN AN OVERSIGHT. It is a hoisted transitive of
 * `@assistant-ui/react`, absent from `portal/package.json` and imported nowhere in `src/`, so
 * reaching for it here would adopt a runtime dependency and a parsing convention for the whole
 * portal as a side effect of one feature — the phantom-dependency hazard, with a schema library
 * attached.
 *
 * WHERE THIS PARSE IS STRICT, AND WHY IT DIFFERS FROM THE CATALOG'S. `marketplaceApi` drops an
 * unreadable row and renders the rest, because that list is a shared catalog of hundreds and one
 * bad app must not blank it for the org. This list is the WHOLE of what an application can read
 * and it has one entry today: dropping the only row would render "nothing is connected", which is
 * a different and false statement. So a row we cannot read is a contract break and throws — the
 * tab already has an error-and-retry state, and it is the honest one.
 */
import { ApiError, isRecord, readApiError, requiredString } from './apiError'
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'

/**
 * This module's binding of the shared required-string reader — the noun is fixed here, once.
 * A missing required field means the server broke its own contract, not an absent value.
 */
const readString = (value: unknown, field: string): string =>
  requiredString(value, 'connector', field)

const jsonOpts = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body ?? {}),
})

/** How a window is expressed. The wire values of the server's `ConnectorWindowKind` enum. */
export type ConnectorWindowKind = 'relative' | 'absolute'

/**
 * What the row actually holds — the pair the citizen picked, BEFORE any clamp.
 *
 * Exactly one shape is populated: `days` for a preset, `start` + `end` for a fixed range. It
 * crosses the wire so `clamped` can be acted on, and it has exactly one reader in this portal:
 * the popover, which uses it to decide WHICH option is ticked. Nothing renders it as the
 * project's current window — that is the resolved pair beside it.
 */
export interface StoredWindow {
  days: number | null
  start: string | null
  end: string | null
}

/**
 * The days one project reads from one connector RIGHT NOW, as `resolve_window` answered.
 *
 * EVERY FIELD IS AN ANSWER, NOT AN INPUT. `start`, `end` and `days` are post-clamp, so the chip
 * and the `Reading N days of flight data` sentence beside it are the same numbers from the same
 * emitter. Nothing in this portal recomputes any of them.
 *
 * `earliestDate` AND `latestDate` ARE WHY THE GRID CAN GREY HONESTLY. A browser in Bangalore and
 * a server in UTC are 5½ hours apart, so a calendar that worked out its own floor would offer a
 * date the next read refuses. Both ends travel, and both disable dates.
 *
 * DATES STAY STRINGS HERE. They are `YYYY-MM-DD` calendar days, not instants, and
 * `new Date('2026-09-01')` parses as UTC midnight — which is 31 August in every timezone west of
 * Greenwich. The one place that needs `Date` objects (the month grid) builds them field by field.
 */
export interface ConnectorWindow {
  kind: ConnectorWindowKind
  start: string
  end: string
  /** `(end - start) + 1` AFTER clamping — what the app can actually see. */
  days: number
  /** The stored pair aged out of retention or pointed past today, and was moved. */
  clamped: boolean
  /** The oldest and newest dates this connector will serve today, inclusive. */
  earliestDate: string
  latestDate: string
  stored: StoredWindow
}

/**
 * What a citizen picks in the popover — the server's discriminated `WindowChoice`.
 *
 * Sending one REPLACES the stored window outright; there is no per-field merge. Omitting it from
 * an update keeps whatever is stored, which is what makes a switch press a one-field call.
 */
export type WindowChoice =
  | { kind: 'relative'; days: number }
  | { kind: 'absolute'; start: string; end: string }

/**
 * One registry connector as ONE APPLICATION sees it — the Settings › Integrations row, and the
 * answer every write returns.
 *
 * `enabled` IS THE SWITCH POSITION AND `effectivelyOn` IS WHETHER IT READS. The switch renders
 * `enabled`, and anything meaning "this project can see the data" reads `effectivelyOn` — the
 * resolver's answer, never one worked out here.
 */
export interface ProjectConnectorEntry {
  key: string
  displayName: string
  /**
   * What this connector's data is called, lowercase, for the tab's two state sentences. It
   * comes off the wire rather than living in the component so that a second connector costs a
   * registry entry and nothing else.
   */
  dataNoun: string
  enabled: boolean
  effectivelyOn: boolean
  window: ConnectorWindow | null
}

/** A required wire boolean. Same strictness as `readString` — a missing one is a contract break. */
function readBoolean(value: unknown, field: string): boolean {
  if (typeof value !== 'boolean') {
    throw new ApiError(`The server sent a connector we could not read (${field}).`, 500)
  }
  return value
}

/** A required wire integer. */
function readNumber(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new ApiError(`The server sent a connector we could not read (${field}).`, 500)
  }
  return value
}

/**
 * A required `YYYY-MM-DD` calendar day.
 *
 * THE SHAPE IS CHECKED, NOT JUST THE TYPE, because every consumer of these four fields splits
 * them on `-` to build a local `Date` without a timezone. A string that is not this shape would
 * produce an `Invalid Date` three components away from here, where the cause is invisible.
 */
function readDate(value: unknown, field: string): string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new ApiError(`The server sent a connector we could not read (${field}).`, 500)
  }
  return value
}

/** The kind, or a throw — never a fallback, which would render a day count as a date range. */
function readWindowKind(value: unknown): ConnectorWindowKind {
  if (value === 'relative' || value === 'absolute') return value
  throw new ApiError('The server sent a window kind this app does not recognise.', 500)
}

/** A wire integer that is genuinely nullable (`stored.days` on an absolute row). */
function optionalNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** A wire date that is genuinely nullable (`stored.start` / `stored.end` on a preset row). */
function optionalDate(value: unknown): string | null {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null
}

/** `null` stays `null` — a project with no row has no window, which is not a broken one. */
function toWindow(value: unknown): ConnectorWindow | null {
  if (value === null || value === undefined) return null
  const row = isRecord(value) ? value : {}
  const stored = isRecord(row.stored) ? row.stored : {}
  return {
    kind: readWindowKind(row.kind),
    start: readDate(row.start, 'window.start'),
    end: readDate(row.end, 'window.end'),
    days: readNumber(row.days, 'window.days'),
    clamped: readBoolean(row.clamped, 'window.clamped'),
    earliestDate: readDate(row.earliestDate, 'window.earliestDate'),
    latestDate: readDate(row.latestDate, 'window.latestDate'),
    stored: {
      days: optionalNumber(stored.days),
      start: optionalDate(stored.start),
      end: optionalDate(stored.end),
    },
  }
}

function toProjectConnector(value: unknown): ProjectConnectorEntry {
  const row = isRecord(value) ? value : {}
  return {
    key: readString(row.key, 'key'),
    displayName: readString(row.displayName, 'displayName'),
    dataNoun: readString(row.dataNoun, 'dataNoun'),
    enabled: readBoolean(row.enabled, 'enabled'),
    effectivelyOn: readBoolean(row.effectivelyOn, 'effectivelyOn'),
    window: toWindow(row.window),
  }
}

/**
 * Every registry connector as ONE application sees it — the read behind Settings › Integrations.
 *
 * IT IS RE-READ, NEVER CACHED. Days resolve against today, and this portal has no query cache to
 * invalidate — so the tab fetches every time it is chosen. That guaranteed pre-load moment is why
 * the tab owns a skeleton.
 *
 * STRICT, LIKE THE REST OF THIS MODULE: an unreadable row is a contract break and throws, because
 * a dropped row would silently remove the only data source this project has.
 */
export async function listProjectConnectors(
  projectId: string,
  deps: AuthFetchDeps = {},
): Promise<ProjectConnectorEntry[]> {
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(projectId)}/connectors`,
    {},
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to load this application’s data settings')
  const body: unknown = await res.json()
  const doc = isRecord(body) ? body : {}
  if (!Array.isArray(doc.connectors)) {
    throw new ApiError('The server sent a data list we could not read.', 500)
  }
  return doc.connectors.map(toProjectConnector)
}

/**
 * Switch a connector on or off for one project, and set the days it reads. One write, both facts.
 *
 * `window` omitted KEEPS whatever the project already had — so switching off and back on returns
 * the range the citizen chose rather than silently re-picking a default. Sending one replaces the
 * stored window outright.
 *
 * Refused with `404` for a project you do not own or a connector that is not in the catalogue,
 * `409` while a chat or a build is running, and `422` for a day count the connector does not
 * offer. The message the server sends is the one to show.
 *
 * Returns the project's connector RESOLVED exactly as a read returns it — which is what the chip
 * re-renders from, never the local pick.
 */
export async function setProjectConnector(
  projectId: string,
  connectorKey: string,
  update: { enabled: boolean; window?: WindowChoice },
  deps: AuthFetchDeps = {},
): Promise<ProjectConnectorEntry> {
  const res = await authFetch(
    `/api/projects/${encodeURIComponent(projectId)}/connectors/${encodeURIComponent(connectorKey)}`,
    jsonOpts('PUT', update),
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to save this application’s data settings')
  return toProjectConnector(await res.json())
}
