/**
 * Confirmation for discarding unsaved changes back to the last saved version. Modeled on
 * `ProjectDeleteDialog` — the same Radix `Dialog`, red confirm styling, and busy guard on
 * confirm. The parent owns the discard request: it calls `onConfirm` and closes the dialog
 * itself once the request settles, on success or failure, and shows any error beside the
 * toolbar rather than in here.
 */
import { useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog'
import { formatStamp, isUsableInstant } from '../../utils/publishPresentation'

const BODY_ID = 'discard-changes-body'

export interface DiscardChangesDialogProps {
  /** When the version it goes back to was saved (ISO-8601), or null when unknown. */
  savedAt: string | null
  onClose: () => void
  onConfirm: () => void | Promise<void>
}

export default function DiscardChangesDialog({
  savedAt,
  onClose,
  onConfirm,
}: DiscardChangesDialogProps): React.JSX.Element {
  const [busy, setBusy] = useState(false)

  const body = isUsableInstant(savedAt)
    ? `Your app goes back to the version you saved on ${formatStamp(savedAt)}. Everything changed since then is removed.`
    : 'Your app goes back to the version you last saved. Everything changed since then is removed.'

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
          <div className="w-9 h-9 rounded-xl bg-red-50 flex items-center justify-center flex-shrink-0">
            <AlertTriangle size={17} className="text-danger" />
          </div>
          <DialogTitle className="text-base font-bold text-tertiary">
            Discard unsaved changes?
          </DialogTitle>
        </div>

        <p id={BODY_ID} className="text-sm text-neutral mt-3 leading-relaxed">
          {body}
        </p>

        <div className="flex gap-3 mt-5">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            data-testid="discard-dialog-cancel"
            className="px-4 border border-bial-border text-tertiary hover:bg-bial-bg font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void confirm()}
            disabled={busy}
            data-testid="discard-dialog-confirm"
            className="flex-1 flex items-center justify-center gap-2 bg-red-600 hover:bg-red-700 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {busy ? <BusyGlyph size={15} /> : null} Discard changes
          </button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
