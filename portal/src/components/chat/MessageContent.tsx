import { Streamdown } from 'streamdown'
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

/** A link inside assistant markdown: an EXTERNAL (`http(s)://`) link opens in a new tab,
 *  never trusted to carry `window.opener`/referrer/search-engine credit back to this app —
 *  the URL came from model output, which is prompt-injection reachable. A fragment-only
 *  href (`remark-gfm`'s footnote links, e.g. `#user-content-fn-1`) or a relative href the
 *  model emits does NOT get `target="_blank"`: opening either in a new tab doesn't scroll
 *  to anything, it just opens a second tab at the current URL — which in BuilderPage races
 *  a second reattach to the same build session. */
function MarkdownLink({
  node: _node,
  href,
  ...props
}: AnchorHTMLAttributes<HTMLAnchorElement> & { node?: unknown }) {
  const external = /^https?:/i.test(href ?? '')
  return (
    <a
      href={href}
      {...props}
      {...(external ? { target: '_blank', rel: 'noopener noreferrer nofollow ugc' } : {})}
    />
  )
}

/**
 * Render one chat message bubble's inner content from the neutral `parts[]` model — the
 * Streamdown variant, shared by ChatPage (the planning chat) and BuilderPage.
 *
 * `partsToText` yields the prose for display (text parts only); `attachmentsFromParts`
 * yields the attachment descriptors (file parts + inline-text attachments) rendered
 * as chips above the text. A plain string is still accepted defensively.
 *
 * SECURITY: `disallowedElements={['img']}` blocks `![](https://attacker.example/x)` —
 * without it, assistant markdown (model output, reachable via prompt injection through
 * user prompts/attachments/sandbox tool output) could fire a zero-click GET to an
 * arbitrary external host the instant the bubble paints, leaking this user's IP and
 * user-agent. Streamdown ships its own hardened sanitize pipeline (`rehype-sanitize` +
 * `rehype-harden`, bundled dependencies of `streamdown@2.5.0`) on top of the react-markdown-
 * compatible props below, so raw HTML and `javascript:`/`data:`/`vbscript:` URLs are
 * blanked by default — the "raw HTML is escaped" and "img never renders" cases are pinned
 * by MessageContent.test.tsx against the actually-installed package, not assumed from docs.
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
          <Streamdown
            remarkPlugins={[remarkGfm, remarkBreaks]}
            disallowedElements={['img']}
            unwrapDisallowed
            components={{
              a: MarkdownLink,
              // A GFM table has no intrinsic wrap; without this a wide one turns the whole
              // transcript column (which sits in an overflow-y-auto ancestor, computing
              // overflow-x to auto) horizontal-scrollable the moment the model emits one.
              table: (props) => (
                <div className="overflow-x-auto">
                  <table {...props} />
                </div>
              ),
            }}
          >
            {text}
          </Streamdown>
        </div>
      )}
    </>
  )
}
