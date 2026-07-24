/**
 * ToolActivityGroup — the Mode-B collapsible (F3/U3): the Mode-A line grown up. A Radix
 * `Collapsible` whose collapsed header IS a `ToolActivityLine` summary + a chevron; expanded, an
 * `<ol>` of `ToolActivityLine` rows on a hairline `border-l` rail.
 *
 * It auto-opens while the run is in flight and auto-collapses ~600ms after the terminal step so the
 * app/answer below isn't pushed down; the collapsed summary keeps "· N steps". Radix (not a native
 * `<details>`) because the auto-open/collapse needs a CONTROLLED `open`. The header is a real
 * `<button>` — Radix wires `aria-expanded` + `aria-controls`, the chevron is `aria-hidden`, and one
 * polite live region announces only on the terminal transition. The spinner + collapse timing are
 * gated behind `prefers-reduced-motion`.
 */
import { ChevronDown } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '../ui/collapsible'
import { ToolActivityLine, usePrefersReducedMotion, type ToolActivityState } from './ToolActivityLine'

const AUTO_COLLAPSE_MS = 600

export interface ToolActivityGroupStep {
  seq: number
  label: string
  state: ToolActivityState
}

export interface ToolActivityGroupProps {
  summaryLabel: string
  summaryState?: ToolActivityState
  steps: ToolActivityGroupStep[]
  /** In-flight → auto-open; the first `false` after `true` announces + auto-collapses. */
  running: boolean
  /** Accessible name for the step list. */
  ariaLabel?: string
  className?: string
}

export function ToolActivityGroup({
  summaryLabel,
  summaryState = 'started',
  steps,
  running,
  ariaLabel = 'Activity',
  className,
}: ToolActivityGroupProps) {
  const reduced = usePrefersReducedMotion()
  const [open, setOpen] = useState(running)
  const [announcement, setAnnouncement] = useState('')
  const wasRunning = useRef(running)

  useEffect(() => {
    if (running) {
      setOpen(true)
      wasRunning.current = true
      return undefined
    }
    if (!wasRunning.current) return undefined
    // The terminal transition: announce once, then auto-collapse (immediately under reduced motion).
    wasRunning.current = false
    const failed = steps.some((step) => step.state === 'failed')
    setAnnouncement(failed ? `${summaryLabel} — finished with issues` : `${summaryLabel} — done`)
    const timer = setTimeout(() => setOpen(false), reduced ? 0 : AUTO_COLLAPSE_MS)
    return () => clearTimeout(timer)
  }, [running, reduced, steps, summaryLabel])

  const metadata = `· ${steps.length} ${steps.length === 1 ? 'step' : 'steps'}`

  return (
    <Collapsible open={open} onOpenChange={setOpen} className={cn('space-y-1', className)}>
      <CollapsibleTrigger className="flex w-full items-center gap-2 text-left">
        <ToolActivityLine
          label={summaryLabel}
          state={summaryState}
          metadata={metadata}
          className="min-w-0 flex-1"
        />
        <ChevronDown
          size={14}
          aria-hidden="true"
          className={cn(
            'flex-shrink-0 text-neutral/50',
            !reduced && 'transition-transform',
            open && 'rotate-180',
          )}
        />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <ol
          aria-label={ariaLabel}
          className="ml-1.5 space-y-1 border-l border-bial-border pl-3 pt-1"
        >
          {steps.map((step) => (
            <li key={step.seq} data-kind="tool-step" data-state={step.state}>
              <ToolActivityLine label={step.label} state={step.state} />
            </li>
          ))}
        </ol>
      </CollapsibleContent>
      <div className="sr-only" role="status" aria-live="polite">
        {announcement}
      </div>
    </Collapsible>
  )
}
