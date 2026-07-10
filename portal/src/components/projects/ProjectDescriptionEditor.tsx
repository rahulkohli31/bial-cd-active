/**
 * The project's description surface: author it by hand, or have the model write
 * it from the project's app code. Two buttons, one textarea, a live length gauge.
 *
 * Two behaviours are load-bearing, not polish:
 *
 *  - **Generate saves first.** `POST …/description:generate` revises the *stored*
 *    description. If the user has typed and not saved, generating would silently
 *    revise a stale copy and throw their edit away. So when the field is dirty we
 *    `patchProject({description})` the typed text FIRST, then generate. (Origin
 *    R19: revise, don't discard.)
 *  - **Every failure leaves the field untouched.** We never optimistically clear
 *    or replace the text; the textarea only changes on a *successful* save/generate
 *    (to the server's canonical copy) or by the user typing.
 *
 * The model call is billed and takes seconds, so for its duration the textarea and
 * both buttons are disabled and a "running" note is shown.
 */
import { useEffect, useRef, useState } from 'react'
import type { ChangeEvent } from 'react'
import { Sparkles, Loader2 } from 'lucide-react'
import { patchProject, generateDescription } from '../../utils/projectApi'
import type { Project } from '../../utils/projectApi'
import { ApiError } from '../../utils/apiError'

const MAX_LENGTH = 2000
const PLACEHOLDER = 'No description yet'

interface ProjectDescriptionEditorProps {
  projectId: string
  /** The project's stored description; `null` = none yet. The parent owns this. */
  description: string | null
  /** Lift the server's canonical project back up after a successful save/generate. */
  onProjectUpdate: (project: Project) => void
}

/** In-flight state; only one operation runs at a time, and it locks the whole surface. */
type Mode = 'idle' | 'saving' | 'generating'

/**
 * Map a `generateDescription` failure to user copy. Branches on HTTP status, not
 * an enum, so there is no exhaustiveness to assert — an unknown status falls back
 * to the server's own message (or the generic retry line).
 */
function messageForGenerateError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.status) {
      case 409:
        return "Build the app first — there's no code to describe yet."
      case 429:
        // The daily-limit sentence the ApiError already carries from the backend.
        return err.message
      case 503:
        return 'Description generation is unavailable.'
      case 500:
        return 'Generation failed. Try again.'
      default:
        return err.message || 'Generation failed. Try again.'
    }
  }
  return 'Generation failed. Try again.'
}

function messageForSaveError(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Could not save. Try again.'
}

/** Whitespace-only clears the field (server maps to null); anything else is stored verbatim. */
function toPayload(text: string): string | null {
  return text.trim() === '' ? null : text
}

export default function ProjectDescriptionEditor({
  projectId,
  description,
  onProjectUpdate,
}: ProjectDescriptionEditorProps) {
  const [text, setText] = useState(description ?? '')
  const [mode, setMode] = useState<Mode>('idle')
  const [error, setError] = useState<string | null>(null)

  // Re-sync the textarea only when the PARENT hands a genuinely new stored value
  // (after our own save/generate propagates back up), never on unrelated renders —
  // so the user's in-progress typing is never clobbered.
  const lastSyncedRef = useRef(description)
  useEffect(() => {
    if (description !== lastSyncedRef.current) {
      lastSyncedRef.current = description
      setText(description ?? '')
    }
  }, [description])

  const busy = mode !== 'idle'
  const over = text.length > MAX_LENGTH
  const dirty = text !== (description ?? '')

  const onChange = (e: ChangeEvent<HTMLTextAreaElement>) => setText(e.target.value)

  const handleSave = async (): Promise<void> => {
    if (busy || over) return
    setMode('saving')
    setError(null)
    try {
      const updated = await patchProject(projectId, { description: toPayload(text) })
      setText(updated.description ?? '')
      onProjectUpdate(updated)
    } catch (err) {
      // The field is intentionally left as the user typed it — never cleared.
      setError(messageForSaveError(err))
    } finally {
      setMode('idle')
    }
  }

  const handleGenerate = async (): Promise<void> => {
    if (busy) return
    setMode('generating')
    setError(null)
    try {
      // Generate saves first: persist the typed text so generation revises what the
      // user can see, not a stale stored copy (origin R19).
      //
      // Lift the SAVED project immediately, before generating. That patch already landed on the
      // server; if generation then fails, the parent must not go on believing the description is
      // the one it held before the user typed.
      if (dirty) onProjectUpdate(await patchProject(projectId, { description: toPayload(text) }))
      const generated = await generateDescription(projectId)
      setText(generated.description ?? '')
      onProjectUpdate(generated)
    } catch (err) {
      // Every failure path leaves the textarea exactly as it was.
      setError(messageForGenerateError(err))
    } finally {
      setMode('idle')
    }
  }

  const counterClass = over ? 'text-danger' : 'text-neutral'

  return (
    <section className="font-manrope">
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-sm font-bold text-tertiary">Description</h2>
        <span className={`text-xs tabular-nums ${counterClass}`} aria-live="polite">
          {text.length} / {MAX_LENGTH}
        </span>
      </div>

      <textarea
        aria-label="Project description"
        placeholder={PLACEHOLDER}
        value={text}
        onChange={onChange}
        disabled={busy}
        rows={5}
        className="w-full rounded-xl border border-bial-border bg-white px-3 py-2 text-sm text-tertiary placeholder:text-neutral focus:outline-none focus:ring-2 focus:ring-primary/30 disabled:bg-bial-bg disabled:text-neutral resize-y"
      />

      {mode === 'generating' && (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-neutral" role="status">
          <Loader2 size={13} className="animate-spin text-primary" />
          Generating description… this runs a model call and can take a few seconds.
        </p>
      )}

      {error && (
        <p className="mt-2 text-xs font-medium text-danger" role="alert">
          {error}
        </p>
      )}

      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          onClick={() => void handleSave()}
          disabled={busy || over}
          className="px-3.5 py-2 text-sm font-semibold bg-primary text-white rounded-lg hover:bg-primary-dark transition disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Save
        </button>
        <button
          type="button"
          onClick={() => void handleGenerate()}
          disabled={busy}
          className="flex items-center gap-1.5 px-3.5 py-2 text-sm font-semibold text-primary border border-primary/30 rounded-lg hover:bg-primary/5 transition disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <Sparkles size={15} />
          Generate
        </button>
      </div>
    </section>
  )
}
