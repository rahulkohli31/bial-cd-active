/**
 * WHAT A PUBLISH STATE LOOKS LIKE AND WHAT IT SAYS — one map, two surfaces.
 *
 * ═══ WHY THIS IS ITS OWN MODULE (plan 002, U4) ═══
 *
 * The boards give the app's status TWO renderings: an always-visible APP STATUS panel in the
 * project rail, and a chip beside the title in the toolbar row. The panel is the fuller surface
 * and the chip is a summary of it, and the requirement is not that they look alike — it is that
 * they can never SAY different things.
 *
 * A shared component would not have given that. They differ in shape (a panel with three
 * provenance rows against a pill with a popover behind it) and in lifetime (the panel is on the
 * project screen, the chip is on both). What they must share is the DECISION: which words, which
 * colour, which action, which rows. So the decision is here, as pure functions over the one
 * server-computed field, and each surface renders it in its own shape.
 *
 * ═══ THE RULE THAT CAME WITH IT, UNCHANGED ═══
 *
 * `presentationFor` switches on `publishState` and on NOTHING ELSE. A client that recombines a
 * server decision from parts has produced the same class of bug four times in this one feature,
 * most recently promising "this can publish automatically" moments before the server routed the
 * app to an administrator. `status`, `unpublishedAt`, `failureCode`, the approval lineage and the
 * pin are all still on the wire for the version ROWS to render — not one of them decides what
 * state the app is in.
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
 * Which version this state is ABOUT. Every one comes from a column the status read already
 * selects — the registry row's submission and approval stamps, the deployment row's head
 * and timestamps. There is deliberately no "your latest saved version" row: the server
 * spends its one object-store metadata HEAD on the drift comparison and serves the answer,
 * not the head, so no saved commit reaches this client to render.
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
 * THE map: one publish state in, one presentation out, ending in `assertNever` so a value
 * the server adds and this map has not labelled is a COMPILE error rather than a chip with
 * no words.
 *
 * TWO STATES DELIBERATELY SHARE THE LABEL "Approved" (reconciliation R-1.8). They are the
 * same state to a citizen — their app is approved — and R38 puts the difference exactly
 * where it belongs: on the button, `Publish` against `Send for review`, plus one sentence
 * each. Every other pair of states has different words, which is what makes the CLOSED
 * chip a complete answer: "Live", "Live · newer work saved" and "Live · couldn't check"
 * are three different things in three words each, and a citizen reading the last is not
 * being told that nothing of theirs is waiting.
 */
