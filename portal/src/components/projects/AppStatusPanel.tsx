/**
 * WHERE AN APPLICATION STANDS AND WHAT MAY BE DONE ABOUT IT: a coloured state pill, its
 * provenance rows, one sentence, one action — nothing behind a popover.
 *
 * THIS IS THE BODY OF SETTINGS › PRODUCTION, and it is the ONE owner of Send for review. A
 * second submit control somewhere else would be a second place to decide whether there is
 * anything to submit, and the action here is conditional by construction: `presentationFor`
 * gives a state no action where there is nothing to send.
 *
 * WHY IT IS NOT THE CHIP: the panel and `PublishStatusChip.tsx` must never disagree, and both
 * read the one server `publishState` through `utils/publishPresentation.ts` — same words,
 * colour, action, rows — but hold SEPARATE reads, deliberately: they differ in shape and
 * lifetime, so sharing a component was the wrong seam. A same-tab `bial:deployment-changed`
 * nudge reconciles the two reads (one extra poll only while a publish is in flight).
 *
 * The saved row's date/id come from the SAME status read as every other row (the store's
 * metadata HEAD, no container in the path) — it must render even when the workspace is
 * stopped, which is exactly where `save-state` (container-attached) has nothing to say.
 */
import { useEffect, useMemo, useState } from 'react'
import { Check, Copy, ExternalLink } from 'lucide-react'
import PublishDialog from '../PublishDialog'
import { usePublishState } from '../../hooks/usePublishState'
import { shortSha } from '../../utils/shortSha'
import { ClipboardRefused, copyToClipboard } from '../../utils/clipboard'
import {
  ACTION_LABEL,
  answerFor,
  busyLabel,
  formatStamp,
  lookFor,
  presentationFor,
  provenanceRows,
  SECONDARY_ACTIONS,
} from '../../utils/publishPresentation'
import type { ProvenanceRow, PublishAnswer } from '../../utils/publishPresentation'
import type { PublishState } from '../../utils/deployApi'

export interface AppStatusPanelProps {
  projectId: string
  /**
   * Whatever else an owner may do to the application whose status this is — rendered at the
   * foot, and given the two facts it needs to decide: the state, and a way to ask again.
   *
   * IT IS A CALLBACK RATHER THAN A NODE so the tab below does not need a deployment read of its
   * own. Two components in one panel each polling the same endpoint is how a screen comes to
   * show two answers to one question, and it was exactly the arrangement here before this.
   */
  actions?: (ctx: {
    state: PublishState
    /** The ending of the LAST attempt, which is not always a fact about production — a
     *  restart that failed leaves one on a row newer than the container still serving. */
    failureCode: string | null
    /**
     * Whether a publish is ON RECORD and not taken down — a succeeded attempt with an address,
     * carrying no takedown stamp. It is not the same question as `state`: the lifecycle arms
     * outrank the deployment row when a state is NAMED, so an application serving one version
     * while a newer one waits for review is named `in_review` and this is still true.
     */
    hasServingRow: boolean
    refresh: () => Promise<void>
  }) => React.ReactNode
}

/** The panel's own small-caps label. It was the rail's to draw; there is no rail. */
function StatusLabel() {
  return <h3 className="text-[10.5px] font-bold tracking-[.7px] text-neutral">STATUS</h3>
}

/**
 * THE LIVE APP'S ADDRESS — open it, or take a copy of it.
 *
 * The panel already linked the address correctly; sharing it meant opening the tab and
 * copying the browser's own address bar — the complaint this control answers. It sits beside
 * the link because it copies THAT address and nothing else.
 *
 * IT IS RENDERED WHEREVER THE ROW OFFERS A URL, and that is the whole of its presence
 * rule. `provenanceRows` already decides where an address is worth pointing at — a
 * taken-offline app's address would 404, so that row carries `url: null` — so a copy
 * control gated on the same value cannot appear on an app whose address does not work,
 * and there is no second place deciding it.
 *
 * A REFUSAL IS SPOKEN, WITH THE ADDRESS BESIDE IT. `navigator.clipboard` is undefined on
 * an insecure origin and rejects on a denied permission, and both are invisible without
 * this: the press does nothing, the citizen pastes whatever was on their clipboard
 * before, and the platform said not one word about it. The address is printed in full so
 * the remedy is in the same place as the failure rather than "try the address bar".
 *
 * NOTHING IS ADDED TO THE PUBLISHED PAGE ITSELF — no provenance strip, no branding,
 * no builder attribution. The link opens the citizen's app as it is.
 */
