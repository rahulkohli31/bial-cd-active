import { useState, useRef, useEffect } from 'react'
import type { RefObject } from 'react'
import { useLocation } from 'react-router-dom'
import { MessageSquare, X, Loader2, AlertCircle } from 'lucide-react'
import { submitFeedback } from '../utils/feedback'

// Mirrors the server's MAX_FEEDBACK_CHARS (server/feedback.js). The counter and
// the Submit gate both measure UTF-8 BYTES (TextEncoder), exactly matching the
// server's Buffer.byteLength check — a char-count cap would let multibyte text
// pass here and 400 on the server.
const MAX_FEEDBACK_BYTES = 4000

const byteLength = (s: string): number => new TextEncoder().encode(s).length

export interface FeedbackModalProps {
  open: boolean
  onClose: () => void
  onSubmitted: () => void
  triggerRef?: RefObject<HTMLElement | null>
  submitFn?: (message: string, page: string) => Promise<unknown>
}

/**
 * Single free-text feedback box in a modal. Reads the current route itself (via
 * useLocation, at submit time) so the stored page reflects where the user was —
 * it is NOT passed in as a prop. `submitFn` is injectable for tests; `triggerRef`
 * is the header button focus returns to on close. The parent owns open/close
 * state and toasts via onSubmitted.
 */
export default function FeedbackModal({ open, onClose, onSubmitted, triggerRef, submitFn = submitFeedback }: FeedbackModalProps) {
  const { pathname } = useLocation()
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  // Tracks the latest `open` so an in-flight submit can detect a mid-request
  // dismiss (overlay/X/Escape) and skip toasting/erroring against a closed dialog.
  const openRef = useRef(open)
  useEffect(() => {
    openRef.current = open
  }, [open])

  // On open: focus the textarea and clear any stale draft/error/busy (the
  // component stays mounted across opens — Navbar renders it unconditionally —
  // so resetting busy here also recovers from a dismiss-mid-submit).
  useEffect(() => {
    if (!open) return
    setMessage('')
    setError(null)
    setBusy(false)
    textareaRef.current?.focus()
  }, [open])

  if (!open) return null

  const bytes = byteLength(message)
  const overCap = bytes > MAX_FEEDBACK_BYTES
  const canSubmit = !busy && message.trim().length > 0 && !overCap

  const restoreFocus = () => triggerRef?.current?.focus()

  const close = () => {
    onClose()
    restoreFocus()
  }

  const handleSubmit = async () => {
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      await submitFn(message.trim(), pathname)
      if (!openRef.current) return // dismissed mid-submit — don't toast a closed dialog
      setMessage('')
      setBusy(false)
      restoreFocus()
      onSubmitted() // parent toasts + closes
    } catch (e) {
      if (!openRef.current) return // dismissed mid-submit — error has nowhere to show
      setError(e instanceof Error ? e.message : String(e))
      setBusy(false)
    }
  }

  // Focus trap: keep Tab/Shift+Tab cycling within the modal's focusables.
  const onKeyDownTrap = (e: React.KeyboardEvent) => {
    if (e.key !== 'Tab') return
    const focusables = cardRef.current?.querySelectorAll<HTMLElement>('textarea, button:not([disabled])')
    if (!focusables || focusables.length === 0) return
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

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Send feedback"
    >
      <div className="absolute inset-0 bg-black/40" onClick={busy ? undefined : close} />
      <div ref={cardRef} onKeyDown={onKeyDownTrap} className="relative bg-white rounded-2xl shadow-2xl w-full max-w-md p-6">
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0">
              <MessageSquare size={15} className="text-primary" />
            </div>
            <div>
              <h3 className="text-base font-bold text-tertiary">Send feedback</h3>
              <p className="text-xs text-neutral mt-0.5">Tell us what's wrong or what could be better.</p>
            </div>
          </div>
          <button
            onClick={close}
            disabled={busy}
            aria-label="Close"
            className="p-1.5 text-neutral hover:text-tertiary rounded-lg hover:bg-bial-bg disabled:opacity-50 transition"
          >
            <X size={18} />
          </button>
        </div>

        <textarea
          ref={textareaRef}
          data-testid="feedback-message"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          rows={5}
          placeholder="Describe the issue or suggestion…"
          className="mt-4 w-full border border-bial-border rounded-xl px-3 py-2.5 text-sm text-tertiary placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary resize-none transition"
        />

        <div className="mt-1.5 flex justify-end">
          <span
            data-testid="feedback-counter"
            className={`text-[11px] tabular-nums ${overCap ? 'text-danger font-semibold' : 'text-neutral'}`}
          >
            {bytes.toLocaleString('en-US')} / {MAX_FEEDBACK_BYTES.toLocaleString('en-US')}
          </span>
        </div>

        {error && (
          <div
            data-testid="feedback-error"
            className="mt-3 flex items-start gap-2 bg-red-50 border border-red-200 rounded-xl px-3 py-2.5"
          >
            <AlertCircle size={14} className="text-red-500 flex-shrink-0 mt-0.5" />
            <p className="text-xs text-red-600">{error}</p>
          </div>
        )}

        <div className="flex gap-3 mt-5">
          <button
            onClick={handleSubmit}
            disabled={!canSubmit}
            data-testid="feedback-submit"
            className="flex-1 flex items-center justify-center gap-2 py-2.5 rounded-xl font-semibold text-sm bg-primary text-white hover:bg-primary/90 disabled:opacity-50 transition"
          >
            {busy && <Loader2 size={15} className="animate-spin" />}
            Send feedback
          </button>
          <button
            onClick={close}
            disabled={busy}
            className="flex-1 py-2.5 rounded-xl font-semibold text-sm border border-bial-border text-tertiary hover:bg-bial-bg disabled:opacity-50 transition"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
