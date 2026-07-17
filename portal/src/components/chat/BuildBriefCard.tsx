/**
 * The build proposal card (003-U3) — the ONE build trigger on a project thread.
 *
 * The model signals "I have enough to build this" with a `bial:build-brief` fence; the thread
 * parses it (`utils/buildBrief.ts`) and renders this card in place of the raw block. Confirming
 * is what starts the build: nothing auto-fires, so the user always sees what will be built.
 * That is also what makes the DEGRADED variant safe — a malformed fence still yields a card, and
 * the brief on it is the thing the user reads before agreeing to it.
 *
 * The same card drives iteration: on a thread whose app already exists, "Build this" performs the
 * existing stop+start refine behind the scenes. The user is agreeing to a brief either way.
 */
import { Hammer, Sparkles, AlertTriangle } from 'lucide-react'

export interface BuildBriefCardProps {
  /** The refined brief — exactly what will be sent to the builder. */
  brief: string
  /** True when the fence was malformed/duplicated: hedge the wording, keep the action. */
  degraded?: boolean
  /** Disables the action while a start is already in flight (the build-intent gate). */
  busy?: boolean
  /** True once this brief's build has been started — the card stays as a transcript record. */
  started?: boolean
  /**
   * True when a LATER turn carries a brief: this one no longer describes the app the thread is
   * about. The action must be dead here — every card lives in the transcript forever, so an armed
   * superseded brief is one scroll and one click away from rebuilding the app from an obsolete
   * spec, over the bundle the newer brief just built. `started` cannot cover this: it is per-mount
   * state, so a reload re-arms every historical card, including one that already built.
   */
  superseded?: boolean
  /** Iteration (the thread already has a live/last session) — the action reads as a rebuild. */
  refine?: boolean
  /** A failed start, surfaced inline so the retry is where the action is. */
  error?: string | null
  onBuild: () => void
}

export default function BuildBriefCard({
  brief,
  degraded = false,
  busy = false,
  started = false,
  superseded = false,
  refine = false,
  error = null,
  onBuild,
}: BuildBriefCardProps) {
  // A live build outranks a supersede (confirm a brief, keep chatting, and the card you are
  // watching build IS superseded — it should still say so), and a supersede outranks this card's
  // own error: retrying a brief the thread has moved past is never the action the user wants,
  // however the start failed.
  const label = started
    ? 'Building…'
    : superseded
      ? 'Replaced by a newer brief'
      : error
        ? 'Try again'
        : refine
          ? 'Rebuild with these changes'
          : 'Build this'
  const actionable = !started && !superseded

  return (
    <div
      data-testid="build-brief-card"
      data-degraded={degraded ? 'true' : 'false'}
      data-superseded={superseded ? 'true' : 'false'}
      className="mt-2 rounded-xl border border-secondary/30 bg-white overflow-hidden"
    >
      <div className="flex items-center gap-1.5 px-3 py-2 border-b border-secondary/20 bg-secondary/5">
        <Sparkles size={11} className="text-secondary flex-shrink-0" />
        <p className="text-[11px] font-bold text-tertiary">
          {refine ? "Here's the updated app" : "Here's what I'll build"}
        </p>
      </div>

      <p className="px-3 py-2.5 text-xs leading-relaxed text-tertiary whitespace-pre-wrap break-words">
        {brief}
      </p>

      {degraded && (
        // Say what we did, not that "parsing failed" — the user cannot act on our internals, but
        // they CAN act on "check this before I build it".
        <p className="flex items-start gap-1.5 px-3 pb-2 text-[10px] leading-relaxed text-neutral">
          <AlertTriangle size={11} className="text-warning flex-shrink-0 mt-0.5" />
          <span>Give this a quick read before building — I may not have captured it cleanly.</span>
        </p>
      )}

      {error && (
        <p role="alert" className="mx-3 mb-2 text-[10px] text-danger bg-danger/5 border border-danger/20 rounded-lg px-2 py-1.5">
          {error}
        </p>
      )}

      <div className="px-3 pb-3">
        <button
          type="button"
          onClick={onBuild}
          disabled={busy || started || superseded}
          className="w-full flex items-center justify-center gap-1.5 bg-secondary hover:bg-secondary-600 disabled:opacity-40 disabled:cursor-not-allowed text-white text-xs font-bold rounded-lg px-3 py-2 transition"
        >
          {label}
          {actionable && <Hammer size={11} />}
        </button>
        {actionable && (
          <p className="mt-1.5 text-[10px] text-neutral text-center">
            Or keep chatting to change anything first.
          </p>
        )}
        {superseded && (
          <p className="mt-1.5 text-[10px] text-neutral text-center">
            Scroll down for the brief this thread is building now.
          </p>
        )}
      </div>
    </div>
  )
}