function LiveAddress({ url }: { url: string }) {
  const [outcome, setOutcome] = useState<'idle' | 'copied' | 'refused'>('idle')

  // The confirmation is temporary on purpose: a control stuck reading "copied" is
  // describing the last press for ever, and the next press has nothing to say.
  useEffect(() => {
    if (outcome !== 'copied') return
    const timer = window.setTimeout(() => setOutcome('idle'), 2500)
    return () => window.clearTimeout(timer)
  }, [outcome])

  return (
    <>
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        aria-label="Open the published app"
        className="ml-1.5 inline-block align-[-1px] text-primary"
      >
        <ExternalLink size={10} aria-hidden />
      </a>
      <button
        type="button"
        data-testid="status-copy-link"
        aria-label={outcome === 'copied' ? 'Link copied' : 'Copy the link to this app'}
        onClick={() => {
          void copyToClipboard(url).then(
            () => setOutcome('copied'),
            (error: unknown) => {
              // `copyToClipboard` folds every cause it knows — no clipboard on this
              // origin, a denied permission, a failed write — into one typed error, so
              // this handles that one case rather than swallowing whatever arrives.
              if (!(error instanceof ClipboardRefused)) throw error
              setOutcome('refused')
            },
          )
        }}
        className="ml-1.5 inline-block align-[-1px] text-primary transition hover:text-primary-600"
      >
        {outcome === 'copied' ? (
          <Check size={10} aria-hidden />
        ) : (
          <Copy size={10} aria-hidden />
        )}
      </button>
      {outcome === 'refused' && (
        <span
          role="alert"
          data-testid="status-copy-refused"
          className="mt-1 block text-[10.5px] leading-relaxed text-danger"
        >
          We could not copy it. Here is the address to copy by hand:{' '}
          <span className="font-mono break-all text-canvas-sha">{url}</span>
        </span>
      )}
    </>
  )
}

/**
 * THE REVIEWER'S OWN WORDS, on the rail, without opening anything.
 *
 * The note reached the browser on every status read and rendered in exactly one place:
 * inside the declaration dialog, which a citizen opens when they believe they are
 * FINISHED. So the sentence telling them what to change arrived one press after the moment
 * they needed it, and only if they pressed at all.
 *
 * BOUNDED AND SCROLLABLE, WITH THE WHOLE NOTE IN THE DOM. The note is free text capped at
 * 1,000 characters and this rail is 360–640px wide, so an unbounded block would push the
 * state's action below the fold — the button the note is telling them to press again.
 * Clipping it in JavaScript would fix the geometry by hiding the reviewer's words, so the
 * text is complete and it is the BOX that is bounded: assistive technology reads all of
 * it, and `tabIndex` makes the overflow reachable by keyboard rather than by mouse wheel
 * alone.
 */
function NoteRow({ row }: { row: ProvenanceRow }) {
  return (
    <div data-testid={`status-row-${row.key}`} className="py-[3px]">
      <span className="block text-[9.5px] font-extrabold tracking-[.4px] text-canvas-label">
        {row.label}
      </span>
      <p
        data-testid={`status-row-${row.key}-note`}
        tabIndex={0}
        className="mt-1 max-h-[7.5rem] overflow-y-auto rounded-[7px] border border-bial-border bg-bial-bg/60 px-2 py-1.5 text-[11.5px] leading-relaxed whitespace-pre-wrap text-primary-900"
      >
        {row.note}
      </p>
    </div>
  )
}

/** The section's head: the label, and whatever the state wants carried to its right. */
function SectionHead({ children }: { children?: React.ReactNode }) {
  return (
    <div className="mb-2.5 flex min-h-[25px] items-center gap-2.5">
      <StatusLabel />
      {children}
    </div>
  )
}

/**
 * One provenance row: fixed-width small-caps label, then date, then short build id.
 *
 * "CANNOT TELL" IS A RENDERING, NOT A BLANK. The two halves are independently null — a bundle
 * written before the metadata stamp existed has a last-modified but no head, so the store can
 * say WHEN without WHICH, and neither absence is filled from the other. No date at all says so
 * in words, never an em-dash a citizen has to interpret.
 */
