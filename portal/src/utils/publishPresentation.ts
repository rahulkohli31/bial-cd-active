/**
 * ONE map, two surfaces: an always-visible APP STATUS panel in the project rail, and a status
 * chip in the toolbar. They differ in shape and lifetime, but must never SAY different things —
 * so the decision (which words, colour, action, row) lives here as pure functions over one
 * server-computed field, and each surface only renders it.
 *
 * `presentationFor` switches on `publishState` and on NOTHING ELSE. A client that recombines a
 * server decision from parts has produced this same bug four times — most recently promising an
 * auto-publish moment before the server routed the app to an administrator. `status`,
 * `unpublishedAt`, `failureCode`, the approval lineage and the pin stay on the wire only for the
 * version ROWS to render.
 */
import { assertNever } from './assertNever'
import type { ApprovalState, DeploymentView, PublishState } from './deployApi'

/**
 * What a press will ATTEMPT. Every one of these except `take_it_back` is the same request
 * through the same questionnaire — the ladder requires a completed declaration on every
 * attempt, so there is no second path and no client-side threshold check. They differ only
 * in what the button honestly promises.
 */
export type ActionKind =
  | 'send_for_review'
  | 'publish'
  | 'send_update_for_review'
  | 'publish_again'
  | 'try_again'
  | 'take_it_back'

export const ACTION_LABEL: Record<ActionKind, string> = {
  send_for_review: 'Send for review',
  publish: 'Publish',
  send_update_for_review: 'Send update for review',
  publish_again: 'Publish again',
  try_again: 'Try again',
  take_it_back: 'Take it back',
}

/**
 * `take_it_back` is the one action NOT painted teal (`#0D7377`, `StatusCardStates`' primary-action
 * colour): every other action moves the app FORWARD, but taking a submission back moves it out of
 * an administrator's queue, and the encouraging colour would ask a citizen to withdraw their own
 * work in the same voice that asked them to submit it. Read by both surfaces that draw an action —
 * the rail panel and the chip's popover — so the two cannot disagree.
 */
export const SECONDARY_ACTIONS: ReadonlySet<ActionKind> = new Set<ActionKind>(['take_it_back'])

/**
 * Which version this state is ABOUT — the ONE version the chip's popover names, drawn from
 * columns the status read already selects.
 *
 * THE CITIZEN'S OWN SAVE IS DELIBERATELY NOT ONE OF THESE: this type answers "which version is
 * this state about" (one answer), while the saved version exists to be CONTRASTED with it — a
 * list, not a row, so it belongs to `provenanceRows` and the rail panel instead.
 */
export type VersionRow = 'none' | 'submitted' | 'submitted_with_note' | 'approved' | 'live' | 'last_published'

export interface Presentation {
  /** The chip's own words, drift included, so the closed chip is a complete answer. */
  label: string
  /** Exactly one sentence. */
  sentence: string
  /** At most one action — or none at all, which is a state with nothing to do rather than
   *  a control that is temporarily away. Those get NO button, never a disabled one. */
  action: ActionKind | null
  version: VersionRow
}

/**
 * THE FAILURE CODES A RESTART WRITES. `did_not_start` is the state for both a first deploy that
 * never came up and a restart that did not come back, and those are not the same event to the
 * person reading them: the first has never had a working version, the second had one a minute
 * ago. The code is the only thing that tells them apart.
 */
export const RESTART_FAILED_CODES: ReadonlySet<string> = new Set([
  'restart_failed',
  'restart_not_ready',
])

/**
 * THE map: one publish state in, one presentation out, ending in `assertNever` so an unlabelled
 * state is a COMPILE error. TWO STATES DELIBERATELY SHARE THE LABEL "Approved" (the difference
 * is on the button/sentence); every other pair differs in words, so the CLOSED chip stays a
 * complete answer — "Live", "Live · newer work saved" and "Live · couldn't check" are three
 * different things, and the last never reads as "nothing of yours is waiting".
 *
 * `failureCode` CORRECTS EXACTLY ONE STATE and is optional for that reason: every caller that
 * has the deployment in hand should pass it, and a caller that does not still gets the shipped
 * answer for all thirteen. It is read HERE rather than at a surface so the chip and the panel
 * cannot come to describe one failed restart in two ways.
 */
