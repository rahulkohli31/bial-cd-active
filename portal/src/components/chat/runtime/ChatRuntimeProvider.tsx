/**
 * THE RUNTIME, MOUNTED ONCE PER CONVERSATION, AROUND EVERYTHING THAT READS IT.
 *
 * It used to be built inside `ChatThread`, wrapping only the transcript — fine while the
 * composer was hand-rolled and read nothing from the runtime. It stopped being fine once the
 * composer became the library's: every primitive resolves against `useAui()`, so the input,
 * attachment control, chips and dropzone all need the same provider as the thread.
 *
 * Hoisting it rather than adding a second one is the point: two runtimes would give the screen
 * two composer states and two capability maps, and the one the citizen typed into would not be
 * the one the transcript belonged to.
 */
import type { ReactNode } from 'react'
import { AssistantRuntimeProvider, type AppendMessage } from '@assistant-ui/react'
import { useChatRuntime } from './useChatRuntime'
import { AttachmentAdapterProviders, useBoundAttachmentAdapter } from './stagedAttachments'
import { BOTH_ATTACHMENT_LANES, type AttachmentLanes } from '../../../utils/attachmentInput'
import type { ChatMessage } from '../../../utils/messageTypes'

export interface ChatRuntimeProviderProps {
  /** The server-owned transcript. Live assembly and reload projection produce the same shape. */
  messages: readonly ChatMessage[]
  isRunning: boolean
  onNew: (message: AppendMessage) => Promise<void>
  /** The relocated stop control, as the runtime sees it. Passing it is what registers `cancel`. */
  onCancel: () => Promise<void>
  /**
   * WHICH ATTACHMENT LANES THIS CONVERSATION CAN HONOUR. Defaulted, because a chat with a
   * workspace can honour both and that is what a surface says by saying nothing; a surface with
   * no workspace passes `MODEL_LANE_ONLY` so the picker never offers a file its server refuses.
   */
  attachmentLanes?: AttachmentLanes
  children: ReactNode
}

export default function ChatRuntimeProvider({
  messages,
  isRunning,
  onNew,
  onCancel,
  attachmentLanes = BOTH_ATTACHMENT_LANES,
  children,
}: ChatRuntimeProviderProps) {
  const bound = useBoundAttachmentAdapter(attachmentLanes)
  const runtime = useChatRuntime({
    messages,
    isRunning,
    onNew,
    onCancel,
    attachments: bound.adapter,
  })
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {/* The pending-read count and the staged binding, mounted as one so no composer can take
          one without the other — see `AttachmentAdapterProviders`. */}
      <AttachmentAdapterProviders bound={bound}>{children}</AttachmentAdapterProviders>
    </AssistantRuntimeProvider>
  )
}
