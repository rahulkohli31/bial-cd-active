/**
 * Create-a-project modal. Two client-side length guards that mirror the server's
 * limits so the user is corrected before a round-trip, not after a 422:
 *   - name is required and capped at 8 WORDS (#158 §14) — the server enforces the same
 *     rule with the same splitting, and 120 chars remains only as a paste backstop,
 *   - description is optional and capped at 2000.
 * The submit button stays disabled while either bound is exceeded, AND the submit
 * handler re-checks, so a programmatic 121-char value can never reach the network.
 *
 * When the server does reject, we surface the message the thrown `ApiError` carries
 * — which `readApiError` already pulled from whichever of the three envelopes the
 * backend chose — never a synthetic "Failed to create project (422)."
 */
import { useState } from 'react'
import { countWords, MAX_PROJECT_NAME_WORDS } from '../../utils/words'
import { X, Loader2 } from 'lucide-react'
import { createProject, type Project } from '../../utils/projectApi'

// The CHARACTER bound is now only a paste backstop at the column width — the limit a
// person is told about is 8 WORDS (#158 §14), counted by the rule the server shares
// (`src/core/words.py` <-> `utils/words.ts`). `maxLength` keeps an unbounded paste out of a
// VARCHAR(120) column; the counter and the disabled button enforce the rule that matters.
const NAME_MAX = 120
const DESCRIPTION_MAX = 2000

export interface ProjectCreateModalProps {
  onClose: () => void
  onCreated: (project: Project) => void
}

export default function ProjectCreateModal({ onClose, onCreated }: ProjectCreateModalProps): React.JSX.Element {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const trimmedName = name.trim()
  const nameTooLong = name.length > NAME_MAX
  const nameWords = countWords(name)
  const tooManyWords = nameWords > MAX_PROJECT_NAME_WORDS
  const descriptionTooLong = description.length > DESCRIPTION_MAX
  const canSubmit =
    trimmedName.length > 0 && !nameTooLong && !tooManyWords && !descriptionTooLong && !busy

  const submit = async (): Promise<void> => {
    // Belt-and-braces: the button is disabled when invalid, but a test (or a paste)
    // can still drive the handler — never let an over-limit name hit the server.
    if (trimmedName.length === 0 || nameTooLong || tooManyWords || descriptionTooLong || busy) return
    setBusy(true)
    setError(null)
    try {
      const project = await createProject({
        name: trimmedName,
        ...(description.trim().length > 0 ? { description } : {}),
      })
      onCreated(project)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 font-manrope">
      {/* NOT shadcn's `bg-black/80`, and not the kit's 12px blur either (#158 §9). A flat
          scrim erases the page; a heavy blur costs you the row you were about to click. The
          panel earns attention from its own shadow and white, so the page behind it only
          needs softening. `-webkit-` stays for Safari: without it this degrades to a flat
          16% scrim, which is acceptable rather than broken. Overlay only — never the list
          behind it, because `backdrop-filter` is GPU work over everything underneath. */}
      <div
          className="absolute inset-0 bg-slate-900/15 backdrop-blur-[3px] [-webkit-backdrop-filter:blur(3px)]"
          onClick={busy ? undefined : onClose}
        />
      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-base font-bold text-tertiary">New project</h3>
            <p className="text-sm text-neutral mt-0.5">A project owns one app, its description, and its chats.</p>
          </div>
          <button
            onClick={onClose}
            disabled={busy}
            aria-label="Close"
            className="p-1.5 text-neutral hover:text-tertiary rounded-lg hover:bg-bial-bg transition disabled:opacity-50"
          >
            <X size={18} />
          </button>
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
        >
          <label className="block mt-5">
            <span className="text-xs font-semibold text-tertiary">Name</span>
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={NAME_MAX}
              placeholder="e.g. VIP Movement Tracker"
              className="mt-1.5 w-full border border-bial-border rounded-xl px-3 py-2.5 text-sm text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary"
            />
            <div className="flex items-baseline justify-between mt-1">
              {/* The expectation stated BEFORE the user trips it, not only after. */}
              <span className="text-[11px] text-neutral">Keep it short — about 6 to 8 words.</span>
              <span
                className={`text-[11px] tabular-nums ${tooManyWords ? 'text-danger font-semibold' : 'text-neutral'}`}
              >
                {nameWords}/{MAX_PROJECT_NAME_WORDS} words
              </span>
            </div>
          </label>

          <label className="block mt-2">
            <span className="text-xs font-semibold text-tertiary">
              Description <span className="font-normal text-neutral">(optional)</span>
            </span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What is this tool for? You can generate this later from the app's code."
              rows={4}
              className="mt-1.5 w-full border border-bial-border rounded-xl px-3 py-2.5 text-sm text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary resize-none"
            />
            <span
              className={`block text-right text-[11px] mt-1 ${descriptionTooLong ? 'text-danger' : 'text-neutral'}`}
            >
              {description.length}/{DESCRIPTION_MAX}
            </span>
          </label>

          {error !== null && (
            <div role="alert" className="mt-3 bg-red-50 border border-red-200 rounded-xl px-3 py-2.5">
              <p className="text-xs text-red-600">{error}</p>
            </div>
          )}

          <div className="flex gap-3 mt-5">
            <button
              type="submit"
              disabled={!canSubmit}
              className="flex-1 flex items-center justify-center gap-2 bg-primary hover:bg-primary/90 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {busy ? <Loader2 size={15} className="animate-spin" /> : null} Create project
            </button>
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="px-4 border border-bial-border text-tertiary hover:bg-bial-bg font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
