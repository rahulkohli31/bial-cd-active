import { useEffect, useState } from 'react'
import { fetchUsageToday, onUsageChanged } from '../utils/usage'
import type { UsageToday } from '../utils/usage'
import { isAuthenticated } from '../utils/auth'

/**
 * Today's token budget, for every surface that shows it.
 *
 * IT IS READ IN TWO PLACES NOW — the navigation panel and the workspace toolbar — and those two
 * must never disagree about the same day's figures. One hook, one read, one subscription to the
 * change notice, so a turn that spends tokens moves both at once.
 *
 * `null` MEANS "NO READING", never zero. Signed out, or a read that failed: either way the
 * meter draws nothing rather than a confident 0 of 0, which would say the budget is spent.
 */
export function useUsageToday(): UsageToday | null {
  const [usage, setUsage] = useState<UsageToday | null>(null)

  useEffect(() => {
    let active = true
    const load = async () => {
      if (!isAuthenticated()) {
        if (active) setUsage(null)
        return
      }
      const data = await fetchUsageToday()
      if (active) setUsage(data)
    }
    void load()
    const off = onUsageChanged(() => void load())
    return () => {
      active = false
      off()
    }
  }, [])

  return usage
}
