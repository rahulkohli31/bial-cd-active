import ReactMarkdown from 'react-markdown'
import AttachmentChips from '../AttachmentChips'
import { partsToText, attachmentsFromParts } from '../../utils/attachmentStore'

/**
 * Render one chat message bubble's inner content from the neutral `parts[]`
 * model. Shared by ChatPage (planning chat) and the builder transcript (general
 * assistant) — the ReactMarkdown variant. (BuilderPage keeps its own
 * MessageContent: it strips jsx:preview code fences, a different behaviour, so it
 * is NOT a consumer of this module.)
 *
 * `partsToText` yields the prose for display (text parts only); `attachmentsFromParts`
 * yields the attachment descriptors (file parts + inline-text attachments) rendered
 * as chips above the text. A plain string is still accepted defensively.
 */
export default function MessageContent({ parts, isUser }) {
  const text = partsToText(parts)
  const attachments = attachmentsFromParts(parts)
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
