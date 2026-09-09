/**
 * The administrator's connector queue: who has asked to reach a connector, and what was decided.
 *
 * THE SIBLING OF `connectorApi.ts`, NOT A SECOND COPY OF IT. That module answers "where do *I*
 * stand with this connector" and every statement behind it is scoped to the caller. This one is
 * the ONE connector surface that reads across users — every route behind it is super-admin-gated
 * — so the two are split exactly where the wire splits them, and no caller has to work out whose
 * rows it is holding.
 *
 * The conventions are that module's, on purpose: `authFetch` + `readApiError`, relative `/api/*`
 * paths (the edge rewrites them to `/v1/*`), a narrowing parse of an `unknown` body, and no
 * `zod` — it is a hoisted transitive of `@assistant-ui/react`, absent from `package.json`, so
 * reaching for it here would adopt a runtime dependency for the whole portal as a side effect of
 * one panel.
 *
 * THE PARSE IS STRICT, for the reason the citizen module's is. This is the screen an
 * administrator opens to find out what is waiting on them; a row quietly dropped for being
 * unreadable would leave a person waiting behind a queue that reads as caught up. A row we
 * cannot read is a contract break and throws — the panel has an error-and-retry state, and that
 * is the honest one.
 *
 * R18: nothing here names a connector. The key is a value, the display name rides the wire.
 */
import { ApiError, isRecord, optionalCount, optionalString, readApiError, requiredString } from './apiError'
import { authFetch } from './api'
import type { AuthFetchDeps } from './api'
import { readConsentLines } from './connectorApi'
import type { ConsentLine } from './connectorApi'

/**
 * Which table a listing asks for. `state` IS REQUIRED ON THE WIRE and there is no honest
 * default: it selects the rows *and* their order — `waiting` is oldest first (the person who has
 * waited longest is on top) and `decided` is newest decision first — so a caller that omitted it
 * would be asking for a queue with no answer to "in what order".
 *
 * A withdrawn request is in NEITHER: nobody is waiting on it and nobody decided it.
 */
export type ConnectorQueueState = 'waiting' | 'decided'

/**
 * A row's own status, mirroring the server's `ConnectorRequestStatus`. `cancelled` is a real
 * stored value that never crosses THIS wire — it is in neither listing — so it is not spelled
 * here either.
 */
export type ConnectorRequestStatus = 'pending' | 'approved' | 'declined'

/**
 * One request as the queue sees it — one shape for both tables, with the fields outside a row's
 * own status reading `null`.
 *
 * `displayName` IS NEVER NULL, AND A SECOND FALLBACK HERE WOULD BE A BUG. `users.display_name`
 * is nullable and the SERVER already substitutes the work email, in the one place that rule
 * lives. A browser-side `displayName || email` would be a second emitter of one substitution,
 * free to drift the day either side is edited.
 *
 * `usingItIn` IS `null` ON A WAITING OR DECLINED ROW AND `0` IS A REAL ANSWER — an approved
 * person who has not switched it on anywhere yet. The board draws an em dash for a decline,
 * which is a different statement from "none", so the two must not be folded together.
 */
export interface ConnectorRequestRow {
  id: string
  /** The person who asked. The queue is the one surface that reads across users, so this rides. */
  userId: string
  /** `users.display_name`, or the work email behind it. Never empty. */
  displayName: string
  /** The work email — the second line of the queue's person cell, in place of the board's
   *  `department`, which exists nowhere in this product and has no directory client behind it. */
  email: string
  /** The stored key. Stable, lowercase, never rendered — `connectorDisplayName` is. */
  connectorKey: string
  connectorDisplayName: string
  /**
   * `WHAT APPROVING GIVES THEM` — the registry's THIRD-PERSON consent set, which is a different
   * tuple from the citizen's `consentLinesRequester` and not derivable from it.
   *
   * IT RIDES THE ROW BECAUSE THE DECIDE DIALOG IS HANDED A ROW AND NOTHING ELSE. A component
   * that spelled these three sentences would make "add a second connector" a component change,
   * which is the claim R18 makes and the exact defect the citizen's half of this wire has
   * already been repaired for once.
   */
  consentLinesApprover: readonly ConsentLine[]
  /** The citizen's own words, in full. Rendered untruncated, as plain text, never markdown. */
  requesterRemarks: string
  askedAt: string
  status: ConnectorRequestStatus
  /** The four decided-only fields. `null` on a waiting row. */
  decidedAt: string | null
  /**
   * WHY THE ID RIDES AND NOT JUST THE NAME: BIAL runs two super-admins, so `WHEN` may read
   * `you` only to the administrator who actually made that decision. The panel compares this
   * against the signed-in profile; the other administrator's rows carry their name.
   */
  decidedById: string | null
  /** The decider's name or email. `null` when the administrator who decided has since been
   *  deleted — a decision outlives its decider, and the row keeps its date unnamed. */
  decidedByName: string | null
  /** Written only on a decline. `null` on an approval is correct, not a missing write. */
  decisionRemarks: string | null
  /** `USING IT IN` — how many of THIS person's projects have THIS connector switched on. */
  usingItIn: number | null
}