export function presentationFor(state: PublishState, failureCode: string | null = null): Presentation {
  // A FAILED RESTART IS NOT A FAILED FIRST DEPLOY, and only this one state is renamed. An
  // administrator's lockout and a pending submission outrank the deployment row server-side, so
  // a code that outlived its attempt must not shout over "Switched off" or "In review" —
  // `did_not_start` is the single state a failed restart is spoken as wrongly.
  if (state === 'did_not_start' && failureCode !== null && RESTART_FAILED_CODES.has(failureCode)) {
    return {
      label: 'Could not restart',
      // No "try again": a restart runs the SAME version, so a restart that keeps failing is an
      // application whose own code is the fault. Pressing it again is waiting for nothing.
      sentence:
        'Your app did not come back up. A restart runs the same version again, so if it keeps ' +
        'failing the fault is in the app itself — describe the fix in a chat and send it for review.',
      action: null,
      version: 'last_published',
    }
  }
  return presentationForState(state)
}

function presentationForState(state: PublishState): Presentation {
  switch (state) {
    case 'nothing_built':
      // Canvas, verbatim.
      return {
        label: 'Nothing built yet',
        sentence: "Describe what you need in a chat and I'll build it.",
        action: null,
        version: 'none',
      }
    case 'draft':
      // Canvas's label, verbatim — "Draft" survived two earlier words: "Ready to send"
      // described a button rather than the app, and "Only you can see it" made a privacy
      // claim nobody asked this chip to make.
      //
      // THE PRIVACY CLAIM IS GONE. The sentence opened "Nobody else can see
      // this yet", which is the same kind of assertion the board's own notes record being
      // retired one word earlier: "Only you can see it" described WHO CAN REACH the app,
      // "which sounds like a privacy setting, and is a claim nobody asked the chip to
      // make". It is the review that a citizen needs to know about here, not the audience.
      //
      // WHAT REPLACES IT IS THE BOARD'S REVIEW SENTENCE, WITH ONE CLAUSE MADE TRUE. The
      // board writes "Every app is checked by an administrator before it goes live", and
      // that is NOT true unconditionally: ladder rule 7 publishes unattended when nothing
      // on the declaration is weighted, and `AppStatus.APPROVED` is written in exactly one
      // place — the admin approve route — so no administrator is involved at all on that
      // path. Promising a review that will not happen is the same class of untrue
      // assertion about server behaviour that this whole feature exists to stop making; it
      // just happens to run in the reassuring direction. So the review is stated as the
      // board states it and the condition it actually carries is kept beside it, and the
      // sentence still says what a press ATTEMPTS rather than what the server will decide.
      return {
        label: 'Draft',
        sentence:
          "Send this version when you're happy with it. If it handles anything sensitive, " +
          'an administrator checks it before it goes live.',
        action: 'send_for_review',
        version: 'none',
      }
    case 'in_review':
      // Canvas, minus its date — the version row below carries that, and saying it twice
      // in two formats is how two sources of one fact start.
      return {
        label: 'In review',
        sentence:
          'This version is with an administrator. You can carry on making changes — ' +
          'what you sent is already a copy.',
        action: 'take_it_back',
        version: 'submitted',
      }
    case 'changes_requested':
      // Canvas, verbatim. The note itself is rendered below it, in the flow, because a
      // note that lives only somewhere else is a note you can publish straight past.
      return {
        label: 'Changes requested',
        sentence: 'An administrator asked for changes. Make them, then send it again.',
        action: 'send_for_review',
        version: 'submitted_with_note',
      }
    case 'approved_ready_to_publish':
      // NO ARTBOARD. Adapted from the retired review card's approved arm with its
      // lineage promise removed: it says an administrator approved this version and that
      // pressing Publish is the next step, and it does NOT say whether that will publish
      // or route. That is the discipline, and it is not pedantry — the decision is
      // taken inside the request, against a tree a `saveFirst` can move first, so no read
      // taken before the press can honestly promise either outcome.
      return {
        label: 'Approved',
        sentence:
          'An administrator approved this version. Publishing it is the next step, ' +
          'and it is yours to take.',
        action: 'publish',
        version: 'approved',
      }
    case 'approved_needs_review_again':
      // NO ARTBOARD. Its whole job is to say that THIS version goes back to an
      // administrator, without implying anything about what the other approved state's
      // press would do.
      return {
        label: 'Approved',
        sentence:
          'An administrator approved an earlier version of this app. What you have now ' +
          'goes back to an administrator before it can go live.',
        action: 'send_for_review',
        version: 'approved',
      }
    case 'starting_up':
      // Canvas, with two DEPARTURES. Its opening "Approved." goes: an app published
      // unattended under ladder rule 7 was never approved by anyone, and this state is
      // reached both ways. And its own opening verb phrase is reworded, because it was
      // word-for-word one of the pipeline's retired phase labels — the vocabulary this
      // plan deletes rather than restyles, and which a guard greps the tree for. While a
      // publish runs the chip says "Starting up" and stops there.
      return {
        label: 'Starting up',
        sentence: 'Your app is coming up now — usually a few minutes. Nothing to do.',
        action: null,
        version: 'none',
      }
    case 'live_current':
      // Canvas's "The two agree — nothing of yours is waiting", rewritten because the two
      // rows it referred to are one row in the CHIP: its popover names a single version, and
      // the canvas's "YOUR LATEST" row is drawn by the rail panel from `provenanceRows`. The
      // reassurance is the half that matters here and it survives.
      return {
        label: 'Live',
        sentence:
          'What is live is the version you last saved — nothing of yours is waiting.',
        action: null,
        version: 'live',
      }
    case 'live_newer_work':
      // Canvas: its explanation and its reassurance, both — with ONE WORD CORRECTED. The
      // canvas says "an approval is pinned to one exact build" and "keeps serving the
      // APPROVED version", and neither is true of an app that published unattended under
      // ladder rule 7: no administrator was involved, and `approved_commit_sha` is NULL.
      // What IS true either way is that one exact BUILD is live and saving does not change
      // which. That is the whole substance of the explanation, so nothing is lost by saying
      // the true version of it.
      //
      // The reassurance's second half stands as a statement about server behaviour, because
      // routing pins a submission and publishes nothing — the live build keeps serving
      // throughout, whichever way it got there.
      return {
        label: 'Live · newer work saved',
        sentence:
          'What is live is one exact build, so anything you have saved since is a ' +
          'different version. Your live app keeps serving that build the whole time a ' +
          'new one is being checked.',
        action: 'send_update_for_review',
        version: 'live',
      }
    case 'live_drift_unknown':
      // NO ARTBOARD, and written as an OCCASIONAL LAPSE rather than a standing state: it
      // is reached only when the server's storage read would not answer, or when the saved
      // bundle predates the version stamp. Phrased in the moment on purpose — a citizen
      // must not read this as a property of their app or of the platform, because they
      // will not see it again. It offers the same action a drifted app offers: withholding
      // one would strand somebody who did save, and saying "nothing of yours is waiting"
      // would be the exact false reassurance this feature keeps shipping.
      return {
        label: "Live · couldn't check",
        sentence:
          'Your app is live. We could not check just now whether anything newer of ' +
          'yours is saved — try again in a minute. Your live app keeps serving the ' +
          'build it is on the whole time a new one is being checked.',
        action: 'send_update_for_review',
        version: 'live',
      }
    case 'taken_offline':
      // NO ARTBOARD. Verbatim from the retired Publish card, which had it right: a
      // taken-down app has a working remedy and a switched-off one does not, and
      // collapsing the two into one word would remove that remedy silently.
      //
      // THE SENTENCE NAMES NO ACTOR, because there are two. This state is now reachable by the
      // owner's own take-down as well as by an administrator's, and telling an owner that an
      // administrator did what they just did themselves is worse than saying nothing about who.
      // What does not change is the remedy, which is the half that matters.
      return {
        label: 'Taken offline',
        sentence:
          'This app is not running in production. Publishing again puts it back at the ' +
          'same address.',
        action: 'publish_again',
        version: 'last_published',
      }
    case 'switched_off':
      // Canvas's first sentence; its second — "It is no longer reachable" — is a
      // DEPARTURE, dropped. `disable` fails closed by severing the app's database; it does
      // not take the container down, so reachability is not a claim this platform can
      // stand behind. The remedy-less truth is the part that matters and it stays.
      //
      // IT NO LONGER MENTIONS PUBLISHING. The kill switch used to reach approved apps only,
      // so "nothing can be published" was the whole of what it meant; it now reaches DRAFT
      // and REJECTED apps too, and to the owner of an app that has never been published —
      // the ordinary case — that sentence named a consequence they were not pursuing and
      // left the one they are hitting unsaid.
      //
      // The second sentence is the true one and it is deliberately the WIDER claim: the
      // workspace refuses to start, and every turn of every kind is refused with it, at
      // `resolve_app_for_project` — the one site both doors run through. Save is the
      // documented exception and is NOT refused, because refusing it would destroy unsaved
      // work in a live container; that is a deliberate trade rather than a gap in the
      // sentence, and nothing consumes the snapshot it lets advance.
      return {
        label: 'Switched off',
        sentence:
          'An administrator switched this app off. You cannot make changes to it ' +
          'until they switch it back on.',
        action: null,
        version: 'none',
      }
    case 'did_not_start':
      // Canvas, minus BOTH of its assertions, and the same fact retires them both: an app
      // that published unattended under ladder rule 7 was never seen by an administrator,
      // and `approved_commit_sha` is NULL for every one of them — the common case.
      //
      // So "Trying again does not go back to an administrator" is cut (it is true only
      // while an approval pin still matches, and usually there is no pin), and so is the
      // canvas's "It WAS APPROVED but would not start", which states outright that somebody
      // signed this off. This state is reached from any failed deployment with a non-routed
      // code, including one a draft app started itself. What is left says only what
      // happened, which is all the citizen needs to press the button below.
      return {
        label: "Didn't start",
        sentence: 'The publish got as far as starting your app up, and then stopped.',
        action: 'try_again',
        version: 'none',
      }
    default:
      return assertNever(state)
  }
}

