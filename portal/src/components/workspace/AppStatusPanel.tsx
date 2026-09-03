/**
 * THE APP STATUS PANEL (plan 002, U4) — the rail section the boards draw, always visible.
 *
 * ═══ WHAT IT REPLACES ═══
 *
 * Two sentences and a hidden popover. The rail showed the workspace's headline ("Your app is
 * saved.") and nothing about publishing at all; everything a citizen could learn about where
 * their app stood — which version is live, when it was approved, what they last saved — lived
 * inside a popover behind a chip, one row at a time, and had to be clicked for.
 *
 * The boards draw it open: a coloured state pill, three provenance rows with dates and short
 * build ids, one sentence, and one action. That is what this renders.
 *
 * ═══ THE PANEL AND THE CHIP CANNOT DISAGREE, AND THIS IS HOW ═══
 *
 * Both read the ONE server-computed `publishState` and both put it through
 * `utils/publishPresentation.ts` — the same words, the same colour, the same action, the same
 * rows. Neither renders the other and neither is a special case of the other; they differ in
 * shape and in lifetime, which is why sharing a component would have been the wrong seam.
 *
 * They hold SEPARATE READS, which is deliberate rather than an oversight. `usePublishState`'s
 * own docblock kept a same-tab `bial:deployment-changed` nudge alive for exactly this case,
 * noting that "the moment anything puts two publish surfaces in one document again it is the
 * difference between them agreeing and them contradicting each other". That moment is now. The
 * cost is one extra read per project screen: the poll runs only while a publish is in flight, so
 * a settled app costs one request per mount and nothing after it.
 *
 * ═══ THE SAVED ROW IS WHY THIS UNIT NEEDED A SERVER FIELD ═══
 *
 * Every other row comes from a column the status read already selects. The citizen's own last
 * save did not reach the browser at all — the server took its one object-store metadata HEAD,
 * computed the drift, and returned the verdict without the head. It returns the head and its
 * timestamp now, from the SAME read, with no container in the request path: the row has to
 * render on a project whose workspace is stopped, which is precisely where `save-state` — which
 * attaches to a container first — has nothing to say.
 */
import { useMemo, useState, type ReactNode } from 'react'
import { ExternalLink } from 'lucide-react'
import DataClassificationModal from '../DataClassificationModal'
import { usePublishState } from '../../hooks/usePublishState'
import { shortSha } from '../../utils/shortSha'
import {
  ACTION_LABEL,
  formatStamp,
  lookFor,
  presentationFor,
  provenanceRows,
  SECONDARY_ACTIONS,
} from '../../utils/publishPresentation'
import type { ProvenanceRow } from '../../utils/publishPresentation'

export interface AppStatusPanelProps {
  projectId: string
  /**
   * The section's own small-caps label, drawn by this component so the state pill can share its row.
   *
   * A NODE RATHER THAN A STRING, and it comes from the rail rather than being written here: the
   * rail owns the treatment every one of its section labels shares, and a second definition of it
   * here is how two of the three end up a half-point apart. What this component owns is the ROW —
   * `PreviewOff`, `NothingBuilt` and `Main` all draw the label and the pill on one 25px band with
   * the pill carried right. The pill was a `float-right` in the block BELOW the heading, and a
   * float cannot rise onto a preceding block's line, so it dropped to its own row and left a stray
   * band of empty rail in every state.
   */
  label: ReactNode
}

/** The section's head: the label, and whatever the state wants carried to its right. */
function SectionHead({ label, children }: { label: ReactNode; children?: ReactNode }) {
  return (
    <div className="mb-2.5 flex min-h-[25px] items-center gap-2.5">
      {label}
      {children}
    </div>
  )
}

/**
 * One provenance row: a fixed-width small-caps label, then the date, then the short build id.
 *
 * "CANNOT TELL" IS A RENDERING, NOT A BLANK. The two halves are independently null — a bundle
 * written before the metadata stamp existed has a last-modified but no head, so the store can
 * say WHEN without saying WHICH — and neither absence may be filled in from the other or from
 * nothing. A row with no date at all says so in words rather than printing an em-dash a citizen
 * has to interpret.
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
          {row.url && (
            <a
              href={row.url}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Open the published app"
              className="ml-1.5 inline-block align-[-1px] text-primary"
            >
              <ExternalLink size={10} aria-hidden />
            </a>
          )}
        </span>
      )}
    </div>
  )
}

export default function AppStatusPanel({ projectId, label }: AppStatusPanelProps) {
  const { deployment, approval, loadError, refresh, onConfirm, saveAndPublish, unsaved, saving, withdraw, withdrawing } =
    usePublishState(projectId)
  const [showModal, setShowModal] = useState(false)

  const state = deployment?.publishState ?? null
  const presentation = useMemo(() => (state === null ? null : presentationFor(state)), [state])
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
        <SectionHead label={label} />
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
        <SectionHead label={label} />
        <p className="text-[11.5px] text-neutral">Checking…</p>
      </div>
    )
  }

  return (
    <div data-testid="app-status-panel" data-publish-state={state}>
      <SectionHead label={label}>
        <span
          data-testid="status-pill"
          className={`ms-auto inline-flex items-center gap-[7px] rounded-full px-[10px] py-1 text-[11px] font-bold whitespace-nowrap ${look.pill}`}
        >
          <span className={`h-1.5 w-1.5 flex-shrink-0 rounded-full ${look.dot}`} aria-hidden />
          {presentation.label}
        </span>
      </SectionHead>

      {rows.length > 0 && (
        <div className="pt-1">
          {rows.map((row) => (
            <Row key={row.key} row={row} />
          ))}
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

      {showModal && (
        <DataClassificationModal
          projectId={projectId}
          // A citizen who presses after a rejection reads WHY before anything else happens —
          // the note belongs in the flow they are in, not only on a panel beside it.
          rejectionNote={approval?.status === 'rejected' ? approval.rejectionNote : null}
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