function Row({ row }: { row: ProvenanceRow }) {
  const tone = row.tone === 'drift' ? 'text-status-amber-fg font-bold' : 'text-primary-900 font-semibold'
  return (
    <div data-testid={`status-row-${row.key}`} className="flex items-baseline gap-2 py-[3px]">
      <span className="w-[86px] flex-shrink-0 text-[9.5px] font-extrabold tracking-[.4px] text-canvas-label">
        {row.label}
      </span>
      {row.stamp === null && row.sha === null ? (
        <span data-testid={`status-row-${row.key}-unknown`} className="text-[11.5px] font-medium text-neutral">
          We could not tell
        </span>
      ) : (
        <span className={`text-[11.5px] ${tone}`}>
          {row.stamp === null ? 'Date unknown' : formatStamp(row.stamp)}
          {row.sha ? (
            <code className="ml-1.5 font-mono text-[10px] font-normal text-canvas-sha">{shortSha(row.sha)}</code>
          ) : (
            <span className="ml-1.5 text-[10px] font-normal text-canvas-sha">version unknown</span>
          )}
          {row.url && <LiveAddress url={row.url} />}
        </span>
      )}
    </div>
  )
}

export default function AppStatusPanel({ projectId, actions }: AppStatusPanelProps) {
  const {
    deployment,
    approval,
    loadError,
    refresh,
    publish,
    publishPhase,
    publishError,
    onConfirm,
    withdraw,
    withdrawing,
    withdrawError,
  } = usePublishState(projectId)
  const [showModal, setShowModal] = useState(false)
  const [answer, setAnswer] = useState<PublishAnswer | null>(null)

  const state = deployment?.publishState ?? null
  // Said only while the app is still where the answer left it: a poll that finds it live, or
  // failed, or taken down, retires the line without anyone pressing anything.
  const said = answer !== null && answer.heldWhile === state ? answer.text : null
  const approvedRetryCommit = deployment?.approvedRetryCommit ?? null
  const presentation = useMemo(
    () => (state === null ? null : presentationFor(state, approvedRetryCommit)),
    [state, approvedRetryCommit],
  )
  const look = useMemo(() => (state === null ? null : lookFor(state)), [state])
  const rows = useMemo(
    () => (state === null ? [] : provenanceRows(state, deployment, approval)),
    [state, deployment, approval],
  )
  const busy = publishPhase !== null || withdrawing

  // THE READ ITSELF FAILED. Never a blank section where the status was — a panel that renders
  // nothing is indistinguishable from a broken page, and this is the surface a citizen goes to
  // in order to find out whether anything is wrong.
  if (loadError !== null) {
    return (
      <div data-testid="app-status-panel" data-publish-state="unavailable">
        <SectionHead />
        <p className="text-[11.5px] leading-relaxed text-neutral">{loadError}</p>
        <button
          type="button"
          data-testid="status-recheck"
          onClick={() => void refresh()}
          className="mt-2.5 w-full rounded-[9px] bg-primary px-3 py-2.5 text-[12.5px] font-bold text-white transition hover:bg-primary-600"
        >
          Check again
        </button>
      </div>
    )
  }

  // Holds the section's shape while the first read is out, and claims no state.
  if (presentation === null || look === null || state === null) {
    return (
      <div data-testid="app-status-panel" data-publish-state="pending">
        <SectionHead />
        <p className="text-[11.5px] text-neutral">Checking…</p>
      </div>
    )
  }

  return (
    <div data-testid="app-status-panel" data-publish-state={state}>
      <SectionHead>
        <span
          data-testid="status-pill"
          className={`ms-auto inline-flex items-center gap-[7px] rounded-full px-[10px] py-1 text-[11px] font-bold whitespace-nowrap ${look.pill}`}
        >
          <span className={`h-1.5 w-1.5 flex-shrink-0 rounded-full ${look.dot}`} aria-hidden />
          {presentation.label}
        </span>
      </SectionHead>

      {/* A ROW IS EITHER PROSE OR PROVENANCE, and the branch is here rather than inside
          `Row` so the two shapes stay two components: a dated line with a fixed-width
          label, and a bounded block of somebody's words. `provenanceRows` decides which
          states get which, and puts the note FIRST so the state's action below stays in
          view however long it is. */}
      {rows.length > 0 && (
        <div className="pt-1">
          {rows.map((row) =>
            typeof row.note === 'string' ? (
              <NoteRow key={row.key} row={row} />
            ) : (
              <Row key={row.key} row={row} />
            ),
          )}
        </div>
      )}

      <p className="mt-2.5 text-[11.5px] leading-relaxed text-neutral">{presentation.sentence}</p>

      {/* WHERE NOTHING CAN BE DONE THERE IS NO BUTTON, rather than one that fails when pressed —
          the board says so in as many words. `take_it_back` is the one action that is not a
          publish attempt, so it acts directly; every other press is `publish`, which either
          sends the approved copy itself or saves and hands back the declaration to open. */}
      {presentation.action !== null && (
        <button
          type="button"
          data-testid="status-action"
          aria-disabled={busy}
          onClick={() => {
            if (busy) return
            setAnswer(null)
            if (presentation.action === 'take_it_back') {
              void withdraw()
              return
            }
            void publish().then((next) => {
              if (next === 'review') setShowModal(true)
              else if (next !== null) setAnswer(answerFor(next))
            })
          }}
          className={`mt-2.5 w-full rounded-[9px] px-3 py-2.5 text-[12.5px] font-bold transition ${
            SECONDARY_ACTIONS.has(presentation.action)
              ? 'border border-bial-border bg-white text-tertiary hover:border-primary hover:text-primary'
              : 'bg-primary text-white hover:bg-primary-600'
          }`}
        >
          {busyLabel(withdrawing, publishPhase) ?? ACTION_LABEL[presentation.action]}
        </button>
      )}

      {/* Outside the button's condition and mounted before it has anything to say: a sent copy
          usually leaves a state with no button, and text injected together with its region is
          often not announced. */}
      <p
        data-testid="status-answer"
        role="status"
        aria-live="polite"
        className={`text-[11.5px] leading-relaxed text-neutral ${said === null ? '' : 'mt-2.5'}`}
      >
        {said}
      </p>

      {/* A SAVE OR A SEND THAT FAILED BEFORE ANY DIALOG, in the server's own words — without
          it the press would look like it did nothing. */}
      {publishError !== null && (
        <p
          data-testid="status-publish-error"
          role="alert"
          className="mt-2.5 text-[11.5px] leading-relaxed text-danger"
        >
          {publishError}
        </p>
      )}

      {/* A REFUSED WITHDRAWAL HAS TO BE SPOKEN, because nothing else on this panel changes when
          one happens. `withdraw` swallows its failure into this message and does NOT re-read — so
          without this the pill, the rows and the button all stay exactly as they were and the
          press looks like it did nothing, which is how a citizen ends up pressing it repeatedly.
          The ordinary case is an administrator reaching the submission first. */}
      {withdrawError !== null && (
        <p
          data-testid="status-withdraw-error"
          role="alert"
          className="mt-2.5 text-[11.5px] leading-relaxed text-danger"
        >
          {withdrawError}
        </p>
      )}

      {/* THE FOOT, where anything the owner may do to a LIVE application goes. Below the
          state's own action rather than beside it: one of them changes which version is
          serving, and the others do not. */}
      {actions?.({
        state,
        failureCode: deployment?.failureCode ?? null,
        hasServingRow:
          deployment?.status === 'succeeded' &&
          deployment.url !== null &&
          deployment.unpublishedAt === null,
        refresh,
      })}

      {showModal && (
        <PublishDialog
          projectId={projectId}
          deployment={deployment}
          // A citizen who presses after a rejection reads WHY before anything else happens —
          // the note belongs in the flow they are in, not only on a panel beside it.
          rejectionNote={approval?.status === 'rejected' ? approval.rejectionNote : null}
          onConfirm={async (commitSha, send) => {
            // Refusals THROW and the dialog renders them itself, beside the button, with the
            // answers still on screen. Only the two successes reach this line.
            setAnswer(answerFor(await onConfirm(commitSha, send)))
            setShowModal(false)
          }}
          onCancel={() => setShowModal(false)}
        />
      )}
    </div>
  )
}
