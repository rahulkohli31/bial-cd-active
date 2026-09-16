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
 * nudge reconciles the two reads (one extra poll only while a publish is in flight) — but it
 * reconciles the READ, not the server's per-mount unsaved-work QUESTION, which no read carries.
 * Mounting this behind a tab is what keeps that question fresh: the panel is built when the tab
 * is chosen and torn down when it is left, so a stale declaration cannot sit behind it.
 *
 * The saved row's date/id come from the SAME status read as every other row (the store's
 * metadata HEAD, no container in the path) — it must render even when the workspace is
 * stopped, which is exactly where `save-state` (container-attached) has nothing to say.
 */
import { useEffect, useMemo, useState } from 'react'
import { Check, Copy, ExternalLink } from 'lucide-react'
import DataClassificationModal from '../DataClassificationModal'
import { usePublishState } from '../../hooks/usePublishState'
import { shortSha } from '../../utils/shortSha'
import { ClipboardRefused, copyToClipboard } from '../../utils/clipboard'
import {
  ACTION_LABEL,
  formatStamp,
  lookFor,
  presentationFor,
  provenanceRows,
  SECONDARY_ACTIONS,
} from '../../utils/publishPresentation'
import type { ProvenanceRow } from '../../utils/publishPresentation'
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
    onConfirm,
    saveAndPublish,
    unsaved,
    saving,
    withdraw,
    withdrawing,
    withdrawError,
  } = usePublishState(projectId)
  const [showModal, setShowModal] = useState(false)

  const state = deployment?.publishState ?? null
  const presentation = useMemo(
    () => (state === null ? null : presentationFor(state, deployment?.failureCode ?? null)),
    [state, deployment?.failureCode],
  )
  const look = useMemo(() => (state === null ? null : lookFor(state)), [state])
  const rows = useMemo(
    () => (state === null ? [] : provenanceRows(state, deployment, approval)),
    [state, deployment, approval],
  )

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
          publish attempt, so it acts directly; every other press opens the same declaration the
          chip's does, because the ladder requires a completed one on every attempt. */}
      {presentation.action !== null && (
        <button
          type="button"
          data-testid="status-action"
          aria-disabled={saving || withdrawing}
          onClick={() => {
            if (saving || withdrawing) return
            if (presentation.action === 'take_it_back') {
              void withdraw()
              return
            }
            setShowModal(true)
          }}
          className={`mt-2.5 w-full rounded-[9px] px-3 py-2.5 text-[12.5px] font-bold transition ${
            SECONDARY_ACTIONS.has(presentation.action)
              ? 'border border-bial-border bg-white text-tertiary hover:border-primary hover:text-primary'
              : 'bg-primary text-white hover:bg-primary-600'
          }`}
        >
          {withdrawing ? 'Taking it back…' : ACTION_LABEL[presentation.action]}
        </button>
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

      {/* THE SAME QUESTION THE CHIP ASKS, because the server asks it of both: a workspace ahead
          of its last save has to be answered before a publish can name a version. */}
      {unsaved !== null && (
        <div data-testid="status-unsaved" className="mt-2.5 border-t border-bial-border pt-2.5">
          <p className="text-[11.5px] leading-relaxed text-neutral">{unsaved}</p>
          <button
            type="button"
            data-testid="status-save-and-publish"
            aria-disabled={saving}
            onClick={() => {
              if (saving) return
              void saveAndPublish()
            }}
            className="mt-2 w-full rounded-[9px] bg-primary px-3 py-2 text-[12px] font-bold text-white transition hover:bg-primary-600"
          >
            {saving ? 'Saving and sending…' : 'Save it first, then send'}
          </button>
        </div>
      )}

      {/* THE FOOT, where anything the owner may do to a LIVE application goes. Below the
          state's own action rather than beside it: one of them changes which version is
          serving, and the others do not. */}
      {actions?.({ state, failureCode: deployment?.failureCode ?? null, refresh })}

      {showModal && (
        <DataClassificationModal
          projectId={projectId}
          // A citizen who presses after a rejection reads WHY before anything else happens —
          // the note belongs in the flow they are in, not only on a panel beside it.
          rejectionNote={approval?.status === 'rejected' ? approval.rejectionNote : null}
          // The one state where the approval pins what is saved; the server publishes it
          // whatever the declaration scores.
          alreadyApproved={state === 'approved_ready_to_publish'}
          onConfirm={async (answers) => {
            // Refusals THROW and the modal renders them itself, beside the button, with the
            // answers still on screen. Only the two successes and the unsaved-work question
            // reach this line — and the question is rendered by the block above rather than
            // spoken here, because it is a choice rather than an answer.
            await onConfirm(answers)
            setShowModal(false)
          }}
          onCancel={() => setShowModal(false)}
        />
      )}
    </div>
  )
}
