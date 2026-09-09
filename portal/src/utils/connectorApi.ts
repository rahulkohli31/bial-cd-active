/**
 * The citizen's own connector access: what the platform can connect to, where this person
 * stands with each system, and the two writes that change it.
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
 * bad app must not blank it for the org. This list is the WHOLE of what Integrations offers and it
 * has one entry today: dropping the only row would render "nothing is connected", which is a
 * different and false statement, and the state is what selects which sentence and which control a
 * row draws. So a row we cannot read is a contract break and throws — the dialog already has an
 * error-and-retry state, and it is the honest one.
 */
import { ApiError, isRecord, optionalCount, optionalString, readApiError, requiredString } from './apiError'
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'

/**
 * Which of the four person states this citizen is in for one connector, mirroring the server's
 * `ConnectorPersonState`. Person state 5 (`withdrawn`) is not in this pass — there is no enum
 * member behind it and nothing that could set it, so it is not spelled here either.
 */
export type ConnectorState = 'neverAsked' | 'pending' | 'approved' | 'declined'

/**
 * One ticked line of the ask panel's `WHAT AN APPROVAL GIVES YOU` box.
 *
 * TWO FIELDS, NOT ONE SENTENCE, because the lead is bold markup against the body's grey. A
 * pre-joined string would make the browser guess the split at the first full stop, and the
 * server's own third-person set has a body that starts lowercase mid-sentence on purpose.
 */
export interface ConsentLine {
  lead: string
  body: string
}

/**
 * One connector as the asking person sees it.
 *
 * Fields outside the caller's own state are `null`, by the server's design: `approvedByName` on a
 * declined row would be a second answer to "who decided".
 *
 * EVERY NAME FIELD IS NULLABLE, AND `null` DOES NOT MEAN "LOOK UP THE EMAIL". The server already
 * substitutes the decider's email when their display name is unset, so a present decider always
 * arrives with a usable handle. `null` means there is NO decider to name — nobody has decided, or
 * the administrator who did has since been deleted. A second fallback in the browser would invent
 * a name for a decision that has none.
 */
export interface ConnectorEntry {
  /** The stored `connector_key`. Stable, lowercase, and never rendered — `displayName` is. */
  key: string
  displayName: string
  subtitle: string
  /**
   * The whole sentence the ask panel sets under its title — what this system holds, and that one
   * administrator answers once for you. NOT `subtitle`, which is the row's four-word label; the
   * panel cannot derive either from the other, so both travel.
   */
  askSubtitle: string
  /**
   * The three promises an approval makes, in board order. THEY COME OFF THE WIRE SO THE PANEL
   * STAYS A RENDERER — a component that spelled one connector's dataset facts would make "add a
   * second connector" a component change rather than a registry entry.
   */
  consentLinesRequester: readonly ConsentLine[]
  state: ConnectorState
  /** `pending` only. Carries the time of day: the row reads `Asked 5 Sep, 08:30 · …`. */
  askedAt: string | null
  /** `approved` only. */
  approvedAt: string | null
  approvedByName: string | null
  /**
   * `approved` only: how many of this person's own projects have the connector switched on.
   * `null` — not `0` — in every other state, because "we did not count" and "none" are different
   * answers and only one of them belongs on a row with no access.
   */
  onProjectCount: number | null
  /** `declined` only. */
  decidedAt: string | null
  decidedByName: string | null
  /** `declined` only: the administrator's own words, rendered as plain text and never markdown. */
  decisionRemarks: string | null
}

const CONNECTORS_CHANGED = 'bial:connectors-changed'

/**
 * SAY THAT A CONNECTOR WRITE HAPPENED, so every surface showing that fact can look again.
 *
 * WHY A SIGNAL AND NOT A PROP. `IntegrationsDialog` has TWO doors — the profile menu, which is on
 * every authed screen, and `Manage integrations →` in the workspace rail — and the drill-down
 * inside it can switch the connector on or off for the very project the rail is describing. Only
 * the rail's own door knew to re-read when it closed, so opening the same dialog from the avatar
 * menu left the rail asserting `Reading 30 days of flight data` about a project that had just
 * been switched off, until the citizen navigated away and back.
 *
 * Wiring the second door to the first door's callback would fix that one pair and leave the next
 * mount site to rediscover it. The invalidation belongs to whoever knows a write occurred, which
 * is the dialog — not to whichever component happened to open it.
 *
 * This is the `notifyUsageChanged` / `onUsageChanged` idiom already shipping in `utils/usage.ts`,
 * for the same reason: a bare signal, no payload, best-effort. Listeners re-read from the server
 * rather than trusting anything carried on the event, so a missed signal degrades to stale data
 * that the next navigation corrects, never to a wrong write.
 */
