/**
 * Create-a-project modal. Two client-side length guards that mirror the server's
 * limits so the user is corrected before a round-trip, not after a 422:
 *   - name is required and capped at 120 chars (the server 422s at 121),
 *   - description is optional and capped at 2000.
 * The submit button stays disabled while either bound is exceeded, AND the submit
 * handler re-checks, so a programmatic 121-char value can never reach the network.
 *
 * When the server does reject, we surface the message the thrown `ApiError` carries
 * — which `readApiError` already pulled from whichever of the three envelopes the
 * backend chose — never a synthetic "Failed to create project (422)."
 */
import { useState } from 'react'
import { X, Loader2 } from 'lucide-react'
import { createProject, type Project } from '../../utils/projectApi'

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
  const descriptionTooLong = description.length > DESCRIPTION_MAX
  const canSubmit = trimmedName.length > 0 && !nameTooLong && !descriptionTooLong && !busy

  const submit = async (): Promise<void> => {
    // Belt-and-braces: the button is disabled when invalid, but a test (or a paste)
    // can still drive the handler — never let an over-limit name hit the server.
    if (trimmedName.length === 0 || nameTooLong || descriptionTooLong || busy) return
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
      <div className="absolute inset-0 bg-black/40" onClick={busy ? undefined : onClose} />
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
              placeholder="e.g. VIP Movement Tracker"
              className="mt-1.5 w-full border border-bial-border rounded-xl px-3 py-2.5 text-sm text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary"
            />
            <span className={`block text-right text-[11px] mt-1 ${nameTooLong ? 'text-danger' : 'text-neutral'}`}>
              {name.length}/{NAME_MAX}
            </span>
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
