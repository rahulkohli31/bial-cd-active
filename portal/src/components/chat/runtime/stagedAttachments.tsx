/**
 * THE ADAPTER, BOUND TO THE RUNTIME IT IS REGISTERED ON.
 *
 * WHY THIS EXISTS: `add` must see what is ALREADY staged — the per-message file cap and the
 * per-conversation byte budget are both cumulative, and checking only the arriving file is
 * exactly how a cap gets bypassed. The staged list lives ON the runtime, but the runtime cannot
 * be built until the adapter is handed to it — so the adapter reads through a ref, kept current
 * by a tiny component INSIDE the provider (the only place `useAui` resolves).
 *
 * PER PROVIDER, NEVER MODULE-LEVEL: two runtimes can mount at once (the rail's composer-only one
 * and a chat's), and a shared ref would validate each against the other's staged files. A REF,
 * NOT A CLOSURE, because `add` runs at pick time, long after the render that built the adapter —
 * a captured array would be stale.
 *
 * THE REF HOLDS A READER, NOT A COPY — it used to hold the last-handed array, refreshed by
 * re-rendering on every staged file; now it holds a function, so nothing needs refreshing and
 * there is no window where it points at a thrown-away render.
 *
 * THIS IS NOT A LAG-FREE VIEW, though it was once described as one: `composer.getState()` is a
 * snapshot fed through React, so inside a single commit the runtime already knows a just-added
 * file before `getState()` does — the reader is only as current as the last render. The gap is
 * covered instead by the adapter's OWN claim list (`attachmentAdapter.ts`), which counts what
 * `add` has accepted and is still reading; the one case neither covers is two separate picks
 * landing inside one repaint, which would need reading an internal the library does not expose.
 */
import { createContext, useContext, useMemo, useRef, useState, type MutableRefObject, type ReactNode } from 'react'
import { useAui, type Attachment, type AttachmentAdapter } from '@assistant-ui/react'
import { ACCEPT_ATTR } from '../../../utils/attachmentInput'
import { createAttachmentAdapter } from './attachmentAdapter'

export interface BoundAdapter {
  adapter: AttachmentAdapter
  /** Render `StagedAttachmentsBinding` with this. It publishes a live READER of the staged list
   *  into the adapter — see the docblock for why a reader rather than the list itself. */
  stagedRef: MutableRefObject<() => readonly Attachment[]>
  /** Where a refused file's sentence goes. The mounted composer fills it — see `useRefusalSink`. */
  refusalRef: MutableRefObject<(message: string) => void>
  /**
   * HOW MANY FILES ARE STILL BEING READ. STATE, not a ref, and that is the whole
   * reason it is here rather than beside the two refs above: the composer has to RE-RENDER when
   * this changes — Send goes unavailable and a pending row appears — and a ref cannot ask for a
   * render. It is the one thing the adapter publishes that the screen has to react to.
   */
  pendingReads: number
}

export function useBoundAttachmentAdapter(): BoundAdapter {
  // NOTHING STAGED UNTIL THE BINDING MOUNTS, which is the honest reading for a composer whose
  // runtime does not exist yet — and it is a function so that the day it does exist, nothing here
  // has to be told.
  const stagedRef = useRef<() => readonly Attachment[]>(() => [])
  // THE REFUSAL SINK IS A REF FOR THE SAME REASON THE STAGED LIST IS: `add` runs at pick time,
  // and the adapter is built once. A composer mounted under this provider registers its own
  // `onUrgent` here; until one does, a refusal has nowhere to go and is dropped rather than
  // thrown at the console.
  const refusalRef = useRef<(message: string) => void>(() => {})
  // A file is not staged until its base64 read has finished, and Send is pressable throughout that
  // window — so a citizen who attaches a workbook and presses Enter sends their question without
  // it. `useState` because the composer must re-render; the setter is stable, so the adapter is
  // still built exactly once.
  const [pendingReads, setPendingReads] = useState(0)
  const adapter = useMemo(
    () =>
      createAttachmentAdapter({
        accept: ACCEPT_ATTR,
        staged: () => stagedRef.current(),
        onRefused: (message) => refusalRef.current(message),
        onReadingChanged: setPendingReads,
      }),
    [],
  )
  return { adapter, stagedRef, refusalRef, pendingReads }
}

