/**
 * The application's description — EDITED WHERE IT IS READ.
 *
 * It used to be a read-only paragraph with an Edit button that opened a second dialog on top of
 * the settings dialog, to type into a textarea. Two modals deep, two focus traps, two Escape
 * meanings, for a field the settings dialog was already showing. The textarea is the surface now:
 * type in it, and Save appears because there is something to save.
 *
 * One behaviour is load-bearing, not polish: every failure leaves the field untouched. The text
 * only changes on a *successful* save (to the server's canonical copy) or by the user typing.
 *
 * REQUIRED AND WORD-BOUNDED (#191): a description can no longer be saved blank — the server
 * rejects both an explicit clear and a whitespace-only write (R11), so Save is gated on the same
 * 15–120 word rule the create form enforces. A project written before #191 that has no
 * description at all still opens normally (R14); it simply cannot be SAVED again until the text
 * clears the bar.
 */
import { useEffect, useRef, useState } from 'react'
import type { ChangeEvent } from 'react'
import { patchProject } from '../../utils/projectApi'
import type { Project } from '../../utils/projectApi'
import { ApiError } from '../../utils/apiError'
import { countWords, MIN_PROJECT_DESCRIPTION_WORDS, MAX_PROJECT_DESCRIPTION_WORDS } from '../../utils/words'

const MAX_LENGTH = 2000
/** Mirrors the create form's placeholder (#191 R15 — the same label/placeholder pair applies
 *  everywhere the field appears). */
const TEXTAREA_PLACEHOLDER = 'Who uses it, and what do they do with it?'

interface ProjectDescriptionEditorProps {
  projectId: string
  /** The project's stored description; `null` = none yet. The parent owns this. */
  description: string | null
  /** Lift the server's canonical project back up after a successful save. */
  onProjectUpdate: (project: Project) => void
}

function messageForSaveError(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Could not save. Try again.'
}

export default function ProjectDescriptionEditor({
  projectId,
  description,
  onProjectUpdate,
}: ProjectDescriptionEditorProps) {
  const [text, setText] = useState(description ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Re-sync the textarea only when the PARENT hands a genuinely new stored value (after our own
  // save propagates back up), never on unrelated renders — so in-progress typing is never
  // clobbered.
  const lastSyncedRef = useRef(description)
  useEffect(() => {
    if (description !== lastSyncedRef.current) {
      lastSyncedRef.current = description
      setText(description ?? '')
    }
  }, [description])

  const stored = description ?? ''
  const dirty = text !== stored
  const over = text.length > MAX_LENGTH
  const words = countWords(text)
  const outOfWordBounds = words < MIN_PROJECT_DESCRIPTION_WORDS || words > MAX_PROJECT_DESCRIPTION_WORDS
  const invalid = over || outOfWordBounds

  const onChange = (e: ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value)
    // The last failure described a write that is no longer the one being attempted.
    if (error !== null) setError(null)
  }

  const revert = () => {
    if (saving) return
    setText(stored)
    setError(null)
  }

  const handleSave = async (): Promise<void> => {
    if (saving || invalid || !dirty) return
    setSaving(true)
    setError(null)
    try {
      const updated = await patchProject(projectId, { description: text.trim() })
      setText(updated.description ?? '')
      onProjectUpdate(updated)
    } catch (err) {
      // The field is intentionally left as the user typed it — never cleared.
      setError(messageForSaveError(err))
    } finally {
      setSaving(false)
    }
  }

  const counterClass = outOfWordBounds && text.length > 0 ? 'text-danger font-semibold' : 'text-neutral'

  return (
    <section className="font-manrope">
      <textarea
        data-testid="project-description-input"
        value={text}
        onChange={onChange}
        disabled={saving}
        rows={6}
        maxLength={MAX_LENGTH}
        placeholder={TEXTAREA_PLACEHOLDER}
        aria-label="What should this app do?"
        className="w-full resize-y rounded-xl border border-bial-border bg-white px-3.5 py-3 text-sm leading-relaxed text-tertiary outline-none transition placeholder:text-neutral focus:border-primary focus:ring-2 focus:ring-primary/20 disabled:opacity-60"
      />

      <div className="mt-1.5 flex items-center justify-between gap-3">
        <span className={`text-xs ${counterClass}`}>
          {words}/{MAX_PROJECT_DESCRIPTION_WORDS} words
        </span>

        {/* THE CONTROLS APPEAR BECAUSE THERE IS SOMETHING TO DO. An always-present Save on a
            field nobody has touched is a control that does nothing, and a citizen who presses it
            and sees nothing happen learns to distrust the screen. */}
        {dirty && (
          <span className="flex items-center gap-2">
            <button
              type="button"
              onClick={revert}
              disabled={saving}
              data-testid="project-description-revert"
              className="rounded-lg px-3 py-1.5 text-xs font-semibold text-neutral transition hover:bg-surface-muted hover:text-primary-900 disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={saving || invalid}
              data-testid="project-description-save"
              className="rounded-lg bg-primary px-3.5 py-1.5 text-xs font-semibold text-white transition hover:bg-primary/90 disabled:opacity-50"
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
          </span>
        )}
      </div>

      {error !== null && (
        <p className="mt-2 text-xs font-medium text-danger" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
