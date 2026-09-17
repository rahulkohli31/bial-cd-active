/**
 * The project's description surface: a read-only view with an Edit button that
 * opens a pop-up editor (claude.ai project-instructions pattern), authored by hand.
 *
 * One behaviour is load-bearing, not polish: every failure leaves the field
 * untouched. We never optimistically clear or replace the text; the textarea only
 * changes on a *successful* save (to the server's canonical copy) or by the user
 * typing.
 *
 * REQUIRED AND WORD-BOUNDED (#191): a description can no longer be saved blank — the
 * server rejects both an explicit clear and a whitespace-only write (R11), so Save is
 * gated on the same 15-120 word rule the create form enforces. A project written before
 * #191 that has no description at all still opens and closes normally (R14); it simply
 * cannot be SAVED again until the text clears the bar.
 */
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { ChangeEvent, ClipboardEvent, KeyboardEvent } from 'react'
import { Pencil, X } from 'lucide-react'
import { patchProject } from '../../utils/projectApi'
import type { Project } from '../../utils/projectApi'
import { ApiError } from '../../utils/apiError'
import { countWords, MIN_PROJECT_DESCRIPTION_WORDS, MAX_PROJECT_DESCRIPTION_WORDS } from '../../utils/words'

const MAX_LENGTH = 2000
/** The read-only summary's empty state — distinct from the textarea's own placeholder
 *  below, which is a prompt for what to TYPE, not a "nothing here" indicator. */
const EMPTY_STATE = 'No description yet'
/** Mirrors the create form's placeholder (#191 R15 — the same label/placeholder pair
 *  applies everywhere the field appears). */
const TEXTAREA_PLACEHOLDER = 'Who uses it, and what do they do with it?'

interface ProjectDescriptionEditorProps {
  projectId: string
  /** The project's stored description; `null` = none yet. The parent owns this. */
  description: string | null
  /** Lift the server's canonical project back up after a successful save. */
  onProjectUpdate: (project: Project) => void
}

/** In-flight state; only one operation runs at a time, and it locks the whole surface. */
type Mode = 'idle' | 'saving'

function messageForSaveError(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Could not save. Try again.'
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
  // A paste that would exceed MAX_LENGTH is silently truncated by the textarea's own native
  // `maxLength` — the browser drops the excess before `onChange` ever sees it, so a paste of
  // 5,000 characters and one of exactly 2,000 are indistinguishable from inside the handler.
  // This is computed in `onPaste`, BEFORE that truncation happens, from the clipboard text and
  // the current selection — the one place the real, untruncated length is still visible.
  const [pasteTruncated, setPasteTruncated] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const editButtonRef = useRef<HTMLButtonElement>(null)

  // Re-sync the textarea only when the PARENT hands a genuinely new stored value
  // (after our own save propagates back up), never on unrelated renders — so the
  // user's in-progress typing is never clobbered.
  const lastSyncedRef = useRef(description)
  useEffect(() => {
    if (description !== lastSyncedRef.current) {
      lastSyncedRef.current = description
      setText(description ?? '')
    }
  }, [description])

  const busy = mode !== 'idle'
  const over = text.length > MAX_LENGTH
  const words = countWords(text)
  const outOfWordBounds = words < MIN_PROJECT_DESCRIPTION_WORDS || words > MAX_PROJECT_DESCRIPTION_WORDS
  const invalid = over || outOfWordBounds

  const onChange = (e: ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value)
    setPasteTruncated(false)
  }

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const pasted = e.clipboardData.getData('text')
    const target = e.currentTarget
    const resultingLength =
      target.value.length - (target.selectionEnd - target.selectionStart) + pasted.length
    setPasteTruncated(resultingLength > MAX_LENGTH)
  }

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
    if (busy || invalid) return
    setMode('saving')
    setError(null)
    try {
      const updated = await patchProject(projectId, { description: text.trim() })
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

  const counterClass = outOfWordBounds && text.length > 0 ? 'text-danger font-semibold' : 'text-neutral'

  return (
    <section className="font-manrope">
      <div className="flex items-center justify-between mb-2">
        {/* THE RAIL'S OWN SECTION-LABEL TREATMENT. `PreviewOff`, `Main` and
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
          directly on a successful save) rather than the `description` prop, so the
          read view reflects our own last-known-good state without waiting on the
          parent to re-render with a fresh prop. */}
      <p className={`text-sm whitespace-pre-wrap break-words ${text ? 'text-tertiary' : 'text-neutral'}`}>
        {text || EMPTY_STATE}
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

            {/* THE FIELD'S LABEL (#191 R15) — the same question as the create form asks,
                applied here too rather than left implicit in the dialog's own title. */}
            <span className="text-xs font-semibold text-tertiary">What should this app do?</span>

            <textarea
              ref={textareaRef}
              aria-label="Project description"
              placeholder={TEXTAREA_PLACEHOLDER}
              value={text}
              onChange={onChange}
              onPaste={onPaste}
              disabled={busy}
              maxLength={MAX_LENGTH}
              rows={10}
              className="mt-1.5 w-full rounded-xl border border-bial-border bg-white px-3 py-2 text-sm text-tertiary placeholder:text-neutral focus:outline-none focus:ring-2 focus:ring-primary/30 disabled:bg-bial-bg disabled:text-neutral resize-y"
            />
            <div className="flex items-baseline justify-between mt-1">
              <span className="text-[11px] text-neutral">
                Between {MIN_PROJECT_DESCRIPTION_WORDS} and {MAX_PROJECT_DESCRIPTION_WORDS} words.
              </span>
              <span className={`text-[11px] tabular-nums ${counterClass}`} aria-live="polite">
                {words}/{MAX_PROJECT_DESCRIPTION_WORDS} words
              </span>
            </div>
            {pasteTruncated && (
              <p role="status" className="text-[11px] text-danger mt-1">
                Pasted text was cut to {MAX_LENGTH} characters.
              </p>
            )}

            {/* THE WRITE-SURFACE NOTICE (#147, widened #191 R17). This field was introduced
                as CHAT GROUNDING — private context for the builder's own assistant — and the
                marketplace republishes it verbatim to the whole org, indexes it for search,
                and now uses it to check for a duplicate before a NEW project is created. Not
                a leak (it is the owner's own field on their own project), but "the sentence I
                typed to orient the assistant" and "my app's public listing copy" are two
                different acts of writing sharing one input, and nothing here said so. Stated
                at the WRITE surface because that is the only place it can change what someone
                types. */}
            <p className="mt-2 text-xs text-neutral">
              Once your app is published, this becomes its listing in the Marketplace —
              visible to everyone at BIAL and searchable by these words. It&apos;s also how the
              platform checks whether something similar already exists before a new project is
              created.
            </p>

            {error && (
              <p className="mt-2 text-xs font-medium text-danger" role="alert">
                {error}
              </p>
            )}

            <div className="mt-4 flex items-center gap-2">
              <button
                type="button"
                onClick={() => void handleSave()}
                disabled={busy || invalid}
                className="px-3.5 py-2 text-sm font-semibold bg-primary text-white rounded-lg hover:bg-primary-dark transition disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Save
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
