/**
 * Asking an administrator for access to one connector — the `AskAccess` board, as a BODY of the
 * Integrations dialog rather than a dialog of its own.
 *
 * WHY A PANEL AND NOT A SECOND `Dialog`. `IntegrationsDialog` owns one `<Dialog open>` mount and
 * swaps its body. A second dialog would unmount one Radix dialog and mount another on every
 * forward and back click — a double backdrop fade, and `dialog.tsx`'s `useFocusBackstop()` firing
 * twice — on what the boards draw as one continuous panel. `IntegrationsDialog.test.tsx` pins it
 * by asserting the same DOM node survives the transition.
 *
 * WHAT THIS PANEL DOES NOT OWN. Not the API call, not the busy lock, not the reload: the dialog
 * does all three, hands `busy` down and rejects `onSubmit` on failure. This is a form, an error
 * region and the consent copy.
 *
 * THE CONSENT PANEL SHIPS WHOLE, AND IT SHIPS FROM THE WIRE. Its lines are promises about what
 * an approval does and does not give you, and R1 makes copy that states a capability or a
 * consequence binding in substance — they may be shortened, they may not start meaning something
 * else, and summarising them is the exact failure R1 exists to prevent. So this file renders
 * every one of them, in the order the server sent them, and pins none of them: the sentences live
 * on the registry entry in `backend/src/core/connectors.py`, where the administrator's
 * differently-voiced set already lives, and where a test holds both byte-exact against the boards.
 *
 * NOT ONE SENTENCE HERE IS ABOUT A PARTICULAR SYSTEM, AND THAT IS THE POINT (R18). The title, the
 * subtitle and every ticked line all come off `entry`. A second connector is therefore a
 * registry entry plus its board copy — no migration, no route, and nothing to change in here.
 */
import { useState } from 'react'
import { ChevronLeft, Check, Loader2, X } from 'lucide-react'
import type { ConnectorEntry } from '../../utils/connectorApi'
import {
  countWords,
  MAX_DELETE_REASON_CHARS,
  MAX_DELETE_REASON_WORDS,
  MIN_DELETE_REASON_WORDS,
} from '../../utils/words'
import { Textarea } from '../ui/textarea'
import { DialogTitle } from '../ui/dialog'
import { ConnectorGlyph } from './ConnectorRow'

/**
 * The box's heading — the panel's own furniture, true of any connector, and the one string here
 * that is not about the connector being asked for. The lines under it are the server's.
 */
const CONSENT_HEADING = 'WHAT AN APPROVAL GIVES YOU'

/**
 * The board's helper, which is ALSO the sentence the server returns for an empty field. One
 * string, so a person who submits nothing reads the same instruction the form was already giving
 * them rather than a second, differently-worded complaint.
 */
const REMARKS_HELPER =
  'Say what you need the data for. An administrator reads this and answers it, so it is the whole of what they have to go on.'

/**
 * The word rule, stated BEFORE it is tripped rather than only after — the form and the server
 * must agree about what is required, and a 422 must never be the first news of it.
 *
 * THE CONSTANTS KEEP THEIR DELETE-FLAVOURED NAMES on purpose, exactly as the server's do: owner
 * decision D1 is "use the rule already shipped for a deletion reason", and a parallel set of
 * aliases would be two names for one number — the drift `words.ts` exists to prevent.
 */
const RULE_ID = 'connector-remarks-rule'
const COUNT_ID = 'connector-remarks-count'
const HELPER_ID = 'connector-remarks-helper'

export interface AskAccessPanelProps {
  entry: ConnectorEntry
  /**
   * The request is in flight. `aria-disabled` rather than `disabled` on the submit, per
   * `dialog.tsx`'s docblock: a disabled control throws focus to `<body>` mid-request.
   */
  busy: boolean
  /**
   * The back chevron, and `Cancel`. Both return to the connector list rather than closing the
   * dialog: abandoning a step inside Integrations leaves you in Integrations, which is where the
   * citizen was. The header's own X is what closes.
   */
  onBack: () => void
  onClose: () => void
  /**
   * Posts the remarks. REJECTS on failure and the panel renders the server's own message — the
   * duplicate-request 409 says "You have already asked for access to this", which is worth reading
   * and which a generic "something went wrong" would throw away.
   */
  onSubmit: (remarks: string) => Promise<void>
}

