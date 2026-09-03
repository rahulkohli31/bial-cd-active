/**
 * RENAME A PROJECT — the control that had to move rather than be dropped (plan 002, U2).
 *
 * It lived in the rail's header, as a pencil that swapped the project's `<h1>` for an input. U3
 * replaces that header with the board's three sections — start a chat, app status, description —
 * none of which is a project name, and the name itself is in the toolbar row now.
 *
 * NO BOARD DRAWS A RENAME CONTROL ANYWHERE. It survives on the origin's own rule: do not delete a
 * shipped capability because an older board omits it. What changed is its shape — the row is one
 * 54px line shared by both screens and an inline text field in it would have to grow the row and
 * fight the title's truncation, so the pencil opens this instead.
 *
 * The same portal-and-scrim treatment as `ProjectDescriptionEditor`, which is the other thing on
 * this screen that edits a project field, so the two read as one interface rather than two.
 *
 * IT CARRIES THE 8-WORD CAP (#158 §14), because §14's rule is "both entry points, or neither".
 * The server refuses a 9-word name on PATCH exactly as it does on POST, so a rename without a
 * client-side guard is a round trip whose only purpose is to be refused. The cap has now missed
 * this control twice by relocation — out of `ProjectPage` into the rail under #172, out of the
 * rail into this dialog under #175 — which is the argument for it living beside the input rather
 * than anywhere upstream of it.
 */
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { patchProject } from '../../utils/projectApi'
import type { Project } from '../../utils/projectApi'
import { ApiError } from '../../utils/apiError'
import { countWords, MAX_PROJECT_NAME_WORDS } from '../../utils/words'

// The VARCHAR(120) column width — a paste backstop, not the rule a person is told about.
// Mirrors the create dialog exactly.
const NAME_MAX_CHARS = 120

export interface ProjectRenameDialogProps {
  project: Project
  onProjectUpdate: (project: Project) => void
  onClose: () => void
}

export default function ProjectRenameDialog({ project, onProjectUpdate, onClose }: ProjectRenameDialogProps) {
  const [draft, setDraft] = useState(project.name)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [])

  // Counted by the rule the server shares (`utils/words.ts` <-> `src/core/words.py`), so the
  // number under the field is the number the API will decide on.
  const words = countWords(draft)
  const tooManyWords = words > MAX_PROJECT_NAME_WORDS

  const submit = () => {
    // A SECOND PRESS WHILE THE FIRST IS STILL IN FLIGHT DOES NOTHING, and the check has to be here
    // rather than on the control. The Save button carries `aria-disabled` and never a real
    // `disabled` attribute — a disabled control throws focus to the document body — so the
    // announcement of inertness is all `aria-disabled` gives; the handler is the only thing that
    // can enforce it. Without this, a double-click or an Enter followed by a click on the still
    // focused button fires two `patchProject` calls for the same rename and closes the dialog
    // twice. Same guard, same reason, as `WorkspaceToolbar`'s Save and `StartAppControl`'s press.
    if (busy) return
    const trimmed = draft.trim()
    // Blocked client-side BEFORE any request: the server 400s on name:null and 422s on "". A
    // whitespace-only name never reaches the wire.
    if (trimmed === '') {
      setError('Name cannot be empty.')
      return
    }
    // Belt-and-braces beside the counter: Enter reaches this handler directly, so an
    // over-limit name must not get out from the keyboard path either.
    if (tooManyWords) {
      setError('Keep the title short — about 6 to 8 words.')
      return
    }
    if (trimmed === project.name) {
      onClose()
      return
    }
    setBusy(true)
    void (async () => {
      try {
        const updated = await patchProject(project.id, { name: trimmed })
        onProjectUpdate(updated)
        onClose()
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Could not rename. Try again.')
        setBusy(false)
      }
    })()
  }

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 font-manrope">
      <div className="absolute inset-0 bg-black/40" onClick={busy ? undefined : onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Rename project"
        className="relative w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl"
      >
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-base font-bold text-tertiary">Rename project</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-lg p-1.5 text-neutral transition hover:bg-bial-bg hover:text-tertiary"
          >
            <X size={18} />
          </button>
        </div>

        <input
          ref={inputRef}
          aria-label="Project name"
          value={draft}
          maxLength={NAME_MAX_CHARS}
          onChange={(e) => {
            setDraft(e.target.value)
            setError(null)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit()
            if (e.key === 'Escape') onClose()
          }}
          className="w-full rounded-xl border border-bial-border px-3 py-2 text-sm text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/30"
        />

        <div className="mt-1 flex items-baseline justify-between">
          <span className="text-[11px] text-neutral">Keep it short — about 6 to 8 words.</span>
          <span
            className={`text-[11px] tabular-nums ${
              tooManyWords ? 'font-semibold text-danger' : 'text-neutral'
            }`}
          >
            {words}/{MAX_PROJECT_NAME_WORDS} words
          </span>
        </div>

        {error && (
          <p role="alert" className="mt-2 text-xs font-medium text-danger">
            {error}
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-xl border border-bial-border px-4 py-2 text-sm font-semibold text-neutral transition hover:bg-bial-bg"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={submit}
            // `aria-disabled`, never `disabled` — a disabled control throws focus to the body.
            aria-disabled={busy || tooManyWords}
            className={`rounded-xl bg-primary px-4 py-2 text-sm font-bold text-white transition hover:bg-primary-600 ${
              tooManyWords ? 'cursor-not-allowed opacity-50' : ''
            }`}
          >
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
