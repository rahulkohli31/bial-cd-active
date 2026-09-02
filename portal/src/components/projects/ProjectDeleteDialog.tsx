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
 *   2. It asks WHY, in 5-50 words, and the confirm button stays disabled until that
 *      reason is inside the bounds (#158 §13.1/§13.2).
 *
 *      IT NAMES WHO, BUT DOES NOT ASK. The deletion is recorded against an account, and the
 *      dialog says which one — but the server stamps that from the session and ignores
 *      anything sent for it. The field briefly WAS a required input, and that was wrong: a
 *      name this dialog could set is a name that can disagree with the account that acted,
 *      and it is the field an administrator reads to answer precisely that question. Shown,
 *      never collected.
 *
 *      THE TYPE-THE-NAME GATE IS GONE. Retyping a name proves you can read, not that you
 *      meant it — and it taught people to copy-paste past the warning they were meant to be
 *      reading. The reason is a better gate for the same purpose AND it is still useful a
 *      month later: it is kept on a `deleted_projects` tombstone an administrator can read.
 *      The helper text says so, because someone writing a private-feeling note deserves to
 *      know who sees it.
 *
 *      The count is validated HERE and again on the server, with the same splitting rule
 *      (`utils/words.ts` <-> `src/core/words.py`). The client keeps the person inside the
 *      limit; the server refuses independently.
 *
 * The dialog does not delete anything itself — the page owns the optimistic removal
 * and the 404-vs-500 reconciliation — it only collects an informed confirmation and
 * calls `onConfirm`.
 */
import { useEffect, useState } from 'react'
import { AlertTriangle, Loader2 } from 'lucide-react'
import { CONVERSATION_LIST_CAP, listProjectConversations } from '../../utils/conversationApi'
import type { Project } from '../../utils/projectApi'
import { getStoredUser } from '../../utils/auth'
import { Textarea } from '../ui/textarea'
import {
  countWords,
  MAX_DELETE_REASON_WORDS,
  MIN_DELETE_REASON_WORDS,
} from '../../utils/words'

/** A paste backstop only — 50 words of ordinary English is far under this. */
const MAX_DELETE_REASON_CHARS = 2000

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
  /** Receives the reason, which the page forwards to the API. */
  onConfirm: (remark: string) => void | Promise<void>
}

export default function ProjectDeleteDialog({
  project,
  onClose,
  onConfirm,
}: ProjectDeleteDialogProps): React.JSX.Element {
  const [chatCount, setChatCount] = useState<number | null>(null)
  const [remark, setRemark] = useState('')
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

  const words = countWords(remark)
  const remarkValid = words >= MIN_DELETE_REASON_WORDS && words <= MAX_DELETE_REASON_WORDS
  // `busy` STAYS in the guard: the button must still disable while the request is in
  // flight, which is a different concern from whether the reason is valid.
  const canDelete = remarkValid && !busy

  // WHO THIS WILL BE RECORDED AGAINST, shown rather than asked. The server stamps the name
  // from the session and ignores anything the client sends, so this is a readback of what
  // WILL be stored, not an input that decides it — which is why it cannot be edited.
  //
  // `null` when the profile has not been cached (it is fetched at sign-in, so this is the
  // rare cold path). The row is still stamped correctly either way, so the fallback says
  // the true thing without naming anybody it cannot name.
  const me = getStoredUser()
  const signedAs = me === null ? null : me.display_name || me.email

  const confirm = async (): Promise<void> => {
    if (!canDelete) return
    setBusy(true)
    await onConfirm(remark)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 font-manrope">
      {/* The softened overlay (#158 §9) — same values as the create dialog. */}
      <div
        className="absolute inset-0 bg-slate-900/15 backdrop-blur-[3px] [-webkit-backdrop-filter:blur(3px)]"
        onClick={busy ? undefined : onClose}
      />
      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-xl bg-red-50 flex items-center justify-center flex-shrink-0">
            <AlertTriangle size={17} className="text-danger" />
          </div>
          <h3 className="text-base font-bold text-tertiary">Delete “{project.name}”?</h3>
        </div>

        <p className="text-sm text-neutral mt-3 leading-relaxed">{cascadeCopy(chatCount)}</p>

        <p className="text-sm font-semibold text-tertiary mt-4">
          Are you sure you want to delete this project?
        </p>

        {/* NAMED, NOT ASKED. Telling someone which account a permanent deletion is about to
            be recorded against is worth a line; asking them to type it is not, because a
            typed name can name the wrong person and this is the field an administrator
            reads to find out who deleted something. */}
        <p className="text-[11px] text-neutral mt-3">
          {signedAs === null
            ? 'This deletion is recorded against your account.'
            : `Recorded against ${signedAs}.`}
        </p>

        <label className="block mt-3">
          <span className="text-xs font-semibold text-tertiary">
            Why are you deleting this project?
          </span>
          <Textarea
            autoFocus
            value={remark}
            onChange={(e) => setRemark(e.target.value)}
            rows={3}
            maxLength={MAX_DELETE_REASON_CHARS}
            aria-label="Why are you deleting this project?"
            className="mt-1.5 resize-y"
          />
          <div className="flex items-baseline justify-between mt-1">
            {/* Says who reads it. The remark is written by whoever deletes — usually the
                owner — and read by administrators, so it is not a private note. */}
            <span className="text-[11px] text-neutral">
              Between {MIN_DELETE_REASON_WORDS} and {MAX_DELETE_REASON_WORDS} words. An
              administrator can see this.
            </span>
            <span
              className={`text-[11px] tabular-nums ${
                remark.length > 0 && !remarkValid ? 'text-danger font-semibold' : 'text-neutral'
              }`}
            >
              {words}/{MAX_DELETE_REASON_WORDS} words
            </span>
          </div>
        </label>

        <div className="flex gap-3 mt-5">
          <button
            type="button"
            disabled={!canDelete}
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
