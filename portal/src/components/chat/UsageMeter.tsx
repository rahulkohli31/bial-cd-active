/**
 * UsageMeter (U13/R12) — the minimal daily-usage strip for the chat header: a thin bar +
 * "N% of today's assistant usage". Reads the EXISTING daily-cap API through the same
 * helper the navbar badge uses (one fetch path, one refresh signal); renders nothing
 * until a successful read (a 401 mid-logout must not flash a broken meter).
 */

import { useEffect, useState } from 'react'

import { fetchUsageToday, onUsageChanged } from '../../utils/usage'

interface UsageToday {
  used: number
  limit: number
  remaining: number
  resetsAt?: string
}

function isUsageToday(value: unknown): value is UsageToday {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { used?: unknown }).used === 'number' &&
    typeof (value as { limit?: unknown }).limit === 'number'
  )
}

export function UsageMeter() {
  const [usage, setUsage] = useState<UsageToday | null>(null)

  useEffect(() => {
    let cancelled = false
    const refresh = () => {
      void fetchUsageToday().then((data: unknown) => {
        if (!cancelled && isUsageToday(data)) setUsage(data)
      })
    }
    refresh()
    const unsubscribe = onUsageChanged(refresh) as () => void
    return () => {
      cancelled = true
      unsubscribe()
    }
  }, [])

  if (usage === null || usage.limit <= 0) return null
  const percent = Math.min(100, Math.round((usage.used / usage.limit) * 100))
  return (
    <div className="usage-meter" title={`${usage.used.toLocaleString()} of ${usage.limit.toLocaleString()} tokens today`}>
      <div
        role="progressbar"
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Daily assistant usage"
        className="usage-meter-track"
      >
        <div className="usage-meter-fill" style={{ width: `${percent}%` }} />
      </div>
      <span className="usage-meter-label">{percent}% of today&apos;s assistant usage</span>
    </div>
  )
}
