import ReactMarkdown from 'react-markdown'
import AttachmentChips from '../AttachmentChips'
import { partsToText, attachmentsFromParts } from '../../utils/attachmentStore'
import type { MessagePart } from '../../utils/messageTypes'

export interface MessageContentProps {
  parts: MessagePart[] | string
  isUser?: boolean
}

/**
 * Render one chat message bubble's inner content from the neutral `parts[]` model — the
 * ReactMarkdown variant. Its one consumer is ChatPage (the planning chat).
 *
 * BuilderPage deliberately keeps its own MessageContent: that one strips `jsx:preview` code
 * fences (they render in the live preview, not the transcript), which is different behaviour.
 *
 * `partsToText` yields the prose for display (text parts only); `attachmentsFromParts`
 * yields the attachment descriptors (file parts + inline-text attachments) rendered
 * as chips above the text. A plain string is still accepted defensively.
 */
export default function MessageContent({ parts, isUser }: MessageContentProps) {
  const text = partsToText(parts)
  // attachmentsFromParts only accepts the array form; its own Array.isArray guard
  // already returns [] for anything else, so the defensive string case is covered.
  const attachments = attachmentsFromParts(Array.isArray(parts) ? parts : [])
  return (
    <>
      {attachments.length > 0 && <AttachmentChips attachments={attachments} />}
      {isUser ? (
        <div className="whitespace-pre-wrap break-words">{text}</div>
      ) : (
        <div className="prose prose-sm max-w-none prose-p:my-1 prose-ul:my-1 prose-ol:my-1 prose-li:my-0.5 prose-strong:text-tertiary prose-ul:pl-4 prose-ol:pl-4">
          <ReactMarkdown>{text}</ReactMarkdown>
        </div>
      )}
    </>
  )
}