/**
 * IS THERE A CONTAINER SERVING RIGHT NOW — the precondition the restart and take-down routes
 * enforce, answered HERE because this module is the one that owns what a publish state means.
 *
 * A surface grouping these three states itself would be a second author of the answer, and the
 * retirement guard's whole subject is client-side predicates that re-decide what the server has
 * already decided. This one does not re-derive anything: it reads the server's own computed
 * field, and it exists so exactly one place has to be edited when a fourteenth state arrives.
 *
 * `starting_up` IS NOT ONE OF THEM. A deploy in flight has no revision to recycle and the server
 * refuses both operations while one runs — offering a control in order to have it refused is what
 * teaches a citizen to distrust the screen.
 */
export function canBeRestarted(state: PublishState): boolean {
  return state === 'live_current' || state === 'live_newer_work' || state === 'live_drift_unknown'
}

/**
 * …AND TAKE DOWN IS NOT RESTART'S TWIN, which one shared predicate quietly made it.
 *
 * The two are refused on different grounds. A restart needs a revision to recycle, so it is
 * offered only where the platform can name one. A take-down needs a container to remove, and the
 * route that does it says so in as many words: it makes no status check at all, because whether an
 * application is in production is a separate question from Draft / In review / Approved.
 *
 * THE STATE THAT SEPARATES THEM IS `in_review`. Submitting a new version for review does not stop
 * the version already serving — the lifecycle arm simply outranks the deployment row when the
 * state is named — so an owner with a live application and a version in the queue was shown no way
 * to take it out of production. The server had gone as far as authoring the sentence for exactly
 * that case, saying the queued version is untouched; nothing could reach it.
 *
 * `switched_off` is deliberately NOT here. That is an administrator's kill-switch, which severs
 * the application's database as well, and offering an owner a control over a container an
 * administrator has already stopped is offering a refusal.
 */
