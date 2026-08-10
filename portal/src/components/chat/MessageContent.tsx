import ReactMarkdown from 'react-markdown'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'
import type { AnchorHTMLAttributes } from 'react'
import AttachmentChips from '../AttachmentChips'
import { partsToText, attachmentsFromParts } from '../../utils/attachmentStore'
import type { MessagePart } from '../../utils/messageTypes'

export interface MessageContentProps {
  parts: MessagePart[] | string
  isUser?: boolean
  /** Shrinks the assistant markdown's typography to match a narrow rail (BuilderPage) —
   *  `prose-sm`'s own font-size otherwise wins over the bubble's inherited `text-xs`,
   *  rendering assistant text visibly larger than user text in the same thread. Leave
   *  unset for a wider surface (ChatPage's planning chat), where `prose-sm` was sized for. */
  compact?: boolean
  /** True only while THIS message is the one actively streaming in. An incomplete
   *  markdown document re-parses per token — bold popping in when the closing `**`
   *  lands, an unterminated code fence rendering the growing tail as a code block —
   *  so a streaming assistant message renders as plain text (same treatment as the
   *  user branch) until it settles, then switches to the real markdown render. */
  isStreaming?: boolean
}

/** A link inside assistant markdown: opened in a new tab, and never trusted to
 *  carry `window.opener`/referrer/search-engine credit back to this app — the URL
 *  came from model output, which is prompt-injection reachable. */
function MarkdownLink({
  node: _node,
  ...props
}: AnchorHTMLAttributes<HTMLAnchorElement> & { node?: unknown }) {
  return <a {...props} target="_blank" rel="noopener noreferrer nofollow ugc" />
}

/**
 * Render one chat message bubble's inner content from the neutral `parts[]` model —
 * the ReactMarkdown variant, shared by ChatPage (the planning chat) and BuilderPage.
 *
 * `partsToText` yields the prose for display (text parts only); `attachmentsFromParts`
 * yields the attachment descriptors (file parts + inline-text attachments) rendered
 * as chips above the text. A plain string is still accepted defensively.
 *
 * SECURITY: `disallowedElements={['img']}` blocks `![](https://attacker.example/x)` —
 * without it, assistant markdown (model output, reachable via prompt injection through
 * user prompts/attachments/sandbox tool output) could fire a zero-click GET to an
 * arbitrary external host the instant the bubble paints, leaking this user's IP and
 * user-agent. Raw HTML is already escaped and `javascript:`/`data:`/`vbscript:` URLs
 * are already blanked by react-markdown's own `defaultUrlTransform` — verified against
 * the actually-installed `react-markdown@10.1.0`, not assumed from docs. Both of those
 * hold only as long as no `rehype-raw` plugin is ever added here — doing so would turn
 * this render path into a stored-XSS sink for sandbox-influenced model output.
 */
export default function MessageContent({ parts, isUser, compact, isStreaming }: MessageContentProps) {
  const text = partsToText(parts)
  // attachmentsFromParts only accepts the array form; its own Array.isArray guard
  // already returns [] for anything else, so the defensive string case is covered.
  const attachments = attachmentsFromParts(Array.isArray(parts) ? parts : [])
  const renderAsPlainText = isUser || isStreaming
  return (
    <>
      {attachments.length > 0 && <AttachmentChips attachments={attachments} />}
      {renderAsPlainText ? (
        <div className="whitespace-pre-wrap break-words">{text}</div>
      ) : (
        <div
          className={`prose prose-sm max-w-none prose-p:my-1 prose-ul:my-1 prose-ol:my-1 prose-li:my-0.5 prose-strong:text-tertiary prose-ul:pl-4 prose-ol:pl-4 ${
            compact
              ? 'prose-p:text-xs prose-li:text-xs prose-headings:text-xs prose-headings:font-semibold'
              : ''
          }`}
        >
          <ReactMarkdown
            remarkPlugins={[remarkGfm, remarkBreaks]}
            disallowedElements={['img']}
            unwrapDisallowed
            components={{ a: MarkdownLink }}
          >
            {text}
          </ReactMarkdown>
        </div>
      )}
    </>
  )
}
