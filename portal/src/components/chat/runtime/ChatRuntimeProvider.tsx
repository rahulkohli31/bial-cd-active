/**
 * THE RUNTIME, MOUNTED ONCE PER CONVERSATION, AROUND EVERYTHING THAT READS IT (plan 002, U5).
 *
 * It used to be built inside `ChatThread`, whose provider therefore wrapped only the transcript.
 * That was fine while the composer was entirely hand-rolled — it read nothing from the runtime. It
 * stops being fine the moment the composer is the library's: every composer primitive resolves
 * against `useAui()`, so the input, the attachment control, the chips and the dropzone all have to
 * be inside the same provider as the thread.
 *
 * Hoisting it rather than adding a second one is the whole point: two runtimes would give the
 * screen two composer states and two capability maps, and the one the citizen typed into would not
 * be the one the transcript belonged to.
 */
import type { ReactNode } from 'react'
import { AssistantRuntimeProvider, type AppendMessage } from '@assistant-ui/react'
import { useChatRuntime } from './useChatRuntime'
import { RefusalSinkProvider, StagedAttachmentsBinding, useBoundAttachmentAdapter } from './stagedAttachments'
import type { ChatMessage } from '../../../utils/messageTypes'

export interface ChatRuntimeProviderProps {
  /** The server-owned transcript. Live assembly and reload projection produce the same shape. */
  messages: readonly ChatMessage[]
  isRunning: boolean
  onNew: (message: AppendMessage) => Promise<void>
  /** R55's relocated stop, as the runtime sees it. Passing it is what registers `cancel`. */
  onCancel: () => Promise<void>
  children: ReactNode
}

export default function ChatRuntimeProvider({
  messages,
  isRunning,
  onNew,
  onCancel,
  children,
}: ChatRuntimeProviderProps) {
  const { adapter, stagedRef, refusalRef } = useBoundAttachmentAdapter()
  const runtime = useChatRuntime({ messages, isRunning, onNew, onCancel, attachments: adapter })
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RefusalSinkProvider value={refusalRef}>
        <StagedAttachmentsBinding target={stagedRef} />
        {children}
      </RefusalSinkProvider>
    </AssistantRuntimeProvider>
  )
}
