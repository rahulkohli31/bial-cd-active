/**
 * ONE SHAPE FOR "ARE YOU SURE?", because three of them had already been written by hand and the
 * fourth would have drifted. Radix dialog, a glyph in a tinted square, a title, one paragraph, and
 * Cancel beside a confirm that locks while the answer is in flight.
 *
 * THE PARENT OWNS THE REQUEST. This waits on `onConfirm`, keeps both buttons inert while it is
 * out, and closes on nothing — escape and the overlay are refused mid-flight, and where the answer
 * goes afterwards is the caller's business. That is what keeps the dialog free of any opinion
 * about what failed and where the failure should be shown.
 *
 * `tone` is the CONFIRM BUTTON's weight, not the dialog's: `danger` for something a person cannot
 * simply press again to undo, `primary` for the rest. It is deliberately not derived from the
 * glyph — a warning triangle over a reversible act is how a product teaches people to click past
 * warnings.
 */
import { useState, type ReactNode } from 'react'
import { BusyGlyph } from './Waiting'
import { Dialog, DialogContent, DialogTitle } from './dialog'

const BODY_ID = 'confirm-dialog-body'

export interface ConfirmDialogProps {
  title: ReactNode
  body: ReactNode
  /** The glyph in the tinted square, already sized by the caller. */
  icon: ReactNode
  /** Tailwind classes for that square — the tint belongs with the glyph that sits in it. */
  iconClassName: string
  confirmLabel: string
  tone: 'danger' | 'primary'
  testId: string
  onClose: () => void
  onConfirm: () => void | Promise<void>
}

const CONFIRM_TONE: Record<ConfirmDialogProps['tone'], string> = {
  danger: 'bg-red-600 hover:bg-red-700',
  primary: 'bg-primary hover:bg-primary-900',
}

export default function ConfirmDialog({
  title,
  body,
  icon,
  iconClassName,
  confirmLabel,
  tone,
  testId,
  onClose,
  onConfirm,
}: ConfirmDialogProps): React.JSX.Element {
  const [busy, setBusy] = useState(false)

  const confirm = async (): Promise<void> => {
    if (busy) return
    setBusy(true)
    try {
      await onConfirm()
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next && !busy) onClose()
      }}
    >
      <DialogContent
        hideClose
        overlayClassName="bg-slate-900/15 backdrop-blur-[3px] [-webkit-backdrop-filter:blur(3px)]"
        className="font-manrope bg-white rounded-2xl shadow-2xl w-full max-w-md p-6 gap-0 border-0"
        aria-describedby={BODY_ID}
      >
        <div className="flex items-center gap-2.5">
          <div className={`w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0 ${iconClassName}`}>
            {icon}
          </div>
          <DialogTitle className="text-base font-bold text-tertiary">{title}</DialogTitle>
        </div>

        <p id={BODY_ID} className="text-sm text-neutral mt-3 leading-relaxed">
          {body}
        </p>

        <div className="flex gap-3 mt-5">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            data-testid={`${testId}-cancel`}
            className="px-4 border border-bial-border text-tertiary hover:bg-bial-bg font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void confirm()}
            disabled={busy}
            data-testid={`${testId}-confirm`}
            className={`flex-1 flex items-center justify-center gap-2 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50 disabled:cursor-not-allowed ${CONFIRM_TONE[tone]}`}
          >
            {busy ? <BusyGlyph size={15} /> : null} {confirmLabel}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
