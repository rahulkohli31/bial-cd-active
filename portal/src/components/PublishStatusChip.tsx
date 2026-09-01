/**
 * THE publishing surface. One chip beside the project name, one server-computed field,
 * one sentence and at most one action (R37, R38, R39).
 *
 * IT REPLACES THREE CONTROLS THAT COULD DISAGREE — the Publish card, the Review & approval
 * card and the builder's toolbar button — and, more to the point, it replaces the thing
 * that made them disagree: each of them re-decided, in the browser, something the server
 * had already decided. That mirror has produced the same class of bug four times in this
 * one feature (`docs/solutions/ui-bugs/publish-dialog-scored-unmerged-answers-2026-08-21.md`),
 * most recently promising "this can publish automatically" beside a Publish button moments
 * before the server routed the app to an administrator. Nine labels were nine assertions
 * about server behaviour. They now have one source.
 *
 * SO THE ONE RULE HERE IS: `presentationFor` switches on `publishState` and on NOTHING
 * ELSE. No status, no `unpublishedAt`, no failure code, no approval lineage, no pin. The
 * other fields on the response are still read — but only to fill in a version row the
 * state has already asked for, never to decide which state it is.
 *
 * PUBLISHING BEHAVIOUR IS UNCHANGED. The seven-rule ladder, the classification
 * questionnaire and the two successes are exactly as they were; what changed is that the
 * interface stopped guessing which of them a press will produce. The button states the
 * CEILING of what it will attempt, and the server's answer states what happened —
 * publishing directly where the button said "Send update for review" reads as the better
 * outcome, not as a contradiction.
 *
 * ── THE CONTRACT PLAN F INHERITS ────────────────────────────────────────────────────
 * Stated here so the workspace header can re-parent this component without reading a line
 * of its internals:
 *   · It takes a project id and nothing else. It reads no router state, no rail mode and
 *     no chat.
 *   · It renders INLINE at its intrinsic size — no absolute positioning, no fixed width,
 *     no sticky behaviour — so a container may lay it out however it likes.
 *   · Its popover is PORTALLED to the document body, so a header with clipped overflow or
 *     its own stacking context cannot hide it. This is load-bearing today: the builder
 *     mount sits under four nested `overflow-hidden` ancestors.
 *   · It owns its own read and its own refresh lifetime, so mounting it twice is CORRECT
 *     rather than merely tolerated.
 *   · Plan F's only obligation is to place it beside the project name and drop the mount
 *     that dies with the builder page. Nothing here changes when F collapses the screens.
 * ────────────────────────────────────────────────────────────────────────────────────
 *
 * WHERE THE COPY COMES FROM. Nine sentences are the design canvas's own, from its
 * "The status chip, nine states" board. Four states have no artboard and their copy is
 * carried across from the tree or written here, each marked at its arm. Three deliberate
 * departures from the canvas are marked the same way — the canvas is authoritative for
 * register and wording, never for a claim the endpoints cannot honestly serve.
 */
import { useCallback, useEffect, useId, useState } from 'react'
import { ChevronDown, ExternalLink } from 'lucide-react'

import { Popover, PopoverContent, PopoverTrigger } from './ui/popover'
import DataClassificationModal from './DataClassificationModal'
import { useDeployment } from '../hooks/useDeployment'
import { assertNever } from '../utils/assertNever'
import type {
  ApprovalState,
  DeployOutcome,
  DeploymentView,
  PublishState,
} from '../utils/deployApi'

/**
 * What a press will ATTEMPT. Every one of these except `take_it_back` is the same request
 * through the same questionnaire — the ladder requires a completed declaration on every
 * attempt, so there is no second path and no client-side threshold check. They differ only
 * in what the button honestly promises.
 */
type ActionKind =
  | 'send_for_review'
  | 'publish'
  | 'send_update_for_review'
  | 'publish_again'
  | 'try_again'
  | 'take_it_back'

