import { useState, useEffect, useCallback } from 'react'
import { AlertCircle, Loader2, RefreshCw, MessageSquare } from 'lucide-react'
import { fetchFeedback } from '../../utils/admin'

const fmtWhen = (iso) => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

/**
 * Admin "Feedback" panel — read-only list of submitted feedback, newest first.
 * Near-clone of UsersLimitsPanel: fetch-on-mount with loading/error/retry, a
 * Tailwind table, and an empty state. Backed by the admin-gated
 * /api/admin/feedback endpoint. Feedback is rendered as PLAIN, React-escaped text
 * (no markdown, no raw HTML) — it is untrusted free input (Decision 10). The
 * `page` chip is plain text, never a link (Decision 4).
 */
export default function FeedbackPanel() {
  const [feedback, setFeedback] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const { feedback: rows, total: n } = await fetchFeedback()
      setFeedback(rows)
      setTotal(n)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (loading) {
    return (
      <div className="flex items-center justify-center gap-2 py-16 text-neutral text-sm">
        <Loader2 size={16} className="animate-spin" /> Loading feedback…
      </div>
    )
  }

  if (error) {
    return (
      <div className="text-center py-16">
        <AlertCircle size={20} className="text-red-500 mx-auto mb-3" />
        <p className="text-sm text-tertiary font-semibold">Couldn’t load feedback</p>
        <p className="text-xs text-neutral mt-1">{error}</p>
        <button
          onClick={load}
          className="mt-4 inline-flex items-center gap-1.5 px-4 py-2 rounded-xl border border-bial-border text-sm font-medium text-tertiary hover:bg-bial-bg transition"
        >
          <RefreshCw size={14} /> Retry
        </button>
      </div>
    )
  }

  if (feedback.length === 0) {
    return (
      <div className="text-center py-16">
        <div className="w-12 h-12 rounded-2xl bg-bial-bg flex items-center justify-center mx-auto mb-3">
          <MessageSquare size={20} className="text-neutral" />
        </div>
        <p className="text-sm text-neutral">No feedback yet.</p>
      </div>
    )
  }

  return (
    <>
      {total > feedback.length && (
        <p className="text-xs text-neutral mb-4">
          Showing newest {feedback.length} of {total.toLocaleString('en-US')} — older feedback needs pagination
          (deferred).
        </p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-bial-border">
              <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">User</th>
              <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Message</th>
              <th className="pb-3 pr-6 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">Page</th>
              <th className="pb-3 text-left text-[10px] font-bold uppercase tracking-wider text-neutral">When</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-bial-border">
            {feedback.map((f, i) => (
              <tr key={`${f.createdAt}-${i}`} className="hover:bg-bial-bg/50 transition align-top">
                <td className="py-3 pr-6 whitespace-nowrap text-tertiary font-medium">{f.email}</td>
                <td className="py-3 pr-6">
                  {/* Plain text, clamped: messages can run to 4000 bytes. Full text
                      in the title tooltip; never whitespace-nowrap. */}
                  <p className="max-w-md text-tertiary line-clamp-2 break-words" title={f.message}>
                    {f.message}
                  </p>
                </td>
                <td className="py-3 pr-6">
                  {f.page ? (
                    <span className="text-[11px] font-mono text-neutral bg-surface-muted border border-bial-border rounded px-1.5 py-0.5">
                      {f.page}
                    </span>
                  ) : (
                    <span className="text-neutral">—</span>
                  )}
                </td>
                <td className="py-3 text-neutral whitespace-nowrap">{fmtWhen(f.createdAt)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
