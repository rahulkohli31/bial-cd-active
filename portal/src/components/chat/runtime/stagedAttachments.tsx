/**
 * THE ADAPTER, BOUND TO THE RUNTIME IT IS REGISTERED ON (plan 002, U5).
 *
 * `add` has to see what is ALREADY staged, because the per-message file cap and the
 * per-conversation text-byte budget are both cumulative — checking only the arriving file is the
 * cap bypass R57 records. But the staged list lives ON the runtime, and the runtime cannot be
 * built until the adapter has been handed to it. So the adapter reads through a ref, and a tiny
 * component INSIDE the provider — the only place `useAui` resolves — keeps that ref current.
 *
 * PER PROVIDER, NEVER MODULE-LEVEL. Two runtimes can be mounted at once (the project rail's
 * composer-only one and a chat's), and a shared ref would have each one validating against the
 * other's staged files.
 *
 * A REF RATHER THAN A CLOSURE OVER STATE: `add` runs at pick time, long after the render that
 * created the adapter, so a captured array would be whatever was staged when the composer last
 * rendered rather than what is staged now.
 *
 * AND WHAT THE REF HOLDS IS A READER, NOT A COPY, which is a correction rather than a refinement.
 * It used to hold the array `useAuiState` had last handed the binding — one RENDER behind the
 * runtime, because the composer appends an attachment the instant `add` resolves and React
 * repaints afterwards. The cap counted against that stale copy, so a file that had genuinely
 * landed was invisible to the very next `add` until a repaint caught up. Reading
 * `composer.getState()` on demand closes the gap outright: the store is the runtime's own state,
 * and there is nothing left to lag. `ComposerBox` reads press-time state the same way and for the
 * same reason — a render-scoped copy is one keystroke stale.
 */
import { createContext, useContext, useMemo, useRef, type MutableRefObject, type ReactNode } from 'react'
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
  const adapter = useMemo(
    () =>
      createAttachmentAdapter({
        accept: ACCEPT_ATTR,
        staged: () => stagedRef.current(),
        onRefused: (message) => refusalRef.current(message),
      }),
    [],
  )
  return { adapter, stagedRef, refusalRef }
}

/**
 * THE REFUSAL SINK, AS CONTEXT rather than as a prop chain.
 *
 * The provider that builds the runtime is mounted ABOVE the composer and does not know which of
 * its children is the one with a voice — on the chat surface the composer is several levels down,
 * beside a transcript and half a dozen banners. Threading a ref through those would be a prop
 * nobody in between has any business carrying.
 *
 * `null` outside a provider is the honest default: a composer rendered with no runtime cannot
 * stage a file at all, so there is nothing for a refusal to be about.
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
 * Renders nothing. Its whole job is to run `useAui` inside the provider and hand the adapter a way
 * to read the composer it was registered on.
 *
 * IT SUBSCRIBES TO NOTHING, deliberately. It used to hold `useAuiState(s => s.composer.attachments)`
 * and re-render on every staged file so the ref could be refreshed; what it publishes now is a
 * reader, so the ref is correct without being refreshed at all and a keystroke costs one component
 * less work.
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
