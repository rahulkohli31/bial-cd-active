/**
 * Confirmation for taking a live application out of production.
 *
 * WHY A TAKE-DOWN GETS ASKED ABOUT AT ALL. It is the one control on the list that changes what
 * every other person at BIAL can reach — the application stops answering for all of them, at the
 * moment it is pressed. It sat one click away, unguarded, in a menu whose neighbour (Delete) asks
 * for a written reason; the friction was calibrated backwards against the blast radius.
 *
 * AND WHY IT IS THIS DIALOG RATHER THAN THE DELETE ONE. A take-down keeps everything: the version,
 * the data, the chats, the review it already passed. Publishing again puts the same application
 * back. Asking for a typed reason would price a reversible act like an irreversible one and teach
 * people to type past both.
 *
 * Modeled on `DiscardChangesDialog`: same Radix dialog, same busy guard, and the parent owns the
 * request and closes this once it settles either way.
 */
import { useState } from 'react'
import { PowerOff } from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog'

const BODY_ID = 'take-down-body'

export interface TakeDownDialogProps {
  appName: string
  onClose: () => void
  onConfirm: () => void | Promise<void>
}

export default function TakeDownDialog({
  appName,
  onClose,
  onConfirm,
}: TakeDownDialogProps): React.JSX.Element {
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
          <div className="w-9 h-9 rounded-xl bg-status-amber-bg flex items-center justify-center flex-shrink-0">
            <PowerOff size={17} className="text-status-amber-fg" />
          </div>
          <DialogTitle className="text-base font-bold text-tertiary">
            Take “{appName}” out of production?
          </DialogTitle>
        </div>

        <p id={BODY_ID} className="text-sm text-neutral mt-3 leading-relaxed">
          Everyone using it now loses access, straight away. Nothing is deleted — the version, its
          data and its chats all stay, and publishing again puts it back.
        </p>

        <div className="flex gap-3 mt-5">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            data-testid="take-down-cancel"
            className="px-4 border border-bial-border text-tertiary hover:bg-bial-bg font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void confirm()}
            disabled={busy}
            data-testid="take-down-confirm"
            className="flex-1 flex items-center justify-center gap-2 bg-red-600 hover:bg-red-700 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {busy ? <BusyGlyph size={15} /> : null} Take it down
          </button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