/**
 * One page of one table.
 *
 * `truncated` IS NOT DECORATION: nothing bounds how many people may ask for a connector, the
 * server stops at 200, and a silent prefix would let a request wait forever behind a console
 * showing a caught-up screen.
 */
export interface ConnectorRequestPage {
  requests: ConnectorRequestRow[]
  truncated: boolean
}

/** A required wire field. Missing means the server broke its own contract, not "absent value". */
/** This module's binding of the shared required-string reader — the noun is fixed here, once. */
const readString = (value: unknown, field: string): string => requiredString(value, 'request', field)

/**
 * The status, or a throw. NOT a fallback: a row whose status we guessed would be filed under the
 * wrong heading — a decision rendered as still waiting, or the reverse — on the one screen whose
 * whole job is to tell those two apart.
 */
function readStatus(value: unknown): ConnectorRequestStatus {
  if (value === 'pending' || value === 'approved' || value === 'declined') return value
  throw new ApiError('The server sent a request state this app does not recognise.', 500)
}

/**
 * One queue row off the wire.
 *
 * `usingItIn` keeps the difference between "none" and "not asked": `0` survives as `0`, because
 * an approved person who has switched the connector on nowhere is a real answer, and folding it
 * to `null` would draw the declined row's em dash over an approval. That rule lives in
 * `optionalCount`.
 *
 * `consentLinesApprover` is read strictly, and this is the one field where degrading gracefully
 * would be worse than failing: `connectorDisplayName` falls back to the stored key on the server
 * for a connector the registry no longer offers, because a lowercase key on screen is still
 * legible — but a consent box with a heading and nothing under it asks an administrator to hand
 * somebody access to BIAL data without saying what the access is. It lands in the queue's
 * error-and-retry state instead, which is honest.
 */
function toRow(value: unknown): ConnectorRequestRow {
  const row = isRecord(value) ? value : {}
  return {
    id: readString(row.id, 'id'),
    userId: readString(row.userId, 'userId'),
    displayName: readString(row.displayName, 'displayName'),
    email: readString(row.email, 'email'),
    connectorKey: readString(row.connectorKey, 'connectorKey'),
    connectorDisplayName: readString(row.connectorDisplayName, 'connectorDisplayName'),
    consentLinesApprover: readConsentLines(row.consentLinesApprover, 'request', 'consentLinesApprover'),
    requesterRemarks: readString(row.requesterRemarks, 'requesterRemarks'),
    askedAt: readString(row.askedAt, 'askedAt'),
    status: readStatus(row.status),
    decidedAt: optionalString(row.decidedAt),
    decidedById: optionalString(row.decidedById),
    decidedByName: optionalString(row.decidedByName),
    decisionRemarks: optionalString(row.decisionRemarks),
    usingItIn: optionalCount(row.usingItIn),
  }
}

/**
 * One table of the queue.
 *
 * TWO CALLS RENDER THE SCREEN, one per table, because `state` selects the rows AND their order
 * and the two orders are opposites. `connector` narrows to one catalogue key and is refused with
 * `400 unknown_connector` if it is not one; `q` matches a person's name or work email,
 * case-insensitively, and is what bounds which 200 rows the cap returns.
 */
