/**
 * "Another project is open" — the #83 choice.
 *
 * A user gets one running workspace at a time. Opening a second project needs the first to
 * give up its container, and the container is where unsaved work lives. This used to happen
 * silently inside the incoming request: the other project was torn down, its unsaved work went
 * with it, and nobody was told. `manager.py`'s `finish_turn_sandbox` states the bargain the
 * platform actually keeps — "a user who loses work must have been told, twice" — and both
 * tellings fire on LEAVING the app, which switching projects is not. This dialog is that
 * missing telling.
 *
 * It is a CHOICE, not an error, and the copy says so: no red, no alert glyph, no apology. The
 * constraint is ordinary (one thing open at a time, like an app on a phone) and the remedy is
 * one click. Saving first is offered because the platform can do it on the user's behalf — the
 * work is one call away from durable — but it stays THEIR call, which is the whole point of
 * KTD-5e. Nothing here saves automatically.
 *
 * `dirty === null` is UNKNOWN, not clean: the server reached the workspace and could not ask
 * it. The copy hedges ("may have unsaved changes") rather than promising, because telling
 * someone their work is safe when nobody checked is the one wrong answer available here.
 */
import { useState } from 'react'
import { FolderOpen, Loader2 } from 'lucide-react'
import type { ReclaimBlocked } from '../../utils/buildSessionApi'

interface Props {
  blocked: ReclaimBlocked
  /** Save the other project, then release it. Rejects if the save fails — the dialog stays
   *  open and says so, because a failed save that closed the workspace anyway is the exact
   *  data loss this whole dialog exists to prevent. */
  onSaveAndSwitch: () => Promise<void>
  /** Release without saving. The user was told; this is them accepting the cost. */
  onSwitchAnyway: () => Promise<void>
  onCancel: () => void
}

export default function ReclaimWorkspaceDialog({
  blocked,
  onSaveAndSwitch,
  onSwitchAnyway,
  onCancel,
}: Props): React.ReactElement {
  const [busy, setBusy] = useState<null | 'save' | 'discard'>(null)
  const [error, setError] = useState<string | null>(null)

  // try/finally, so a rejection re-arms the buttons instead of leaving the dialog disarmed and
  // unclosable — the failure mode `ProjectDeleteDialog` currently has.
  const run = async (which: 'save' | 'discard', fn: () => Promise<void>): Promise<void> => {
    setBusy(which)
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'That did not work. Please try again.')
    } finally {
      setBusy(null)
    }
  }

  const unsaved = blocked.dirty === true ? 'has unsaved changes' : 'may have unsaved changes'

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 font-manrope"
      role="dialog"
      aria-modal="true"
      aria-labelledby="reclaim-title"
    >
      <div className="absolute inset-0 bg-black/40" onClick={busy ? undefined : onCancel} />
      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-xl bg-bial-bg flex items-center justify-center flex-shrink-0">
            <FolderOpen size={17} className="text-primary" />
          </div>
          <h3 id="reclaim-title" className="text-base font-bold text-tertiary">
            “{blocked.projectName}” is still open
          </h3>
        </div>

        <p className="text-sm text-neutral mt-3 leading-relaxed">
          You can work on one app at a time. “{blocked.projectName}” {unsaved} — save it before
          switching, and you can pick it up exactly where you left off.
        </p>

        {error ? (
          <p role="alert" className="text-sm text-danger mt-3 leading-relaxed">
            {error}
          </p>
        ) : null}

        <div className="flex flex-col gap-2.5 mt-5">
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => void run('save', onSaveAndSwitch)}
            className="w-full flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {busy === 'save' ? <Loader2 size={15} className="animate-spin" /> : null} Save and
            switch
          </button>
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => void run('discard', onSwitchAnyway)}
            className="w-full flex items-center justify-center gap-2 border border-bial-border text-tertiary hover:bg-bial-bg font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {busy === 'discard' ? <Loader2 size={15} className="animate-spin" /> : null} Switch
            without saving
          </button>
          <button
            type="button"
            disabled={busy !== null}
            onClick={onCancel}
            className="w-full text-neutral hover:text-tertiary font-semibold py-2 rounded-xl transition text-sm disabled:opacity-50"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
