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
import { BOTH_ATTACHMENT_LANES, type AttachmentLanes } from '../../../utils/attachmentInput'
import { createAttachmentAdapter } from './attachmentAdapter'

export interface BoundAdapter {
  adapter: AttachmentAdapter
  /** The lanes this adapter was built with — published so the composer's words and the picker's
   *  filter cannot disagree about which formats this surface takes. */
  lanes: AttachmentLanes
  /** Render `StagedAttachmentsBinding` with this. It publishes a live READER of the staged list
   *  into the adapter — see the docblock for why a reader rather than the list itself. */
  stagedRef: MutableRefObject<() => readonly Attachment[]>
  /**
   * HOW MANY FILES ARE STILL BEING READ. STATE, not a ref, and that is the whole
   * reason it is here rather than beside the ref above: the composer has to RE-RENDER when
   * this changes — Send goes unavailable and a pending row appears — and a ref cannot ask for a
   * render. It is the one thing the adapter publishes that the screen has to react to.
   */
  pendingReads: number
}

/**
 * `lanes` IS WHERE A CONVERSATION'S KIND ARRIVES, narrowed to the only thing an adapter can act
 * on. A surface with no workspace passes `MODEL_LANE_ONLY`, and the library's accept filter then
 * refuses a spreadsheet at pick, paste and drop — every path into `add` runs through it — instead
 * of staging a chip and learning the same thing from the server a full upload later.
 */
export function useBoundAttachmentAdapter(lanes: AttachmentLanes = BOTH_ATTACHMENT_LANES): BoundAdapter {
  // NOTHING STAGED UNTIL THE BINDING MOUNTS, which is the honest reading for a composer whose
  // runtime does not exist yet — and it is a function so that the day it does exist, nothing here
  // has to be told.
  const stagedRef = useRef<() => readonly Attachment[]>(() => [])
  // A file is not staged until its base64 read has finished, and Send is pressable throughout that
  // window — so a citizen who attaches a workbook and presses Enter sends their question without
  // it. `useState` because the composer must re-render; the setter is stable, so the adapter is
  // still built exactly once.
  const [pendingReads, setPendingReads] = useState(0)
  const adapter = useMemo(
    () =>
      createAttachmentAdapter({
        accept: lanes.accept,
        staged: () => stagedRef.current(),
        // A REFUSAL REACHES THE COMPOSER THROUGH THE LIBRARY'S OWN `composer.attachmentAddError`
        // event now (see `ComposerBox.tsx`), which carries the same message this hook would have
        // relayed. The adapter still requires one; nothing needs to listen here.
        onRefused: () => {},
        onReadingChanged: setPendingReads,
      }),
    [lanes],
  )
  return { adapter, lanes, stagedRef, pendingReads }
}

/**
 * HOW MANY FILES THE COMPOSER IS STILL READING, as context.
 *
 * The provider that owns the count is mounted above the composer and does not know which of its
 * children has the send control. `0` outside a provider is the honest default — a composer with
 * no runtime can stage nothing, so nothing can be in flight.
 */
const PendingReadsContext = createContext(0)

export function PendingReadsProvider({ value, children }: { value: number; children: ReactNode }) {
  return <PendingReadsContext.Provider value={value}>{children}</PendingReadsContext.Provider>
}

export function usePendingAttachmentReads(): number {
  return useContext(PendingReadsContext)
}

/**
 * WHICH LANES THE COMPOSER BELOW IS SPEAKING FOR. The adapter's `accept` decides what is refused
 * and the composer decides what the refusal says; they are one decision, so the composer reads it
 * from the same value the adapter was built with rather than being handed it a second time. Both
 * lanes outside a provider, which is what a composer with no runtime could only be.
 */
const AttachmentLanesContext = createContext(BOTH_ATTACHMENT_LANES)

export function useAttachmentLanes(): AttachmentLanes {
  return useContext(AttachmentLanesContext)
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
 * Pieces wired by hand at each call site is how one gets forgotten: a composer that mounts
 * the staged binding but not `PendingReadsProvider` reads the context default `0`, and Send's
 * "a file is still arriving" guard never fires; one that skips the lanes says the two-lane
 * sentence over an adapter that takes one. So a composer that binds an adapter mounts this, and
 * gets all of them or none.
 */
export function AttachmentAdapterProviders({
  bound,
  children,
}: {
  bound: BoundAdapter
  children: ReactNode
}) {
  return (
    <AttachmentLanesContext.Provider value={bound.lanes}>
      <PendingReadsProvider value={bound.pendingReads}>
        <StagedAttachmentsBinding target={bound.stagedRef} />
        {children}
      </PendingReadsProvider>
    </AttachmentLanesContext.Provider>
  )
}