const ACTION_LABEL: Record<ActionKind, string> = {
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
type VersionRow = 'none' | 'submitted' | 'submitted_with_note' | 'approved' | 'live' | 'last_published'

interface Presentation {
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
function presentationFor(state: PublishState): Presentation {
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
      // Canvas, verbatim. "Draft" survived two earlier words: "Ready to send" described a
      // button rather than the app, and "Only you can see it" made a privacy claim nobody
      // asked this chip to make.
      return {
        label: 'Draft',
        sentence:
          'Every app is checked by an administrator before it goes live. ' +
          "Send this version when you're happy with it.",
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
      // NO ARTBOARD. Adapted from the retired `SubmitControl`'s approved arm with its
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
      // reached both ways. And its "Starting it up now" is reworded, because that exact
      // phrase is one of the pipeline's own retired phase labels — the vocabulary this
      // plan deletes rather than restyles, and which a guard greps for. While a publish
      // runs the chip says "Starting up" and stops there.
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
      // Canvas: its explanation and its reassurance, both. The second half is a statement
      // about server behaviour and it is true — routing pins a submission and publishes
      // nothing, so the live app keeps serving the approved build throughout.
      return {
        label: 'Live · newer work saved',
        sentence:
          'An approval is pinned to one exact build, so anything you have saved since ' +
          'going live is a different version. Your live app keeps serving the approved ' +
          'version the whole time a new one is being checked.',
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
          'approved version the whole time a new one is being checked.',
        action: 'send_update_for_review',
        version: 'live',
      }
    case 'taken_offline':
      // NO ARTBOARD. Verbatim from the retired `DeployControl`, which had it right: a
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
      // Canvas, minus "Trying again does not go back to an administrator." That clause is
      // true only while the approval pin still matches, and the pin is NULL for every app
      // that published unattended under ladder rule 7 — the common case. A second field
      // value to condition one clause of one sentence is not worth its cost, so the clause
      // is CUT rather than conditioned.
      return {
        label: "Didn't start",
        sentence: 'It was approved but would not start.',
        action: 'try_again',
        version: 'none',
      }
    default:
      return assertNever(state)
  }
}

/** `25 Aug 2026, 14:20` — the canvas's form, and the half a citizen recognises. */
function formatStamp(iso: string): string {
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

interface VersionRowData {
  heading: string
  stamp: string | null
  sha: string | null
  /** Present only where the state says the address is worth offering. A taken-offline
   *  address would 404, and a citizen cannot tell that from an app that has broken. */
  url: string | null
  note: string | null
}

function versionRowData(
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
 * The answer to a press, and there is exactly ONE treatment for it because there is only
 * ever one kind of thing here: a success. Both of the ladder's outcomes resolve — `202
 * started` and `200 routed_for_review` — and being sent for review is a success, not a
 * failure of the thing the citizen just asked for. Every REFUSAL throws instead, and the
 * questionnaire renders it beside its own button with the answers still on screen, which
 * is where a citizen who has to change something is already looking.
 *
 * So this region is never red and never carries an alert role. That is not a styling
 * choice to be tidied later — it is the property three retired tests pinned.
 */
const STARTED_ANSWER = 'Publishing now — this takes a few minutes.'

export interface PublishStatusChipProps {
  projectId: string
}

export default function PublishStatusChip({
  projectId,
}: PublishStatusChipProps): React.ReactElement {
  const {
    deployment,
    approval,
    loadError,
    unsaved,
    saving,
    onConfirm,
    saveAndPublish,
    dismissUnsaved,
    refresh,
    withdraw,
    withdrawing,
    withdrawError,
  } = useDeployment(projectId)

  const [open, setOpen] = useState(false)
  const [showModal, setShowModal] = useState(false)
  const [confirmingWithdraw, setConfirmingWithdraw] = useState(false)
  const [answer, setAnswer] = useState<string | null>(null)
  const headingId = useId()

  // An answer, or the unsaved-work question, must not land behind a closed popover: the
  // citizen pressed something and is owed what happened.
  useEffect(() => {
    if (answer !== null || unsaved !== null) setOpen(true)
  }, [answer, unsaved])

  // A fresh press starts from a clean slate, and closing the popover retires an answer
  // that has already been read.
  const onOpenChange = useCallback(
    (next: boolean): void => {
      setOpen(next)
      if (!next) {
        setAnswer(null)
        setConfirmingWithdraw(false)
      }
    },
    [],
  )

  const state: PublishState | null = deployment?.publishState ?? null
  const presentation = state === null ? null : presentationFor(state)
  const version =
    presentation === null ? null : versionRowData(presentation.version, deployment, approval)
  const busy = saving || withdrawing
  const busyReason = withdrawing ? 'Taking it back…' : saving ? 'Saving and publishing…' : undefined

  const speak = useCallback((outcome: DeployOutcome | null): void => {
    // `null` is the unsaved-work question, which speaks for itself below.
    if (outcome === null) return
    setAnswer(
      outcome.outcome === 'routed_for_review'
        ? // The server's OWN sentence. Both publish surfaces said the same words before
          // there was one of them, and a success rendered in anyone else's words is a
          // success the citizen has to translate.
          outcome.message
        : STARTED_ANSWER,
    )
  }, [])

  const pressAction = useCallback((): void => {
    if (busy) return
    setAnswer(null)
    if (presentation?.action === 'take_it_back') {
      setConfirmingWithdraw(true)
      return
    }
    // Every other action is the same request through the same questionnaire.
    setShowModal(true)
    setOpen(false)
  }, [busy, presentation])

  const doWithdraw = useCallback(async (): Promise<void> => {
    if (withdrawing) return
    await withdraw()
    setConfirmingWithdraw(false)
  }, [withdraw, withdrawing])

  const doSaveAndPublish = useCallback(async (): Promise<void> => {
    if (saving) return
    speak(await saveAndPublish())
  }, [saving, saveAndPublish, speak])

  // ── The read itself failed ────────────────────────────────────────────────────────
  // One honest presentation for all of it: the narrower threw on a value it did not
  // recognise, the network failed, or the server 500'd. Never a blank space where the
  // publish affordance was — this is the only publishing surface the citizen has, and a
  // chip that renders nothing is indistinguishable from a broken page.
  //
  // A SERVER-SIDE STORAGE FAILURE IS NOT ONE OF THESE and must not be treated as one: the
  // server degrades that to `live_drift_unknown` and answers 200, so it arrives here as an
  // ordinary state with its own words and its own action.
  if (loadError !== null) {
    return (
      <Popover open={open} onOpenChange={onOpenChange}>
        <PopoverTrigger asChild>
          <button
            type="button"
            data-testid="publish-chip"
            aria-label="Publish status: unavailable"
            className="inline-flex items-center gap-1 rounded-md border border-bial-border bg-surface-muted px-2 py-0.5 text-xs font-semibold text-neutral transition hover:bg-white"
          >
            Status unavailable
            <ChevronDown size={12} aria-hidden />
          </button>
        </PopoverTrigger>
        <PopoverContent data-testid="publish-popover" aria-labelledby={headingId}>
          <p id={headingId} className="text-xs leading-relaxed text-neutral">
            {loadError}
          </p>
          <button
            type="button"
            data-testid="publish-recheck"
            onClick={() => void refresh()}
            className="mt-3 w-full rounded-lg bg-primary px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-primary-600"
          >
            Check again
          </button>
        </PopoverContent>
      </Popover>
    )
  }

  // ── The first read has not answered yet ───────────────────────────────────────────
  // Holds the space beside the project name so nothing jumps, and claims no state. Not a
  // button: there is nothing yet to explain.
  if (presentation === null) {
    return (
      <span
        data-testid="publish-chip-pending"
        className="inline-flex items-center rounded-md border border-transparent bg-surface-muted px-2 py-0.5 text-xs font-semibold text-neutral"
      >
        Checking…
      </span>
    )
  }

  return (
    <>
      {/* ONE permanently-mounted, initially-empty polite live region. A region injected
          together with its text is frequently not announced at all — the portal already
          states this convention at `LivePreview.tsx`. It sits outside the popover on
          purpose: an answer that arrives must be announced whether or not the popover
          happens to be open. */}
      <span
        data-testid="publish-announce"
        role="status"
        aria-live="polite"
        className="sr-only"
      >
        {answer ?? ''}
      </span>

      <Popover open={open} onOpenChange={onOpenChange}>
        <PopoverTrigger asChild>
          <button
            type="button"
            data-testid="publish-chip"
            // The state is IN the accessible name, so a screen reader user learns it
            // without opening anything — R39's "visible without opening the chip" is not
            // a sighted-only guarantee.
            aria-label={`Publish status: ${presentation.label}`}
            className="inline-flex items-center gap-1 rounded-md border border-bial-border bg-white px-2 py-0.5 text-xs font-semibold text-tertiary transition hover:bg-surface-muted"
          >
            {presentation.label}
            <ChevronDown size={12} aria-hidden />
          </button>
        </PopoverTrigger>

        <PopoverContent data-testid="publish-popover" aria-labelledby={headingId} className="w-80">
          <p id={headingId} className="text-xs leading-relaxed text-neutral">
            {presentation.sentence}
          </p>

          {version && (
            <div data-testid="publish-version" className="mt-3 border-t border-bial-border pt-2.5">
              <p className="text-[10px] font-bold uppercase tracking-wide text-neutral/70">
                {version.heading}
              </p>
              <p className="mt-0.5 text-xs font-semibold text-tertiary">
                {version.stamp ? formatStamp(version.stamp) : '—'}
                {version.sha && (
                  <code
                    data-testid="publish-version-sha"
                    className="ml-1.5 rounded bg-surface-muted px-1 py-0.5 text-[10px] font-normal text-neutral"
                  >
                    {version.sha.slice(0, 7)}
                  </code>
                )}
              </p>
              {version.url && (
                <a
                  data-testid="publish-url"
                  href={version.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-1 inline-flex items-center gap-1 break-all text-xs font-semibold text-primary hover:underline"
                >
                  <ExternalLink size={12} className="flex-shrink-0" aria-hidden />
                  {version.url}
                </a>
              )}
              {version.note && (
                <p
                  data-testid="publish-rejection-note"
                  className="mt-2 border-l-2 border-bial-border pl-2 text-xs italic leading-relaxed text-neutral"
                >
                  {version.note}
                </p>
              )}
            </div>
          )}

          {/* The unsaved-work QUESTION, not a failure: the one refusal that has a second
              answer. It re-sends the answers already declared rather than reopening the
              questionnaire. */}
          {unsaved !== null && (
            <div data-testid="publish-unsaved" className="mt-3 border-t border-bial-border pt-2.5">
              <p className="text-xs leading-relaxed text-neutral">{unsaved}</p>
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  data-testid="publish-save-and-publish"
                  onClick={() => void doSaveAndPublish()}
                  // R64/D1: marked unavailable, never hard-disabled. Disabling a control
                  // that has focus blurs it to `document.body` (KTD-2), which is how a
                  // keyboard user loses their place mid-flight. `doSaveAndPublish` is the
                  // enforcement; this is affordance only.
                  aria-disabled={saving}
                  title={saving ? 'Saving and publishing…' : undefined}
                  className={`flex-1 rounded-lg bg-primary px-3 py-1.5 text-xs font-semibold text-white transition ${
                    saving ? 'cursor-default opacity-40' : 'hover:bg-primary-600'
                  }`}
                >
                  Save and publish
                </button>
                <button
                  type="button"
                  data-testid="publish-unsaved-cancel"
                  onClick={dismissUnsaved}
                  className="rounded-lg border border-bial-border px-3 py-1.5 text-xs font-semibold text-neutral transition hover:bg-surface-muted"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {answer !== null && (
            <p
              data-testid="publish-answer"
              className="mt-3 border-t border-bial-border pt-2.5 text-xs leading-relaxed text-neutral"
            >
              {answer}
            </p>
          )}

          {withdrawError !== null && (
            <p
              data-testid="publish-withdraw-error"
              role="alert"
              className="mt-3 text-xs leading-relaxed text-danger"
            >
              {withdrawError}
            </p>
          )}

          {/* AT MOST ONE ACTION. A state with nothing to do renders NO button — not a
              disabled one. R64's "mark unavailable rather than switch off" governs a
              control that is temporarily away and will come back, which is a different
              thing from a state where nothing can be done. */}
          {presentation.action !== null && unsaved === null && !confirmingWithdraw && (
            <button
              type="button"
              data-testid="publish-action"
              onClick={pressAction}
              aria-disabled={busy}
              title={busyReason}
              className={`mt-3 w-full rounded-lg bg-primary px-3 py-1.5 text-xs font-semibold text-white transition ${
                busy ? 'cursor-default opacity-40' : 'hover:bg-primary-600'
              }`}
            >
              {ACTION_LABEL[presentation.action]}
            </button>
          )}

          {/* Taking a submission back is destructive from the citizen's side — the version
              leaves the queue and an administrator stops looking at it — so it asks once. */}
          {confirmingWithdraw && (
            <div data-testid="publish-withdraw-confirm" className="mt-3">
              <p className="text-xs leading-relaxed text-neutral">
                Take this version back? An administrator will stop reviewing it, and you
                can send it again whenever you are ready.
              </p>
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  data-testid="publish-withdraw-yes"
                  onClick={() => void doWithdraw()}
                  aria-disabled={withdrawing}
                  title={withdrawing ? 'Taking it back…' : undefined}
                  className={`flex-1 rounded-lg bg-primary px-3 py-1.5 text-xs font-semibold text-white transition ${
                    withdrawing ? 'cursor-default opacity-40' : 'hover:bg-primary-600'
                  }`}
                >
                  Take it back
                </button>
                <button
                  type="button"
                  data-testid="publish-withdraw-no"
                  onClick={() => setConfirmingWithdraw(false)}
                  className="rounded-lg border border-bial-border px-3 py-1.5 text-xs font-semibold text-neutral transition hover:bg-surface-muted"
                >
                  Keep it there
                </button>
              </div>
            </div>
          )}
        </PopoverContent>
      </Popover>

      {showModal && (
        <DataClassificationModal
          projectId={projectId}
          // A citizen who presses after a rejection reads WHY before anything else
          // happens — the note belongs in the flow they are actually in, not only on a
          // panel beside it that they may never open.
          rejectionNote={approval?.status === 'rejected' ? approval.rejectionNote : null}
          onConfirm={async (answers) => {
            // Refusals THROW and the modal renders them itself, beside the button, with
            // the answers still on screen. Only the two successes and the unsaved-work
            // question reach this line.
            speak(await onConfirm(answers))
            setShowModal(false)
          }}
          onCancel={() => setShowModal(false)}
        />
      )}
    </>
  )
}
