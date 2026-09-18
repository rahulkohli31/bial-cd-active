/**
 * Confirmation for putting the workspace back to an earlier version.
 *
 * ITS CLAUSES ARE CONDITIONAL, which is the whole design of it. A rollback means different things
 * to different apps — one that nobody has published loses nothing but the files in the workspace,
 * one that BIAL staff are running keeps serving untouched, one that is approved loses that
 * approval on its next publish. Listing all four every time would bury the one that applies, so
 * each is drawn only when it is true of THIS app.
 *
 * IT CLOSES ON CONFIRM, the opposite of `SaveVersionDialog` and the same as the discard
 * confirmation: the wait belongs in the Save shell, where the elapsed counter is, not behind a
 * modal that hides the workspace it is changing. Errors come back through `save.error`.
 */
import { useState } from 'react'
import { History } from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog'
import { versionStamp } from '../../utils/projectDates'

const BODY_ID = 'rollback-version-body'

export interface RollbackVersionDialogProps {
  /** When the version being restored was saved (ISO-8601), or null when it carries no date. */
  savedAt: string | null
  /** What the citizen called it, or null. */
  description: string | null
  /** There is unsaved work in the workspace that this replaces. */
  dirty: boolean
  /** BIAL staff are running a published copy of this app right now. */
  live: boolean
  /** The app is approved, so its next publish needs approval again. */
  approved: boolean
  onClose: () => void
  onConfirm: () => void | Promise<void>
}

export default function RollbackVersionDialog({
  savedAt,
  description,
  dirty,
  live,
  approved,
  onClose,
  onConfirm,
}: RollbackVersionDialogProps): React.JSX.Element {
  const [busy, setBusy] = useState(false)

  const named = description ? `“${description}”` : 'this version'
  const dated = savedAt !== null ? ` saved ${versionStamp(savedAt)}` : ''

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
          <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl bg-primary-50">
            <History size={17} className="text-primary" />
          </div>
          <DialogTitle className="text-base font-bold text-tertiary">
            Go back to this version?
          </DialogTitle>
        </div>

        <p id={BODY_ID} className="mt-3 text-sm leading-relaxed text-neutral">
          Your workspace goes back to {named}
          {dated}.
        </p>

        <ul className="mt-3 flex flex-col gap-1.5 text-[12.5px] leading-relaxed text-neutral">
          {dirty && (
            <li>Your unsaved changes are replaced. You can get back to them from your versions.</li>
          )}
          {live && <li>The published app keeps running. Nothing changes for BIAL staff.</li>}
          {approved && (
            <li>Publishing this version again needs approval, because the code has changed.</li>
          )}
          {/* ALWAYS DRAWN, because it is the sentence that makes the press safe to make. The
              other three are facts about this app; this one is the guarantee. */}
          <li>
            Nothing is deleted. The version you are on now becomes your previous version, so you
            can come straight back to it.
          </li>
        </ul>

        <div className="mt-5 flex gap-3">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            data-testid="rollback-dialog-cancel"
            className="rounded-xl border border-bial-border px-4 py-2.5 text-sm font-semibold text-tertiary transition hover:bg-bial-bg disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void confirm()}
            disabled={busy}
            data-testid="rollback-dialog-confirm"
            className="flex flex-1 items-center justify-center gap-2 rounded-xl bg-primary py-2.5 text-sm font-semibold text-white transition hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? <BusyGlyph size={15} /> : null} Go back to this version
          </button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
