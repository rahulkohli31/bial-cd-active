/**
 * Naming the version a Save is about to create, and saying what that Save costs.
 *
 * WHY IT ASKS AT ALL. A version is reached later from a list of two, and a date alone does not
 * tell a citizen which of two Tuesdays holds the working approval flow. The words are theirs,
 * optional, and can never block the save — an empty line saves exactly as before.
 *
 * IT CLOSES ONLY ON SUCCESS, which is the opposite of the discard confirmation and is deliberate:
 * a failed save that closed this dialog would take the typed line with it, and the citizen would
 * have to remember what they wrote before they could try again.
 */
import { useState } from 'react'
import { Save } from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog'
import { versionStamp } from '../../utils/projectDates'
import type { AppVersion } from '../../utils/buildSessionApi'

const BODY_ID = 'save-version-body'

/** Matches `app_versions.description`, which matches the application name's ceiling. */
export const DESCRIPTION_MAX_CHARS = 120

export interface SaveVersionDialogProps {
  /**
   * The version this Save pushes off the list, or null when nothing drops. NULL IS SILENT — not
   * "nothing will be dropped", which is a warning about something that is not going to happen on
   * the common path, and a dialog that warns about nothing trains people to dismiss it.
   */
  evicting: AppVersion | null
  onClose: () => void
  onConfirm: (description: string) => void | Promise<void>
}

export default function SaveVersionDialog({
  evicting,
  onClose,
  onConfirm,
}: SaveVersionDialogProps): React.JSX.Element {
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const confirm = async (): Promise<void> => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await onConfirm(description.trim())
      // INSIDE THE TRY, AFTER THE AWAIT. See the module docstring: a close in `finally` would
      // discard the typed line on exactly the path where it is hardest to retype.
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save your work. Try again.')
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
            <Save size={17} className="text-primary" />
          </div>
          <DialogTitle className="text-base font-bold text-tertiary">Save this version</DialogTitle>
        </div>

        <p id={BODY_ID} className="mt-3 text-sm leading-relaxed text-neutral">
          Give this version a short description so you can recognise it later. You can leave it
          blank.
        </p>

        <label className="mt-4 block">
          <span className="text-xs font-semibold text-tertiary">Description</span>
          <input
            value={description}
            autoFocus
            onChange={(e) => {
              setDescription(e.target.value)
              setError(null)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !busy) void confirm()
            }}
            maxLength={DESCRIPTION_MAX_CHARS}
            placeholder="Added the approval step"
            aria-label="Version description"
            data-testid="save-version-description"
            className="mt-1.5 w-full rounded-xl border border-bial-border px-3 py-2.5 text-sm text-tertiary placeholder:text-gray-400 focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/30"
          />
        </label>

        {/* THE LIMIT, NAMED BEFORE IT APPLIES. Two versions are offered; this is the moment the
            third one displaces the oldest, and it is the only moment at which saying so is of any
            use. It is not deleted — it stops being offered — and the sentence says exactly that. */}
        {evicting !== null && (
          <p className="mt-4 rounded-xl bg-surface-muted px-3 py-2.5 text-[12px] leading-relaxed text-neutral">
            Your list keeps the two most recent versions, so{' '}
            <span className="font-semibold text-tertiary">
              {evicting.description ?? 'the version'}
              {evicting.savedAt !== null ? ` (${versionStamp(evicting.savedAt)})` : ''}
            </span>{' '}
            will no longer be listed after this save. Nothing is deleted.
          </p>
        )}

        {error !== null && (
          <p role="alert" className="mt-3 text-[11px] font-semibold text-danger">
            {error}
          </p>
        )}

        <div className="mt-5 flex gap-3">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            data-testid="save-version-cancel"
            className="rounded-xl border border-bial-border px-4 py-2.5 text-sm font-semibold text-tertiary transition hover:bg-bial-bg disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void confirm()}
            disabled={busy}
            data-testid="save-version-confirm"
            className="flex flex-1 items-center justify-center gap-2 rounded-xl bg-primary py-2.5 text-sm font-semibold text-white transition hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? <BusyGlyph size={15} /> : null} Save version
          </button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