export default function AskAccessPanel({
  entry,
  busy,
  onBack,
  onClose,
  onSubmit,
}: AskAccessPanelProps): React.JSX.Element {
  const [remarks, setRemarks] = useState('')
  const [error, setError] = useState<string | null>(null)

  const words = countWords(remarks)
  const remarksValid = words >= MIN_DELETE_REASON_WORDS && words <= MAX_DELETE_REASON_WORDS
  const canSubmit = remarksValid && !busy

  const submit = async (): Promise<void> => {
    // The guard is real, not decorative: `aria-disabled` says so without doing so, so a keyboard
    // Enter or a paste-then-submit still has to clear the same bound the counter is showing.
    if (!canSubmit) return
    setError(null)
    try {
      await onSubmit(remarks)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    }
  }

  return (
    <div data-testid="ask-access-panel">
      <div className="flex items-start gap-2.5 px-6 pt-[22px]">
        <button
          type="button"
          onClick={onBack}
          aria-label="Back to integrations"
          className="flex-shrink-0 pt-0.5 text-neutral transition hover:text-primary-900"
        >
          <ChevronLeft size={16} />
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2.5">
            <ConnectorGlyph size="title" />
            <DialogTitle className="text-base font-extrabold tracking-[-0.2px] text-primary-900">
              Ask for access to {entry.displayName}
            </DialogTitle>
          </div>
          <p
            id="integrations-dialog-subtitle"
            className="mt-[5px] text-xs leading-[1.6] text-neutral"
          >
            {entry.askSubtitle}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="flex-shrink-0 p-0.5 text-neutral transition hover:text-primary-900"
        >
          <X size={17} />
        </button>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault()
          void submit()
        }}
      >
        <div className="px-6 pb-5 pt-[18px]">
          <div className="mb-1.5 flex items-center gap-2">
            <span className="text-[11px] font-bold tracking-[.3px] text-neutral">REMARKS</span>
            <span className="ml-auto inline-flex items-center rounded-full bg-amber-100 px-[7px] py-0.5 text-[9.5px] font-extrabold tracking-[.5px] text-amber-700">
              REQUIRED
            </span>
          </div>
          <Textarea
            autoFocus
            value={remarks}
            onChange={(e) => setRemarks(e.target.value)}
            rows={3}
            // A paste backstop at the column width, not the rule anybody is told about — that is
            // the word count under the box.
            maxLength={MAX_DELETE_REASON_CHARS}
            aria-label={`Why you need access to ${entry.displayName}`}
            aria-describedby={`${HELPER_ID} ${RULE_ID} ${COUNT_ID}`}
            className="resize-y rounded-[10px] border-bial-border px-[13px] py-[11px] text-[12.5px] leading-[1.6] text-primary-900"
          />
          <p id={HELPER_ID} className="mt-1.5 text-[11px] leading-[1.55] text-neutral">
            {REMARKS_HELPER}
          </p>
          <div className="mt-1 flex items-baseline justify-between">
            <span id={RULE_ID} className="text-[11px] text-neutral">
              Between {MIN_DELETE_REASON_WORDS} and {MAX_DELETE_REASON_WORDS} words.
            </span>
            <span
              id={COUNT_ID}
              className={`text-[11px] tabular-nums ${
                remarks.length > 0 && !remarksValid ? 'font-semibold text-danger' : 'text-neutral'
              }`}
            >
              {words}/{MAX_DELETE_REASON_WORDS} words
            </span>
          </div>

          <div className="mt-4 rounded-xl border border-canvas-offeredge bg-canvas-offer px-3.5 py-3">
            <div className="mb-1.5 text-[10.5px] font-extrabold tracking-[.5px] text-primary-dark">
              {CONSENT_HEADING}
            </div>
            {entry.consentLinesRequester.map((line) => (
              <div key={line.lead} className="flex items-start gap-2 py-1">
                <span className="mt-0.5 flex-shrink-0">
                  <Check size={12} strokeWidth={2.4} className="text-primary-dark" aria-hidden />
                </span>
                <p className="m-0 text-[11.5px] leading-[1.55] text-neutral">
                  <b className="font-bold text-primary-900">{line.lead}</b> {line.body}
                </p>
              </div>
            ))}
          </div>

          {error !== null && (
            <div
              role="alert"
              className="mt-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2.5"
            >
              <p className="text-xs text-red-600">{error}</p>
            </div>
          )}
        </div>

        <div className="flex items-center gap-2.5 border-t border-bial-border px-6 pb-[18px] pt-3.5">
          <button
            type="button"
            onClick={onBack}
            className="ml-auto inline-flex items-center rounded-[9px] border border-bial-border bg-white px-4 py-2.5 text-[12.5px] font-semibold text-neutral transition hover:text-primary-900"
          >
            Cancel
          </button>
          <button
            type="submit"
            aria-disabled={!canSubmit}
            className="inline-flex items-center gap-2 rounded-[9px] bg-primary px-[18px] py-2.5 text-[12.5px] font-bold text-white transition hover:bg-primary-600 aria-disabled:opacity-60"
          >
            {busy ? <Loader2 size={13} className="animate-spin" aria-hidden /> : null}
            Ask an administrator
          </button>
        </div>
      </form>
    </div>
  )
}
