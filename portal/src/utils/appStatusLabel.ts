/**
 * One vocabulary for what state an app is in.
 *
 * WHY THIS EXISTS
 *
 * The projects list must reuse `PublishStatusChip`'s words rather than invent a
 * second set; this module is the SUBSET a list row can prove without an N-way
 * per-row deployment fetch — same words, fewer of them, never different ones.
 *
 * TWO FACTS, NOT ONE. Whether an app is LIVE is a deployment fact ("live = deployed
 * / published, with a url" — confirmed on a call) and is NOT derivable from
 * `appStatus`: `approved` only means an admin said yes, and one-click deploy never
 * writes `status` at all, so an ordinary live app still reads `draft`. `isServing` is
 * therefore checked FIRST — computed server-side by `services/deploy/liveness.py`,
 * the same predicate the marketplace and dashboard's "In production" count use.
 *
 * A PUBLISH IN FLIGHT IS A THIRD FACT, and it outranks both: `isPublishing` is true exactly
 * when the chip reads `starting_up`, which the chip says over a live app as well as a new one.
 *
 * Wording below is the chip's, not the board mockups' ("NOT SENT"/"LIVE") — confirmed
 * on a call to use explainable, simple language over the mocks' placeholder terms.
 */
import type { AppStatus, Project } from './projectApi'

export type StatusTone = 'live' | 'review' | 'attention' | 'idle' | 'off'

export interface StatusLabel {
  label: string
  tone: StatusTone
}

/** `Nothing built yet` — a project whose app does not exist. The chip's own words. */
const NOTHING_BUILT: StatusLabel = { label: 'Nothing built yet', tone: 'idle' }

/** The chip's words and colour for `starting_up`. */
const STARTING_UP: StatusLabel = { label: 'Starting up', tone: 'live' }

const BY_STATUS: Record<AppStatus, StatusLabel> = {
  // Built, never submitted. The chip's comment records that "Draft" beat "Ready to send"
  // on the canvas, so this word has already been chosen once and should not be re-picked.
  draft: { label: 'Draft', tone: 'idle' },
  pending: { label: 'In review', tone: 'review' },
  // The citizen has something to do, which is why this is the one tone that draws the eye.
  rejected: { label: 'Changes requested', tone: 'attention' },
  // Approved but NOT serving. Deliberately distinct from `Live`: conflating them would
  // tell someone their app is reachable when it may never have been deployed.
  approved: { label: 'Approved', tone: 'review' },
  disabled: { label: 'Switched off', tone: 'off' },
}

/**
 * What to show for one project row.
 *
 * `isServing` outranks `appStatus` because it answers a different and more useful question:
 * an approved app that is serving reads `Live`, and an approved one that never deployed
 * reads `Approved`.
 */
export function statusFor(
  project: Pick<Project, 'appStatus' | 'isServing' | 'isPublishing'>,
): StatusLabel {
  if (project.isPublishing) return STARTING_UP
  if (project.isServing) return { label: 'Live', tone: 'live' }
  if (project.appStatus === null) return NOTHING_BUILT
  return BY_STATUS[project.appStatus] ?? NOTHING_BUILT
}

/** Tailwind classes per tone. Colour is never the only signal — the label always says it. */
export const TONE_CLASS: Record<StatusTone, string> = {
  live: 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-600/20',
  review: 'bg-amber-50 text-amber-700 ring-1 ring-amber-600/20',
  attention: 'bg-red-50 text-red-700 ring-1 ring-red-600/20',
  idle: 'bg-slate-100 text-slate-600 ring-1 ring-slate-500/20',
  off: 'bg-slate-100 text-slate-500 ring-1 ring-slate-500/20',
}
