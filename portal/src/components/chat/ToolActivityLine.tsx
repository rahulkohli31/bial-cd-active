/**
 * ToolActivityLine — the shared Mode-A atom for build/tool activity (F3/U3). ONE renderer used by
 * both the LIVE `BuildProgress` step rows and the RELOAD stored-step rows in `BuilderPage`, so the
 * two feeds can never visually diverge.
 *
 * A chrome-free flex row — no card, no border: `[state glyph 14px] friendly label`.
 * The friendly label is neutral-coloured in every state; a FAILED row conveys failure by the glyph
 * SHAPE (a cross, not merely a red tint) plus a visually-hidden "failed" — never by colour alone
 * (WCAG 1.4.1). Height is constant across states so a line never reflows as it resolves. The
 * running spinner is gated behind `prefers-reduced-motion`.
 *
 * Palette: the portal's CUSTOM tokens the `BuildProgress` bubble already uses (`text-danger`,
 * `text-primary`, `text-tertiary`) — NOT shadcn's `text-destructive` / `text-muted-foreground`, so
 * the new lines don't diverge inside the same bubble.
 */
import { CheckCircle2, Loader2, XCircle } from 'lucide-react'
import { useEffect, useState } from 'react'
import { cn } from '@/lib/utils'
import { assertNever } from '../../utils/assertNever'

/** Live steps are started/ok/failed; reloaded steps are ok/failed/pending — the atom spans both. */
export type ToolActivityState = 'started' | 'pending' | 'ok' | 'failed'

function readReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

/** Tracks `prefers-reduced-motion`; SSR/jsdom-safe (no matchMedia → false, i.e. animate). */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(readReducedMotion)
  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return undefined
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = () => setReduced(mq.matches)
    onChange()
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [])
  return reduced
}

function StateGlyph({ state, reduced }: { state: ToolActivityState; reduced: boolean }) {
  switch (state) {
    case 'ok':
      return <CheckCircle2 size={14} aria-hidden="true" className="flex-shrink-0 text-green-600" />
    case 'failed':
      return <XCircle size={14} aria-hidden="true" className="flex-shrink-0 text-danger" />
    case 'started':
      return (
        <Loader2
          size={14}
          aria-hidden="true"
          className={cn('flex-shrink-0 text-primary', !reduced && 'animate-spin')}
        />
      )
    case 'pending':
      return <Loader2 size={14} aria-hidden="true" className="flex-shrink-0 text-neutral/40" />
    default:
      return assertNever(state)
  }
}

export interface ToolActivityLineProps {
  label: string
  state: ToolActivityState
  className?: string
}

export function ToolActivityLine({ label, state, className }: ToolActivityLineProps) {
  const reduced = usePrefersReducedMotion()
  return (
    <span
      // `relative` contains the sr-only "failed" span: sr-only is position:absolute with no
      // inset, so without a positioned ancestor it anchors to the DOCUMENT and lands ~11,000px
      // down a long transcript, stretching the page (measured 11,558px vs an 836px viewport).
      className={cn('relative inline-flex w-full items-center gap-2 text-xs text-tertiary', className)}
      data-kind="tool-activity"
      data-state={state}
    >
      <StateGlyph state={state} reduced={reduced} />
      <span className="min-w-0 truncate">{label}</span>
      {/* Failure carried as TEXT, not colour alone (WCAG 1.4.1) — the label stays neutral. */}
      {state === 'failed' && <span className="sr-only">failed</span>}
    </span>
  )
}
