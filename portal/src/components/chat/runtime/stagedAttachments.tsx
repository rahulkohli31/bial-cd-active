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
 */
import { createContext, useContext, useMemo, useRef, type MutableRefObject, type ReactNode } from 'react'
import { useAuiState, type Attachment, type AttachmentAdapter } from '@assistant-ui/react'
import { ACCEPT_ATTR } from '../../../utils/attachmentInput'
import { createAttachmentAdapter } from './attachmentAdapter'

export interface BoundAdapter {
  adapter: AttachmentAdapter
  /** Render `StagedAttachmentsBinding` with this. It publishes the live staged list into the adapter. */
  stagedRef: MutableRefObject<readonly Attachment[]>
  /** Where a refused file's sentence goes. The mounted composer fills it — see `useRefusalSink`. */
  refusalRef: MutableRefObject<(message: string) => void>
}

export function useBoundAttachmentAdapter(): BoundAdapter {
  const stagedRef = useRef<readonly Attachment[]>([])
  // THE REFUSAL SINK IS A REF FOR THE SAME REASON THE STAGED LIST IS: `add` runs at pick time,
  // and the adapter is built once. A composer mounted under this provider registers its own
  // `onUrgent` here; until one does, a refusal has nowhere to go and is dropped rather than
  // thrown at the console.
  const refusalRef = useRef<(message: string) => void>(() => {})
  const adapter = useMemo(
    () =>
      createAttachmentAdapter({
        accept: ACCEPT_ATTR,
        staged: () => stagedRef.current,
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

/** Renders nothing. Its whole job is to run `useAuiState` inside the provider. */
export function StagedAttachmentsBinding({
  target,
}: {
  target: MutableRefObject<readonly Attachment[]>
}) {
  target.current = useAuiState((s) => s.composer.attachments)
  return null
}