export async function listConnectorRequests(
  state: ConnectorQueueState,
  filters: { connector?: string | null; q?: string | null } = {},
  deps: AuthFetchDeps = {},
): Promise<ConnectorRequestPage> {
  const params = new URLSearchParams({ state })
  if (filters.connector) params.set('connector', filters.connector)
  if (filters.q) params.set('q', filters.q)
  const res = await authFetch(`/api/admin/connector-requests?${params.toString()}`, {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load the connector queue')
  const body: unknown = await res.json()
  const doc = isRecord(body) ? body : {}
  if (!Array.isArray(doc.requests)) {
    throw new ApiError('The server sent a connector queue we could not read.', 500)
  }
  return {
    requests: doc.requests.map(toRow),
    // Absent reads as "not truncated": the server defaults it to `false`, and treating a missing
    // flag as `true` would put a cap notice over a three-row queue.
    truncated: doc.truncated === true,
  }
}

/**
 * How many people are waiting on a decision — the Integrations tab badge's only source.
 *
 * A DEDICATED ROUTE RATHER THAN `requests.length` OFF THE LISTING: that listing projects up to
 * 200 rows, joins `users` twice and counts every approved person's enabled projects, and a badge
 * reading it would pay all of that — and pay MORE as the queue it reports on grows, which is
 * exactly backwards. An empty queue answers `0`, never a 404.
 */
export async function fetchWaitingConnectorCount(deps: AuthFetchDeps = {}): Promise<number> {
  const res = await authFetch('/api/admin/connector-requests/counts', {}, deps)
  if (!res.ok) throw await readApiError(res, 'Failed to load the waiting count')
  const body: unknown = await res.json()
  const doc = isRecord(body) ? body : {}
  if (typeof doc.waiting !== 'number' || !Number.isFinite(doc.waiting)) {
    throw new ApiError('The server sent a waiting count we could not read.', 500)
  }
  return Math.max(0, Math.trunc(doc.waiting))
}

// --- the two decisions ----------------------------------------------------------
//
// BOTH ARE `POST`s BEHIND `RequireCsrf`, and `authFetch` puts the double-submit header on every
// mutating call, so neither function does anything about it. Both answer with the row's new
// state and NEITHER RETURN IT: the queue's answer to "what is on screen now" is a reload of both
// tables, not a body patched into one row, and a parsed shape with no reader would be a contract
// this module claims to hold and nothing checks. The refusals are what callers act on.

/**
 * Give this person access to the connector they asked for.
 *
 * NO BODY AT ALL, and that is the `AdminReview` board's largest departure rather than an
 * omission (R10). The board draws a permanent `REQUIRED` remark over both outcomes; an approval
 * stores nothing, because an approval remark would be readable nowhere — the audit row carries
 * ids, the citizen is never shown one, and the decided table has no remarks column. Sending an
 * empty `{}` here would be a body the route does not declare and a reader would have to work out
 * was ignored.
 *
 * Refused with `409 already_decided` when another administrator answered first — its
 * `error.detail` carries `{status, decidedByName, decidedAt}`, because the server formats no
 * human-readable date and the console that renders this already formats every date on the screen
 * behind it. `409 request_cancelled` means the citizen withdrew between the render and the click,
 * and carries no `detail` at all: there is nothing measured to hand over.
 */
export async function approveConnectorRequest(
  requestId: string,
  deps: AuthFetchDeps = {},
): Promise<void> {
  const res = await authFetch(
    `/api/admin/connector-requests/${encodeURIComponent(requestId)}/approve`,
    { method: 'POST' },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to approve the request')
}

/**
 * Refuse this person's request, in words they will read.
 *
 * `remarks` is required — 5 to 50 words by the shared rule in `utils/words.ts`, which is the
 * rule the server counts with — and reaches the citizen VERBATIM on their own Integrations row.
 * A form that let a short one through would meet a `422` whose `detail[]` says the same thing
 * the counter was already showing, which is why the dialog enforces the floor before this is
 * called rather than after. The two 409s are the approve route's.
 */
export async function declineConnectorRequest(
  requestId: string,
  remarks: string,
  deps: AuthFetchDeps = {},
): Promise<void> {
  const res = await authFetch(
    `/api/admin/connector-requests/${encodeURIComponent(requestId)}/decline`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ remarks }),
    },
    deps,
  )
  if (!res.ok) throw await readApiError(res, 'Failed to decline the request')
}
