import { useState, useEffect, useCallback } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { AlertCircle, RefreshCw } from 'lucide-react'
import { BusyGlyph } from '../ui/Waiting'
import { fetchFeedback } from '../../utils/admin'
import type { FeedbackItem } from '../../utils/admin'
import AdminDataTable from './AdminDataTable'

const fmtWhen = (iso: string): string => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

const SEARCHABLE = new Set(['email', 'message', 'page'])

const columns: ColumnDef<FeedbackItem>[] = [
  {
    id: 'email',
    accessorFn: (f) => f.email,
    header: 'User',
    cell: ({ row }) => <span className="whitespace-nowrap text-tertiary font-medium">{row.original.email}</span>,
  },
  {
    id: 'message',
    accessorFn: (f) => f.message,
    header: 'Message',
    // Plain text, clamped: messages can run to 4000 bytes. Full text in the title tooltip; never whitespace-nowrap.
    cell: ({ row }) => (
      <p className="max-w-md text-tertiary line-clamp-2 break-words" title={row.original.message}>
        {row.original.message}
      </p>
    ),
  },
  {
    id: 'page',
    accessorFn: (f) => f.page,
    header: 'Page',
    cell: ({ row }) =>
      row.original.page ? (
        <span className="text-[11px] font-mono text-neutral bg-surface-muted border border-bial-border rounded px-1.5 py-0.5">
          {row.original.page}
        </span>
      ) : (
        <span className="text-neutral">—</span>
      ),
  },
  {
    id: 'createdAt',
    accessorFn: (f) => f.createdAt,
    header: 'When',
    cell: ({ row }) => <span className="whitespace-nowrap text-neutral">{fmtWhen(row.original.createdAt)}</span>,
  },
]

/**
 * Admin "Feedback" panel — read-only list of submitted feedback, newest first.
 * Fetch-on-mount with loading/error/retry, then the shared admin table with search, sort
 * and paging over the loaded rows. Backed by the admin-gated
 * /api/admin/feedback endpoint. Feedback is rendered as PLAIN, React-escaped text
 * (no markdown, no raw HTML) — it is untrusted free input (Decision 10). The
 * `page` chip is plain text, never a link (Decision 4).
 */
export default function FeedbackPanel() {
  const [feedback, setFeedback] = useState<FeedbackItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const { feedback: rows, total: n } = await fetchFeedback()
      setFeedback(rows)
      setTotal(n)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
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
        <BusyGlyph size={16} /> Loading feedback…
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

  return (
    <AdminDataTable<FeedbackItem>
      columns={columns}
      rows={feedback}
      searchable={SEARCHABLE}
      searchLabel="Search feedback"
      searchPlaceholder="Search user, message or page…"
      emptyMessage="No feedback yet."
      truncated={total > feedback.length}
    />
  )
}