export function canBeTakenDown(state: PublishState, hasServingRow: boolean): boolean {
  return canBeRestarted(state) || (state === 'in_review' && hasServingRow)
}

/** `25 Aug 2026, 14:20` — the canvas's form, and the half a citizen recognises. */
export function formatStamp(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  return parsed.toLocaleString(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Whether `formatStamp` can render this instant; it hands an unparseable one back unchanged. */
export function isUsableInstant(value: string | null): value is string {
  return value !== null && !Number.isNaN(new Date(value).getTime())
}

export interface VersionRowData {
  heading: string
  stamp: string | null
  sha: string | null
  /** Present only where the state says the address is worth offering. A taken-offline
   *  address would 404, and a citizen cannot tell that from an app that has broken. */
  url: string | null
  note: string | null
}

export function versionRowData(
  kind: VersionRow,
  deployment: DeploymentView | null,
  approval: ApprovalState | null,
): VersionRowData | null {
  switch (kind) {
    case 'none':
      return null
    case 'submitted':
    case 'submitted_with_note':
      return {
        heading: 'Sent for review',
        stamp: approval?.submittedAt ?? null,
        sha: approval?.submittedSha ?? null,
        url: null,
        note: kind === 'submitted_with_note' ? (approval?.rejectionNote ?? null) : null,
      }
    case 'approved':
      return {
        heading: 'Approved version',
        stamp: approval?.approvedAt ?? null,
        sha: approval?.approvedCommitSha ?? null,
        url: null,
        note: null,
      }
    case 'live':
      return {
        heading: 'Live now',
        stamp: deployment?.finishedAt ?? null,
        sha: deployment?.headSha ?? null,
        url: deployment?.url ?? null,
        note: null,
      }
    case 'last_published':
      return {
        heading: 'Last published',
        stamp: deployment?.finishedAt ?? null,
        sha: deployment?.headSha ?? null,
        // Deliberately never linked — see `taken_offline` above.
        url: null,
        note: null,
      }
    default:
      return assertNever(kind)
  }
}

/**
 * THE STATE COLOURS (`StatusCardStates`) — previously all one grey pill; now an explicit
 * text/ground pair + dot each. SIX FAMILIES COVER THIRTEEN STATES: three pairs share a look and
 * differ only in their words, and four of the portal's states have no board at all. `approved_*`
 * takes GREEN (platform said yes; the difference is on the button); `live_drift_unknown` stays
 * GREEN like the other live states (the uncertainty is in the label, not an amber that would
 * wrongly say something broke); `taken_offline` shares `switched_off`'s off-grey (only one has a
 * remedy, again on the button).
 */
export interface StateLook {
  /** Tailwind classes for the pill: its text and its ground. */
  pill: string
  /** The 6px leading dot's ground. */
  dot: string
}

const GREY: StateLook = { pill: 'text-status-grey-fg bg-status-grey-bg', dot: 'bg-status-grey-dot' }
const FAINT: StateLook = { pill: 'text-status-faint-fg bg-status-faint-bg', dot: 'bg-status-faint-dot' }
const AMBER: StateLook = { pill: 'text-status-amber-fg bg-status-amber-bg', dot: 'bg-status-amber-dot' }
const RED: StateLook = { pill: 'text-status-red-fg bg-status-red-bg', dot: 'bg-status-red-dot' }
const GREEN: StateLook = { pill: 'text-status-green-fg bg-status-green-bg', dot: 'bg-status-green-dot' }
const OFF: StateLook = { pill: 'text-status-off-fg bg-status-off-bg', dot: 'bg-status-off-dot' }

export function lookFor(state: PublishState): StateLook {
  switch (state) {
    case 'nothing_built':
      return FAINT
    case 'draft':
      return GREY
    case 'in_review':
      return AMBER
    case 'changes_requested':
    case 'did_not_start':
      return RED
    case 'approved_ready_to_publish':
    case 'approved_needs_review_again':
    case 'starting_up':
    case 'live_current':
    case 'live_newer_work':
    case 'live_drift_unknown':
      return GREEN
    case 'taken_offline':
    case 'switched_off':
      return OFF
    default:
      return assertNever(state)
  }
}

/**
 * THE PANEL'S PROVENANCE ROWS: published, approved, and the citizen's own last save, each dated
 * with a short build id. The saved row needed a NEW server field — the server previously
 * returned only the drift verdict, not the head/timestamp. Its label tracks the state ("YOUR
 * LATEST" to CONTRAST with something live, "LAST SAVED" otherwise); `live_newer_work` alone
 * prints its date in #B45309, the canvas's only amber text, since live and saved differ there.
 */
export type RowTone = 'ink' | 'drift'

export interface ProvenanceRow {
  key: string
  /** The small-caps label in the row's fixed-width first column. */
  label: string
  /** `null` where the platform genuinely does not know — rendered as "cannot tell", never blank. */
  stamp: string | null
  /** `null` for a bundle written before the metadata stamp existed. See `savedRow`. */
  sha: string | null
  tone: RowTone
  /** Offered only where the state says the address is worth pointing at. */
  url?: string | null
  /**
   * FREE TEXT INSTEAD OF A DATE AND AN ID — the reviewer's own words, and the one row
   * that is prose rather than provenance.
   *
   * IT RIDES ON THIS TYPE RATHER THAN BESIDE IT because the rail had nowhere else to put
   * it: the note reached the browser on every read and rendered only inside the dialog a
   * citizen opens when they believe they are FINISHED, which is one press too late to be
   * the thing they act on. A row carries it above the state's action so a long note
   * cannot push "Send for review" out of view.
   *
   * A ROW HAS EITHER A NOTE OR A STAMP/SHA PAIR, never both — the panel branches on this
   * being a string and draws a bounded, scrollable block instead of a dated line.
   */
  note?: string | null
}

/**
 * The citizen's own save, as a row — or NO ROW AT ALL for a project that has never saved.
 *
 * THE TWO HALVES ARE INDEPENDENTLY NULL. A bundle written before the metadata stamp exists still
 * has a last-modified on the object, so the store can say WHEN without saying WHICH. That mixed
 * case is not hypothetical and it is not an error: the row prints its date and says the version
 * is unknown, rather than printing a blank or inventing an id.
 *
 * BOTH NULL is the "cannot tell" rendering, and it now means ONE thing rather than three. It used
 * to be reached three ways — the store said there is no bundle, the store was not configured, or
 * the store raised — and the panel spoke all three as "LAST SAVED — We could not tell". On the
 * first of them that sentence is false and it is false in the frightening direction: a citizen
 * who has never saved reads it as the platform having LOST their work, on the exact panel they
 * open when they are unsure their work is safe. The backend now says which of the three happened
 * (`SavedState`), so:
 *
 *   NEVER SAVED  → no row, exactly as an app nobody approved gets no APPROVED row (see
 *                  `provenanceRows` below) and for the same reason: the absent row says the true
 *                  thing by saying nothing.
 *   ANYTHING ELSE → the row stays and says "We could not tell", which is what that wording was
 *                  written for — a save that exists and could not be read is a genuine gap.
 *
 * A `null` deployment is not "never saved" either: it is no answer at all, so it keeps the row.
 */
export function savedRow(
  deployment: DeploymentView | null,
  label: string,
  tone: RowTone,
): ProvenanceRow | null {
  if (deployment?.savedState === 'never_saved') return null
  return {
    key: 'saved',
    label,
    stamp: deployment?.savedAt ?? null,
    sha: deployment?.savedHead ?? null,
    tone,
  }
}

/**
 * WHICH ROWS THE PANEL SHOWS FOR A STATE. Driven by the same `publishState` as everything else,
 * so a row can never describe a state the words do not.
 */
export function provenanceRows(
  state: PublishState,
  deployment: DeploymentView | null,
  approval: ApprovalState | null,
): ProvenanceRow[] {
  const published: ProvenanceRow = {
    key: 'published',
    label: 'PUBLISHED',
    stamp: deployment?.finishedAt ?? null,
    sha: deployment?.headSha ?? null,
    tone: 'ink',
    url: deployment?.url ?? null,
  }
  const approved: ProvenanceRow = {
    key: 'approved',
    label: 'APPROVED',
    stamp: approval?.approvedAt ?? null,
    sha: approval?.approvedCommitSha ?? null,
    tone: 'ink',
  }
  const submitted: ProvenanceRow = {
    key: 'submitted',
    label: 'SENT FOR REVIEW',
    stamp: approval?.submittedAt ?? null,
    sha: approval?.submittedSha ?? null,
    tone: 'ink',
  }

  /**
   * NO APPROVAL MEANS NO APPROVED ROW, not an APPROVED row saying "cannot tell". An unattended
   * ladder-rule-7 publish is the COMMON case with `approved_at`/`approved_commit_sha` both NULL —
   * rendering the row anyway would read as an approval whose record got lost, which never happened.
   * Either field answers it (server always writes both together). The two `approved_*` states keep
   * the row unconditionally: those states ASSERT an approval, so a missing stamp there is a genuine
   * "cannot tell" about a real event.
   */
  const wasApproved = (approval?.approvedAt ?? approval?.approvedCommitSha ?? null) !== null

  /** The saved row where there is one to draw, and nothing at all where there is not — see
   *  `savedRow`, which is where the "never saved gets no row" decision lives. */
  const saved = (label: string, tone: RowTone): ProvenanceRow[] => {
    const row = savedRow(deployment, label, tone)
    return row === null ? [] : [row]
  }

  /**
   * THE REVIEWER'S OWN WORDS, on the state that asks the citizen to act on them.
   *
   * NO NOTE MEANS NO ROW, on exactly the `wasApproved` reasoning above: an administrator may
   * reject without writing anything, and a row headed WHY whose whole value is "We could not
   * tell" would invent a note that was never written.
   */
  const rejection: ProvenanceRow | null =
    typeof approval?.rejectionNote === 'string' && approval.rejectionNote.trim().length > 0
      ? { key: 'rejection', label: 'WHY', stamp: null, sha: null, tone: 'ink', note: approval.rejectionNote }
      : null

  const liveRows = (tone: RowTone): ProvenanceRow[] => [
    published,
    ...(wasApproved ? [approved] : []),
    ...saved('YOUR LATEST', tone),
  ]

  switch (state) {
    // NOTHING TO SHOW, AND NOT BECAUSE A FETCH IS MISSING. A project with nothing built has no
    // version of anything; a publish in flight and a switched-off app both have nothing a row
    // could honestly date.
    case 'nothing_built':
    case 'starting_up':
    case 'switched_off':
      return []
    case 'draft':
    case 'did_not_start':
      return saved('LAST SAVED', 'ink')
    // THE NOTE COMES FIRST, and that ordering is the requirement rather than a preference:
    // every row here renders above the state's action, so a note capped at 1,000 characters
    // in a 360px rail must not be able to push "Send for review" below the fold. It is
    // bounded and scrollable where it is drawn (`AppStatusPanel`), and it sits above the
    // save row because it is the thing the citizen has to read before doing anything.
    case 'changes_requested':
      return [...(rejection === null ? [] : [rejection]), ...saved('LAST SAVED', 'ink')]
    case 'in_review':
      return [submitted, ...saved('LAST SAVED', 'ink')]
    case 'approved_ready_to_publish':
    case 'approved_needs_review_again':
      return [approved, ...saved('LAST SAVED', 'ink')]
    // THE ROWS THE BOARD DRAWS — three where an administrator approved the version, two where
    // nobody did (see `wasApproved`) — and the one that is amber. Only `live_newer_work` is
    // KNOWN to have drifted: `live_current` knows the two agree, and `live_drift_unknown` is
    // the state where the server could not tell — and a colour that says "yours is newer"
    // there would be the same false claim in the other direction.
    case 'live_current':
      return liveRows('ink')
    case 'live_newer_work':
      return liveRows('drift')
    case 'live_drift_unknown':
      return liveRows('ink')
    case 'taken_offline':
      return [
        { ...published, label: 'LAST PUBLISHED', url: null },
        ...saved('LAST SAVED', 'ink'),
      ]
    default:
      return assertNever(state)
  }
}