export function notifyConnectorsChanged(): void {
  try {
    window.dispatchEvent(new CustomEvent(CONNECTORS_CHANGED))
  } catch {
    // window/CustomEvent unavailable (SSR/tests) — best-effort only, exactly as usage.ts is.
  }
}

/** Subscribe to connector-changed signals. Returns an unsubscribe function. */
export function onConnectorsChanged(handler: () => void): () => void {
  if (typeof window === 'undefined') return () => {}
  window.addEventListener(CONNECTORS_CHANGED, handler)
  return () => window.removeEventListener(CONNECTORS_CHANGED, handler)
}

/**
 * This module's binding of the shared required-string reader — the noun is fixed here, once.
 * A missing required field means the server broke its own contract, not an absent value.
 */
const readString = (value: unknown, field: string): string =>
  requiredString(value, 'connector', field)

/**
 * The state, or a throw. NOT a fallback to `neverAsked`: that would put a `Request access` button
 * in front of somebody the server has already answered, and their click would earn a 409.
 */
function readState(value: unknown): ConnectorState {
  if (value === 'neverAsked' || value === 'pending' || value === 'approved' || value === 'declined') {
    return value
  }
  throw new ApiError('The server sent a connector state this app does not recognise.', 500)
}

/**
 * A consent panel off the wire, or a throw — the strict half, not the catalog's forgiving one. Shared with the admin client, which reads the
 * APPROVER set off its own queue rows — one parser, because the citizen's panel and the
 * administrator's are the same shape carrying different promises, and a shape check that can
 * disagree between the two surfaces is worse than no check at all.
 *
 * AN EMPTY LIST IS A BREAK TOO, not an absent value. This is the copy that tells somebody what
 * they are consenting to before they ask for it: a lowercase key standing in for a display name
 * is still legible, but a connector that promises nothing is not a thinner panel — it is a
 * consent box with a heading and no consent under it. Dropping a malformed LINE would be worse
 * still, leaving two of three promises on screen with nothing admitting the third went missing.
 * Both land in the dialog's error-and-retry state, which is honest.
 */
export function readConsentLines(
  value: unknown,
  subject: string,
  field: string,
): readonly ConsentLine[] {
  if (!Array.isArray(value) || value.length === 0) {
    throw new ApiError(`The server sent a ${subject} we could not read (${field}).`, 500)
  }
  return value.map((line: unknown) => {
    const row = isRecord(line) ? line : {}
    return {
      lead: requiredString(row.lead, subject, `${field}.lead`),
      body: requiredString(row.body, subject, `${field}.body`),
    }
  })
}

/** A count that is genuinely absent stays absent; anything unreadable is treated the same way. */
function toEntry(value: unknown): ConnectorEntry {
  const row = isRecord(value) ? value : {}
  return {
    key: readString(row.key, 'key'),
    displayName: readString(row.displayName, 'displayName'),
    // Not required: a connector with no one-line description renders a row with no subtitle,
    // which is a thinner row rather than an unreadable one.
    subtitle: typeof row.subtitle === 'string' ? row.subtitle : '',
    // Required, unlike `subtitle` above: a row with no one-line label is a thinner row, but an
    // ask panel with no opening sentence is a panel that will not say what it is asking about.
    askSubtitle: readString(row.askSubtitle, 'askSubtitle'),
    consentLinesRequester: readConsentLines(row.consentLinesRequester, 'connector', 'consentLinesRequester'),
    state: readState(row.state),
    askedAt: optionalString(row.askedAt),
    approvedAt: optionalString(row.approvedAt),
    approvedByName: optionalString(row.approvedByName),
    onProjectCount: optionalCount(row.onProjectCount),
    decidedAt: optionalString(row.decidedAt),
    decidedByName: optionalString(row.decidedByName),
    decisionRemarks: optionalString(row.decisionRemarks),
  }
}

