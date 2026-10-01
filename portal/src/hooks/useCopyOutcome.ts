import { useCallback, useEffect, useRef, useState } from 'react'
import { ClipboardRefused, copyToClipboard } from '../utils/clipboard'

export type CopyOutcome = 'idle' | 'copied' | 'refused'

const COPIED_FOR_MS = 2500

/**
 * The state a copy control reports: `copied` for a moment, then `idle`, or `refused` until the
 * next press. `copy` settles once the outcome is set and rejects with anything that is not a
 * `ClipboardRefused`. `reset` returns to `idle` and drops an outcome still in flight.
 */
export function useCopyOutcome(): { outcome: CopyOutcome; copy: (text: string) => Promise<void>; reset: () => void } {
  const [outcome, setOutcome] = useState<CopyOutcome>('idle')
  const generation = useRef(0)

  useEffect(() => {
    if (outcome !== 'copied') return undefined
    const timer = window.setTimeout(() => setOutcome('idle'), COPIED_FOR_MS)
    return () => window.clearTimeout(timer)
  }, [outcome])

  const copy = useCallback((text: string): Promise<void> => {
    const issued = generation.current
    const settle = (next: CopyOutcome) => {
      if (generation.current === issued) setOutcome(next)
    }
    return copyToClipboard(text).then(
      () => settle('copied'),
      (error: unknown) => {
        if (!(error instanceof ClipboardRefused)) throw error
        settle('refused')
      },
    )
  }, [])

  const reset = useCallback(() => {
    generation.current += 1
    setOutcome('idle')
  }, [])

  return { outcome, copy, reset }
}