export function presentationFor(state: PublishState): Presentation {
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
      // THE PRIVACY CLAIM IS GONE (plan 002, U4). The sentence opened "Nobody else can see
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
      // or route. That is the R38 discipline, and it is not pedantry — the decision is
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
      // rows it referred to are one row here: the canvas drew a "YOUR LATEST" row this
      // read cannot serve. The reassurance is the half that matters and it survives.
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
      return {
        label: 'Taken offline',
        sentence:
          'An administrator has taken this app offline. Publishing again puts it back ' +
          'at the same address.',
        action: 'publish_again',
        version: 'last_published',
      }
    case 'switched_off':
      // Canvas's first sentence; its second — "It is no longer reachable" — is a
      // DEPARTURE, dropped. `disable` fails closed by severing the app's database; it does
      // not take the container down, so reachability is not a claim this platform can
      // stand behind. The remedy-less truth is the part that matters and it stays.
      return {
        label: 'Switched off',
        sentence:
          'An administrator switched this app off. Nothing can be published until ' +
          'they switch it back on.',
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
 * THE NINE STATES' COLOURS, from `StatusCardStates` — a text/ground pair and a leading dot each.
 *
 * THE COLOUR IS THE SIGNAL AND IT WAS ENTIRELY MISSING. Every state rendered one neutral grey
 * pill, so "Draft" was chromatically indistinguishable from "Changes requested" and from
 * "Didn't start". The board is a whole artboard devoted to this, titled "The status chip, nine
 * states", with nine `color:`/`background:`/dot triples on it.
 *
 * SIX FAMILIES COVER THIRTEEN STATES, because three pairs share a look and differ only in their
 * words, and four of the portal's states have no board at all:
 *
 *   `approved_ready_to_publish` / `approved_needs_review_again` take the GREEN of "Starting up",
 *   because what they have in common with it is the platform having said yes. The difference
 *   between the two is on the button, which is where R38 puts it.
 *
 *   `live_drift_unknown` is green like the other two live states — the app IS live, and the
 *   thing that could not be checked is in the label, not in the colour. Painting it amber would
 *   say something is wrong with the app when nothing is.
 *
 *   `taken_offline` shares `switched_off`'s off-grey. Both are down; only one has a remedy, and
 *   again that difference is on the button.
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
 * THE PANEL'S PROVENANCE ROWS — what is published, what was approved, and what the citizen last
 * saved, each with its date and its short build id.
 *
 * THE SAVED ROW IS WHY U4 NEEDED A SERVER FIELD. Everything else here comes from columns the
 * status read already selects; the citizen's own last save did not reach this client at all,
 * because the server spent its one metadata HEAD on the drift comparison and returned only the
 * verdict. It returns the head and its timestamp now.
 *
 * ITS LABEL CHANGES WITH THE STATE, exactly as the boards draw it: "YOUR LATEST" where something
 * is live, because the row exists to be CONTRASTED with what is serving; "LAST SAVED" where
 * nothing is, because there is nothing to contrast it with.
 *
 * AND ITS COLOUR IS THE DRIFT SIGNAL. `live_newer_work` prints the date in #B45309 — the only
 * amber TEXT the canvas uses anywhere — because that is the state where what is live and what is
 * theirs are two different versions. Every other state prints it in ink.
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
}

/**
 * The citizen's own save, as a row — and the ONE case worth spelling out.
 *
 * THE TWO HALVES ARE INDEPENDENTLY NULL. A bundle written before the metadata stamp exists still
 * has a last-modified on the object, so the store can say WHEN without saying WHICH. That mixed
 * case is not hypothetical and it is not an error: the row prints its date and says the version
 * is unknown, rather than printing a blank or inventing an id.
 *
 * BOTH NULL is the "cannot tell" rendering: nothing has ever been saved, or the store would not
 * answer. Either way the row must not read as "saved just now with no id".
 */
export function savedRow(deployment: DeploymentView | null, label: string, tone: RowTone): ProvenanceRow {
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

  switch (state) {
    // NOTHING TO SHOW, AND NOT BECAUSE A FETCH IS MISSING. A project with nothing built has no
    // version of anything; a publish in flight and a switched-off app both have nothing a row
    // could honestly date.
    case 'nothing_built':
    case 'starting_up':
    case 'switched_off':
      return []
    case 'draft':
    case 'changes_requested':
    case 'did_not_start':
      return [savedRow(deployment, 'LAST SAVED', 'ink')]
    case 'in_review':
      return [submitted, savedRow(deployment, 'LAST SAVED', 'ink')]
    case 'approved_ready_to_publish':
    case 'approved_needs_review_again':
      return [approved, savedRow(deployment, 'LAST SAVED', 'ink')]
    // THE THREE ROWS THE BOARD DRAWS, and the one that is amber. Only `live_newer_work` is
    // KNOWN to have drifted: `live_current` knows the two agree, and `live_drift_unknown` is
    // the state where the server could not tell — and a colour that says "yours is newer"
    // there would be the same false claim in the other direction.
    case 'live_current':
      return [published, approved, savedRow(deployment, 'YOUR LATEST', 'ink')]
    case 'live_newer_work':
      return [published, approved, savedRow(deployment, 'YOUR LATEST', 'drift')]
    case 'live_drift_unknown':
      return [published, approved, savedRow(deployment, 'YOUR LATEST', 'ink')]
    case 'taken_offline':
      return [
        { ...published, label: 'LAST PUBLISHED', url: null },
        savedRow(deployment, 'LAST SAVED', 'ink'),
      ]
    default:
      return assertNever(state)
  }
}