/** The envelope, unwrapped. A body with no `connectors` array is a break, not an empty list. */
function toEntries(body: unknown): ConnectorEntry[] {
  const doc = isRecord(body) ? body : {}
  if (!Array.isArray(doc.connectors)) {
    throw new ApiError('The server sent an integrations list we could not read.', 500)
  }
  return doc.connectors.map(toEntry)
}

const jsonOpts = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body ?? {}),
})

/**
 * Every system this platform can connect to, and where you stand with each one.
 *
 * One entry per catalogue connector, always — a connector you have never asked about comes back
 * as `neverAsked`, because this is the whole of what Integrations offers rather than a list of
 * your grants.
 */
export async function listConnectors(deps: AuthFetchDeps = {}): Promise<ConnectorEntry[]> {
  const res = await authFetch('/api/connectors', {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load your integrations')
  return toEntries(await res.json())
}

/**
 * Ask an administrator for access to one connector, for yourself.
 *
 * `remarks` is required, 5 to 50 words by the shared rule in `utils/words.ts`. Refused with a 409
 * (`already_pending` / `already_decided`) if you are already waiting or have already been
 * answered; the message the server sends is the one to show.
 */
export async function requestConnectorAccess(
  connectorKey: string,
  remarks: string,
  deps: AuthFetchDeps = {},
): Promise<ConnectorEntry> {
  const res = await authFetch(
    `/api/connectors/${encodeURIComponent(connectorKey)}/request`,
    jsonOpts('POST', { remarks }),
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to ask for access')
  return toEntry(await res.json())
}

/**
 * Withdraw your own waiting request. Only a request still waiting can be withdrawn — one an
 * administrator has already answered is refused with `409 nothing_pending` rather than silently
 * accepted. Returns the connector in its new state.
 */
export async function cancelConnectorRequest(
  connectorKey: string,
  deps: AuthFetchDeps = {},
): Promise<ConnectorEntry> {
  const res = await authFetch(
    `/api/connectors/${encodeURIComponent(connectorKey)}/cancel`,
    jsonOpts('POST'),
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to cancel your request')
  return toEntry(await res.json())
}

// --- the project's switch and its days ------------------------------------------
//
// A SECOND FAMILY IN THIS MODULE, mirroring `api/v1/connectors/schemas.py`, which splits at the
// same line and for the same reason. Everything above answers "where does this PERSON stand with
// this connector"; everything below answers "what does this PROJECT read". `ConnectorStates`
// draws the two as separate state machines, and that split is why one administrator's answer
// covers every project somebody owns.

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
 * EVERY FIELD IS AN ANSWER, NOT AN INPUT (R13). `start`, `end` and `days` are post-clamp, so the
 * chip and the rail's `Reading N days of flight data` are the same numbers from the same
 * emitter. Nothing in this portal recomputes any of them, and `ConnectorProjectsPanel.test.tsx`
 * feeds a resolved window deliberately inconsistent with its stored pair to prove it.
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

/** One of the caller's projects, on the drill-down list behind an approved connector row. */
export interface ConnectorProjectEntry {
  projectId: string
  name: string
  enabled: boolean
  /** `null` for a project this connector was never switched on in — there is no window to draw. */
  window: ConnectorWindow | null
}

/**
 * Every project the caller owns for one connector, and whether that IS all of them.
 *
 * `truncated` is not decoration: nothing bounds a citizen's project count, the server stops at
 * 200, and a silent prefix would be a list that lies about being the whole list.
 */
export interface ConnectorProjectList {
  projects: ConnectorProjectEntry[]
  truncated: boolean
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
 * One registry connector as ONE PROJECT sees it — the rail's DATA row, and the answer every
 * write returns.
 *
 * `enabled` IS THE SWITCH POSITION AND `effectivelyOn` IS WHETHER IT READS. Different facts, both
 * shipped: the switch renders `enabled`, and anything meaning "this project can see the data"
 * reads `effectivelyOn`. A client computing `enabled && state === 'approved'` would be a second
 * home for a conjunction the resolver owns.
 */
export interface ProjectConnectorEntry {
  key: string
  displayName: string
  /**
   * What this connector's data is called, lowercase, for the rail's two state sentences. It
   * comes off the wire rather than living in the component for the same reason the ask panel's
   * copy does: a second connector must cost a registry entry and nothing else (R18).
   */
  dataNoun: string
  state: ConnectorState
  /** `pending` only: when this person asked. */
  askedAt: string | null
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

function toProjectRow(value: unknown): ConnectorProjectEntry {
  const row = isRecord(value) ? value : {}
  return {
    projectId: readString(row.projectId, 'projectId'),
    name: readString(row.name, 'name'),
    enabled: readBoolean(row.enabled, 'enabled'),
    window: toWindow(row.window),
  }
}

function toProjectConnector(value: unknown): ProjectConnectorEntry {
  const row = isRecord(value) ? value : {}
  return {
    key: readString(row.key, 'key'),
    displayName: readString(row.displayName, 'displayName'),
    dataNoun: readString(row.dataNoun, 'dataNoun'),
    state: readState(row.state),
    askedAt: optionalString(row.askedAt),
    enabled: readBoolean(row.enabled, 'enabled'),
    effectivelyOn: readBoolean(row.effectivelyOn, 'effectivelyOn'),
    window: toWindow(row.window),
  }
}

/**
 * Every registry connector as ONE project sees it — the read behind the rail's DATA section.
 *
 * THE MIRROR IMAGE OF `listConnectorProjects` BELOW, and the pair is the whole of the feature's
 * two axes: this one fixes the project and walks the connectors, that one fixes the connector and
 * walks the projects. Both hand back rows the same `ProjectConnectorRow` renders.
 *
 * IT IS RE-READ, NEVER CACHED (origin Q12). Days resolve against today, an administrator's
 * approval can land between two visits, and this portal has no query cache to invalidate — so
 * the section fetches on every project navigation and again whenever the Integrations dialog
 * closes over it. That guaranteed pre-load moment is why the section owns a skeleton.
 *
 * STRICT, LIKE THE REST OF THIS MODULE: an unreadable row is a contract break and throws, because
 * the state is what selects which sentence and which control a row draws, and a dropped row would
 * silently remove the only data source this project has.
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
  if (!res.ok) throw await readApiError(res, 'Failed to load this project’s data settings')
  const body: unknown = await res.json()
  const doc = isRecord(body) ? body : {}
  if (!Array.isArray(doc.connectors)) {
    throw new ApiError('The server sent a data list we could not read.', 500)
  }
  return doc.connectors.map(toProjectConnector)
}

/**
 * Every project you own, with this connector's switch and days in each one — newest first.
 *
 * A project you have never switched this connector on in is still here, with `enabled: false`
 * and a `null` window: the list is your projects, not your switches, because switching one on is
 * the whole reason the panel opens. Having no projects at all is an empty list, not a failure —
 * an administrator can approve somebody before they have made anything.
 */
export async function listConnectorProjects(
  connectorKey: string,
  deps: AuthFetchDeps = {},
): Promise<ConnectorProjectList> {
  const res = await authFetch(
    `/api/connectors/${encodeURIComponent(connectorKey)}/projects`,
    {},
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to load your projects')
  const body: unknown = await res.json()
  const doc = isRecord(body) ? body : {}
  if (!Array.isArray(doc.projects)) {
    throw new ApiError('The server sent a project list we could not read.', 500)
  }
  return {
    projects: doc.projects.map(toProjectRow),
    // Absent reads as "not truncated": the server defaults it to `false`, and treating a missing
    // flag as `true` would put a cap notice over a four-project list.
    truncated: doc.truncated === true,
  }
}

/**
 * Switch a connector on or off for one project, and set the days it reads. One write, both facts.
 *
 * `window` omitted KEEPS whatever the project already had — so switching off and back on returns
 * the range the citizen chose rather than silently re-picking a default. Sending one replaces the
 * stored window outright.
 *
 * Refused with `403 access_not_approved` for anyone an administrator has not approved, `404` for
 * a project you do not own or a connector that is not in the catalogue, and `422` for a day count
 * the connector does not offer. The message the server sends is the one to show.
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
  if (!res.ok) throw await readApiError(res, 'Failed to save this project’s data settings')
  return toProjectConnector(await res.json())
}
