/**
 * Delete-a-project confirmation. Two things it owes the user before it lets them
 * pull the trigger:
 *
 *   1. It names the whole cascade out loud. Deleting a project destroys the project,
 *      its one app, its own PostgreSQL database and files, AND every chat filed under
 *      it — so it counts those chats
 *      (`listProjectConversations`) and says the number. While that count is still
 *      in flight, or if the count call fails, it falls back to copy that still names
 *      the cascade WITHOUT a number ("all of its chats") — it must never flash
 *      "all 0 chats" from a count that simply has not resolved yet.
 *   2. It makes the click deliberate: the confirm button stays disabled until the
 *      user types the project's name back exactly (a trailing space or wrong case
 *      does not count).
 *
 * The dialog does not delete anything itself — the page owns the optimistic removal
 * and the 404-vs-500 reconciliation — it only collects an informed confirmation and
 * calls `onConfirm`.
 */
import { useEffect, useState } from 'react'
import { AlertTriangle, Loader2 } from 'lucide-react'
import { CONVERSATION_LIST_CAP, listProjectConversations } from '../../utils/conversationApi'
import type { Project } from '../../utils/projectApi'

/**
 * `null` count = not resolved yet (loading, or the count call failed) → name the cascade with
 * no number rather than flash a wrong one.
 *
 * A count that lands exactly ON the server's row cap means "at least this many" — the endpoint
 * has no cursor, so there may be more. Quoting the cap as a total would state a falsehood
 * immediately before an irreversible cascade, which is precisely what this dialog exists to
 * prevent. Say "or more".
 */
/**
 * The half of the cascade that has no row count to quote, and the half that is genuinely
 * irreversible. Every project owns its own database from the moment it is created — before
 * it has an app, before it has a single chat — so this sentence belongs on ALL four
 * branches, including the zero-chat one, which is otherwise the quietest copy in the dialog
 * about the most data. Deleting the project drops that database outright: no export, no
 * snapshot, no undo.
 */
const IRREVERSIBLE = 'The database and files behind the app are destroyed permanently. This cannot be undone.'

function cascadeCopy(chatCount: number | null): string {
  if (chatCount === null) return `This deletes the project, its app, and all of its chats. ${IRREVERSIBLE}`
  if (chatCount === 0) return `This deletes the project and its app. ${IRREVERSIBLE}`
  if (chatCount >= CONVERSATION_LIST_CAP) {
    return `This deletes the project, its app, and all ${CONVERSATION_LIST_CAP} or more of its chats. ${IRREVERSIBLE}`
  }
  return `This deletes the project, its app, and all ${chatCount} chat${chatCount === 1 ? '' : 's'}. ${IRREVERSIBLE}`
}

export interface ProjectDeleteDialogProps {
  project: Project
  onClose: () => void
  onConfirm: () => void | Promise<void>
}

export default function ProjectDeleteDialog({
  project,
  onClose,
  onConfirm,
}: ProjectDeleteDialogProps): React.JSX.Element {
  const [chatCount, setChatCount] = useState<number | null>(null)
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    // The number in the cascade sentence. A failure here is non-fatal: leave the
    // count null and fall back to the numberless copy — a broken counter must not
    // block a user from deleting their own project.
    listProjectConversations(project.id)
      .then((chats) => {
        if (live) setChatCount(chats.length)
      })
      .catch(() => {
        /* leave chatCount null → numberless cascade copy */
      })
    return () => {
      live = false
    }
  }, [project.id])

  const confirmArmed = typed === project.name && !busy

  const confirm = async (): Promise<void> => {
    if (!confirmArmed) return
    setBusy(true)
    await onConfirm()
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 font-manrope">
      <div className="absolute inset-0 bg-black/40" onClick={busy ? undefined : onClose} />
      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-xl bg-red-50 flex items-center justify-center flex-shrink-0">
            <AlertTriangle size={17} className="text-danger" />
          </div>
          <h3 className="text-base font-bold text-tertiary">Delete “{project.name}”?</h3>
        </div>

        <p className="text-sm text-neutral mt-3 leading-relaxed">{cascadeCopy(chatCount)}</p>

        <label className="block mt-4">
          <span className="text-xs font-semibold text-tertiary">
            Type <span className="font-bold text-danger">{project.name}</span> to confirm
          </span>
          <input
            autoFocus
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            aria-label="Type the project name to confirm deletion"
            className="mt-1.5 w-full border border-bial-border rounded-xl px-3 py-2.5 text-sm text-tertiary focus:outline-none focus:ring-2 focus:ring-danger/30 focus:border-danger"
          />
        </label>

        <div className="flex gap-3 mt-5">
          <button
            type="button"
            disabled={!confirmArmed}
            onClick={() => void confirm()}
            className="flex-1 flex items-center justify-center gap-2 bg-red-600 hover:bg-red-700 text-white font-semibold py-2.5 rounded-xl transition text-sm disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {busy ? <Loader2 size={15} className="animate-spin" /> : null} Delete project
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
      </div>
    </div>
  )
}
