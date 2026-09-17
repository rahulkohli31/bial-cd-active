/**
 * Delete-a-project confirmation. Owes the user two things before the trigger: it names the whole cascade out
 * loud — project, app, its own Postgres DB and files, every filed chat, counted via `listProjectConversations`
 * (fallback logic in the docstring below) — and asks WHY in 5-50 words, split the same way as
 * `src/core/words.py`, gating confirm. WHO it's recorded against is shown, never collected (see `getStoredUser`
 * below); the retyped-name gate this replaced is gone in favour of that reason (see the helper-text comment
 * below for what the `deleted_projects` tombstone supports today).
 *
 * Built on the vendored Radix `Dialog`, not hand-rolled — the prior shell announced nothing and trapped no
 * focus. The dialog deletes nothing itself: the page owns optimistic removal and 404-vs-500 reconciliation;
 * this only collects confirmation and calls `onConfirm`.
 */
import { useEffect, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import { CONVERSATION_LIST_CAP, listProjectConversations } from '../../utils/conversationApi'
import type { Project } from '../../utils/projectApi'
import { getStoredUser } from '../../utils/auth'
import { Textarea } from '../ui/textarea'
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog'
import {
  countWords,
  MAX_DELETE_REASON_WORDS,
  MIN_DELETE_REASON_WORDS,
  MAX_DELETE_REASON_CHARS,
} from '../../utils/words'

/** Ties the TEXTAREA to the rule it must satisfy and to its own running count.
 *
 *  The field carried `aria-label` and nothing else, so a reader heard "Why are you deleting this
 *  project?" and was told neither the bound nor how close they were to it — the two facts the
 *  sighted person reads directly under the box. The count is live in its own right: it changes on
 *  every keystroke, which is exactly what `aria-describedby` re-reads on demand rather than
 *  announcing at people.
 *
 *  NOT ON THE CONTENT. `DialogContent`'s own `aria-describedby` stays pointed at the cascade
 *  sentence — that is what a reader should hear after the title, and the one thing in here they
 *  must not miss. Two different elements, two different descriptions. */
const RULE_ID = 'delete-remark-rule'
const COUNT_ID = 'delete-remark-count'

/** Ties the panel to its cascade sentence for `aria-describedby`. */
const CASCADE_ID = 'delete-project-cascade'

/** A paste backstop only — 50 words of ordinary English is far under this. */

/**
 * `null` count = not resolved yet (loading, or the count call failed) → name the cascade
 * with no number, never a flashed wrong one. A count landing exactly ON the server's row
 * cap means "at least this many" (no cursor, so there may be more) — quoting it as a total
 * would state a falsehood right before an irreversible cascade. Say "or more".
 */
/**
 * The half of the cascade that has no row count to quote, and the half that is genuinely
 * irreversible. Every project owns its own database from the moment it is created — before
 * it has an app, before it has a single chat — so this sentence belongs on ALL four
 * branches, including the zero-chat one, which is otherwise the quietest copy in the dialog
 * about the most data. Deleting the project drops that database outright: no export, no
 * snapshot, no undo.
 */
const IRREVERSIBLE = 'The database and files behind it are destroyed permanently. This cannot be undone.'

function cascadeCopy(chatCount: number | null): string {
  if (chatCount === null) return `This deletes the application and all of its chats. ${IRREVERSIBLE}`
  if (chatCount === 0) return `This deletes the application. ${IRREVERSIBLE}`
  if (chatCount >= CONVERSATION_LIST_CAP) {
    return `This deletes the application and all ${CONVERSATION_LIST_CAP} or more of its chats. ${IRREVERSIBLE}`
  }
  return `This deletes the application and all ${chatCount} chat${chatCount === 1 ? '' : 's'}. ${IRREVERSIBLE}`
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
    <Dialog
      open
      onOpenChange={(next) => {
        // Radix routes Escape, the overlay click and the close button through here. `busy`
        // holds it open mid-request exactly as the hand-rolled overlay's guard did.
        if (!next && !busy) onClose()
      }}
    >
      <DialogContent
        hideClose
        // The softened overlay, unchanged — the values, the `-webkit-` prefix, and the
        // overlay ONLY. Passed as an override because the vendored default is `bg-black/80`.
        overlayClassName="bg-slate-900/15 backdrop-blur-[3px] [-webkit-backdrop-filter:blur(3px)]"
        className="font-manrope bg-white rounded-2xl shadow-2xl w-full max-w-md p-6 gap-0 border-0"
        // The cascade sentence IS the description — what a screen reader should hear after
        // the title, and the one thing in here a reader must not miss.
        aria-describedby={CASCADE_ID}
      >
        <div className="flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-xl bg-red-50 flex items-center justify-center flex-shrink-0">
            <AlertTriangle size={17} className="text-danger" />
          </div>
          <DialogTitle className="text-base font-bold text-tertiary">
            Delete “{project.name}”?
          </DialogTitle>
        </div>

        <p id={CASCADE_ID} className="text-sm text-neutral mt-3 leading-relaxed">
          {cascadeCopy(chatCount)}
        </p>

        <p className="text-sm font-semibold text-tertiary mt-4">
          Are you sure you want to delete this application?
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
            Why are you deleting this application?
          </span>
          <Textarea
            autoFocus
            value={remark}
            onChange={(e) => setRemark(e.target.value)}
            rows={3}
            maxLength={MAX_DELETE_REASON_CHARS}
            aria-label="Why are you deleting this application?"
            aria-describedby={`${RULE_ID} ${COUNT_ID}`}
            className="mt-1.5 resize-y"
          />
          <div className="flex items-baseline justify-between mt-1">
            {/* SAYS ONLY WHAT IS TRUE TODAY. This read "An administrator can see this",
                and nothing reads `deleted_projects` — there is no route, no schema and no
                screen. Every deletion collects a mandatory 5-50 word justification, so a
                promise about who reads it is a promise to a user, not an internal TODO. The
                read surface is tracked separately; when it lands, the stronger sentence becomes
                true again and this reverts. Until then the copy says what the platform
                actually does, which is keep the reason with the record. */}
            <span id={RULE_ID} className="text-[11px] text-neutral">
              Between {MIN_DELETE_REASON_WORDS} and {MAX_DELETE_REASON_WORDS} words. Kept with
              the deletion record.
            </span>
            <span
              id={COUNT_ID}
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
            {busy ? <BusyGlyph size={15} /> : null} Delete application
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
      </DialogContent>
    </Dialog>
  )
}
