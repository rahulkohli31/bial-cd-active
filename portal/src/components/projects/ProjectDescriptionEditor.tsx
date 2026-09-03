/**
 * The project's description surface: a read-only view with an Edit button that
 * opens a pop-up editor (claude.ai project-instructions pattern) — author it by
 * hand, or have the model write it from the project's app code.
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
 * every button in the modal are disabled and a "running" note is shown.
 */
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { ChangeEvent, KeyboardEvent } from 'react'
import { Sparkles, Loader2, Pencil, X } from 'lucide-react'
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
  const [editing, setEditing] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const editButtonRef = useRef<HTMLButtonElement>(null)

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

  const openEditor = () => {
    setError(null)
    setEditing(true)
  }

  // Cancel/backdrop/X: discard any unsaved typing and close — never while busy.
  const closeEditor = () => {
    if (busy) return
    setText(description ?? '')
    setError(null)
    setEditing(false)
    editButtonRef.current?.focus()
  }

  useEffect(() => {
    if (editing) textareaRef.current?.focus()
  }, [editing])

  // A busy request disables the textarea AND every button, so the moment they disable
  // the browser blurs whichever one had focus and it falls to `<body>` — outside the
  // trap's own keydown listener, silently disarming both Tab-containment and Escape for
  // the rest of the request. Move focus onto the card itself (tabIndex={-1} below makes
  // it a valid target) so there is always somewhere inside the dialog holding focus.
  useEffect(() => {
    if (editing && busy) cardRef.current?.focus()
  }, [editing, busy])

  // Escape closes like Cancel (closeEditor already no-ops while busy); Tab/Shift+Tab
  // cycles within the modal's focusables. While busy, every textarea/button is disabled
  // and `focusables` is empty — hold the trap on the card itself (rather than bailing)
  // so Tab still can't escape onto the page behind the modal.
  const onKeyDownTrap = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      closeEditor()
      return
    }
    if (e.key !== 'Tab') return
    const focusables = cardRef.current?.querySelectorAll<HTMLElement>(
      'textarea:not([disabled]), button:not([disabled])',
    )
    if (!focusables || focusables.length === 0) {
      e.preventDefault()
      cardRef.current?.focus()
      return
    }
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }

  const handleSave = async (): Promise<void> => {
    if (busy || over) return
    setMode('saving')
    setError(null)
    try {
      const updated = await patchProject(projectId, { description: toPayload(text) })
      setText(updated.description ?? '')
      onProjectUpdate(updated)
      // Save saves AND closes — but only on success; a failure leaves the modal
      // open with the typed text intact so the user can retry.
      setEditing(false)
      editButtonRef.current?.focus()
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
        {/* THE RAIL'S OWN SECTION-LABEL TREATMENT (plan 002, U3). `PreviewOff`, `Main` and
            `NothingBuilt` draw `DESCRIPTION` in exactly the micro-label form `START A CHAT` and
            `APP STATUS` use — 10.5px, 700, .7px tracking, `#9CA3AF` — with a grey Edit beside it.
            It shipped as 14px sentence-case bold with a teal Edit, which read as a heading of a
            different rank from the two sections above it and put the canvas's only primary-action
            colour on a control that opens a text box. */}
        <h2 className="text-[10.5px] font-bold uppercase tracking-[.7px] text-canvas-label">Description</h2>
        <button
          type="button"
          ref={editButtonRef}
          onClick={openEditor}
          className="flex items-center gap-1.5 text-xs font-semibold text-neutral transition hover:text-primary"
        >
          <Pencil size={13} />
          Edit
        </button>
      </div>

      {/* Reads `text` (kept in sync with `description` by the effect above, and updated
          directly on a successful save/generate) rather than the `description` prop, so
          the read view reflects our own last-known-good state without waiting on the
          parent to re-render with a fresh prop. */}
      <p className={`text-sm whitespace-pre-wrap break-words ${text ? 'text-tertiary' : 'text-neutral'}`}>
        {text || PLACEHOLDER}
      </p>

      {editing && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 font-manrope">
          <div className="absolute inset-0 bg-black/40" onClick={closeEditor} />
          <div
            ref={cardRef}
            tabIndex={-1}
            onKeyDown={onKeyDownTrap}
            role="dialog"
            aria-modal="true"
            aria-label="Edit description"
            className="relative bg-white rounded-2xl shadow-2xl w-full max-w-lg p-6"
          >
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-base font-bold text-tertiary">Edit description</h3>
              <button
                type="button"
                onClick={closeEditor}
                disabled={busy}
                aria-label="Close"
                className="p-1.5 text-neutral hover:text-tertiary rounded-lg hover:bg-bial-bg disabled:opacity-50 transition"
              >
                <X size={18} />
              </button>
            </div>

            <div className="flex items-center justify-end mb-1">
              <span className={`text-xs tabular-nums ${counterClass}`} aria-live="polite">
                {text.length} / {MAX_LENGTH}
              </span>
            </div>

            <textarea
              ref={textareaRef}
              aria-label="Project description"
              placeholder={PLACEHOLDER}
              value={text}
              onChange={onChange}
              disabled={busy}
              rows={10}
              className="w-full rounded-xl border border-bial-border bg-white px-3 py-2 text-sm text-tertiary placeholder:text-neutral focus:outline-none focus:ring-2 focus:ring-primary/30 disabled:bg-bial-bg disabled:text-neutral resize-y"
            />

            {/* THE WRITE-SURFACE NOTICE (#147). This field was introduced as CHAT GROUNDING
                — private context for the builder's own assistant — and the marketplace
                republishes it verbatim to the whole org and indexes it for full-text search.
                It can also be model-written from the app's source by Generate above, so the
                text being broadcast is not necessarily anything the author composed or read.
                Not a leak (it is the owner's own field on their own project), but "the
                sentence I typed to orient the assistant" and "my app's public listing copy"
                are two different acts of writing sharing one input, and nothing here said
                so. Stated at the WRITE surface because that is the only place it can change
                what someone types. */}
            <p className="mt-2 text-xs text-neutral">
              Once your app is published, this becomes its listing in the Marketplace —
              visible to everyone at BIAL and searchable by these words.
            </p>

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

            <div className="mt-4 flex items-center gap-2">
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
              <button
                type="button"
                onClick={closeEditor}
                disabled={busy}
                className="ml-auto px-3.5 py-2 text-sm font-semibold border border-bial-border text-tertiary rounded-lg hover:bg-bial-bg transition disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </section>
  )
}
