/**
 * ONE vocabulary for what state an app is in — the words, in one place.
 *
 * #158 §10 is explicit that the projects list must borrow the project page's words and
 * "should not invent a second set". `PublishStatusChip` is that first set, and it is richer
 * than anything a list row can support: it reads a per-project deployment fetch and can say
 * `Starting up`, `Live · newer work saved` and `Taken offline`. A list cannot — one request
 * per row is an N-way fan-out on the landing screen — so this module is the SUBSET the list
 * can prove, using the chip's exact words for the states they share.
 *
 * That is the whole design constraint: same words, fewer of them, never different ones.
 *
 * TWO FACTS, NOT ONE. Whether an app is LIVE is a deployment fact — settled on the #158
 * call as "live = deployed / published — if the application is published and has url" —
 * and it is NOT derivable from `appStatus`:
 *
 *   - `approved` means an administrator said yes. Nothing may ever have been deployed.
 *   - one-click deploy never writes `status` at all, so the ordinary live app is `draft`.
 *
 * So `isServing` is checked FIRST and wins. The server computes it from the deployment
 * history (`services/deploy/liveness.py`), which is the same predicate the marketplace and
 * the dashboard's "In production" count read.
 *
 * The board for #158 drew `NOT SENT` and `LIVE`; the words below are the chip's instead,
 * confirmed on the call — "the mocks are just for reference, exact terminology is not
 * finalised yet, use explainable and simple language".
 */
import type { AppStatus, Project } from './projectApi'

export type StatusTone = 'live' | 'review' | 'attention' | 'idle' | 'off'

export interface StatusLabel {
  label: string
  tone: StatusTone
}

/** `Nothing built yet` — a project whose app does not exist. The chip's own words. */
const NOTHING_BUILT: StatusLabel = { label: 'Nothing built yet', tone: 'idle' }

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
export function statusFor(project: Pick<Project, 'appStatus' | 'isServing'>): StatusLabel {
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