/**
 * HOW MANY FILES THE COMPOSER IS STILL READING, as context.
 *
 * The same shape as the refusal sink and for the same reason: the provider that owns the count is
 * mounted above the composer and does not know which of its children has the send control. `0`
 * outside a provider is the honest default — a composer with no runtime can stage nothing, so
 * nothing can be in flight.
 */
const PendingReadsContext = createContext(0)

export function PendingReadsProvider({ value, children }: { value: number; children: ReactNode }) {
  return <PendingReadsContext.Provider value={value}>{children}</PendingReadsContext.Provider>
}

export function usePendingAttachmentReads(): number {
  return useContext(PendingReadsContext)
}

/**
 * THE REFUSAL SINK, AS CONTEXT rather than a prop chain: the provider is mounted ABOVE the
 * composer and does not know which child has the voice — on the chat surface the composer sits
 * several levels down, beside a transcript and banners — so threading a ref through would be a
 * prop nobody in between has business carrying. `null` outside a provider is the honest default:
 * a composer with no runtime cannot stage a file, so there is nothing for a refusal to be about.
 */
const RefusalSinkContext = createContext<MutableRefObject<(message: string) => void> | null>(null)

export function RefusalSinkProvider({
  value,
  children,
}: {
  value: MutableRefObject<(message: string) => void>
  children: ReactNode
}) {
  return <RefusalSinkContext.Provider value={value}>{children}</RefusalSinkContext.Provider>
}

/**
 * REGISTER THE COMPOSER'S OWN URGENT SINK, so a refused file is spoken where that surface speaks.
 * Called BY the composer, because it is the composer that has the voice.
 */
export function useRefusalSink(onUrgent: (message: string) => void): void {
  const sink = useContext(RefusalSinkContext)
  if (sink) sink.current = onUrgent
}

/**
 * Renders nothing; its whole job is running `useAui` inside the provider and handing the adapter
 * a reader over the composer it was registered on. SUBSCRIBES TO NOTHING, deliberately — it used
 * to hold `useAuiState(s => s.composer.attachments)` and re-render on every staged file to
 * refresh the ref; now it publishes a reader instead, so nothing needs refreshing, and nothing
 * downstream wanted those renders anyway.
 */
export function StagedAttachmentsBinding({
  target,
}: {
  target: MutableRefObject<() => readonly Attachment[]>
}) {
  const aui = useAui()
  target.current = () => aui.composer.getState().attachments
  return null
}

/**
 * EVERY PROVIDER A COMPOSER'S ATTACHMENTS NEED, MOUNTED AS ONE.
 *
 * ★ THIS EXISTS BECAUSE THE SEND GATE WAS HALF-SHIPPED. The pending-read count reached the chat
 * composer through `PendingReadsProvider` in `ChatRuntimeProvider` — and the rail composer, which
 * binds the same adapter, stages the same files and renders the same box, mounted the refusal sink
 * and the staged binding but never that provider. `usePendingAttachmentReads()` read the context
 * default `0`, Send's guard never fired, and a file dropped on the rail and sent mid-read landed
 * nowhere while the chat started from the sentence alone.
 *
 * Three pieces wired by hand at each call site is how one gets forgotten, so they are one
 * component: a composer that binds an adapter mounts this, and gets all three or none.
 */
export function AttachmentAdapterProviders({
  bound,
  children,
}: {
  bound: BoundAdapter
  children: ReactNode
}) {
  return (
    <RefusalSinkProvider value={bound.refusalRef}>
      <PendingReadsProvider value={bound.pendingReads}>
        <StagedAttachmentsBinding target={bound.stagedRef} />
        {children}
      </PendingReadsProvider>
    </RefusalSinkProvider>
  )
}
