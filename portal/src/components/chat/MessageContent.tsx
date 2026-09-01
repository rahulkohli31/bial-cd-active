import { Streamdown, defaultRemarkPlugins } from 'streamdown'
import remarkBreaks from 'remark-breaks'
import type { AnchorHTMLAttributes, HTMLAttributes } from 'react'
import AttachmentChips from '../AttachmentChips'
import { partsToText, attachmentsFromParts } from '../../utils/attachmentStore'
import type { MessagePart } from '../../utils/messageTypes'

export interface MessageContentProps {
  parts: MessagePart[] | string
  isUser?: boolean
  /** Shrinks the assistant markdown's typography to match a narrow rail (the chat panel
   *  beside a live app pane) — `prose-sm`'s own font-size otherwise wins over the bubble's
   *  inherited `text-xs`, rendering assistant text visibly larger than user text in the same
   *  thread. Leave unset for a full-width surface, which `prose-sm` was sized for. */
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
 *  to anything, it just opens a second tab at the current URL — which races a second
 *  reattach to the same build session.
 *
 *  Streamdown still computes its own default `target="_blank"`/`rel="noopener noreferrer"`
 *  for EVERY link and passes them down as props to whichever component renders `a` — a
 *  component override swaps whose function runs, not what gets passed to it. Verified
 *  directly: rendering with `components={{ a: MarkdownLink }}` and NOT destructuring
 *  `target`/`rel` out below leaks Streamdown's `target="_blank"` onto internal/relative
 *  links regardless of `external`, because the plain `{...props}` spread carries them in
 *  before the conditional spread runs. Destructured out and discarded so the conditional
 *  spread is the only source of truth for both. */
function MarkdownLink({
  node: _node,
  href,
  target: _target,
  rel: _rel,
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

/** Streamdown's default `strong` rendering is an animated `<span data-streamdown="strong">`
 *  (part of its word-fade-in streaming effect), not a semantic `<strong>` — restoring the
 *  real element keeps `prose-strong:text-tertiary` (Tailwind Typography targets `strong`)
 *  and accessibility semantics unchanged from the react-markdown behaviour this replaced. */
function MarkdownStrong({ node: _node, ...props }: HTMLAttributes<HTMLElement> & { node?: unknown }) {
  return <strong {...props} />
}

/**
 * Render one chat message bubble's inner content from the neutral `parts[]` model — the
 * Streamdown variant, used by the one conversation surface for both chat kinds.
 *
 * `partsToText` yields the prose for display (text parts only); `attachmentsFromParts`
 * yields the attachment descriptors (file parts + inline-text attachments) rendered
 * as chips above the text. A plain string is still accepted defensively.
 *
 * `mode="static"` is load-bearing, not decorative: Streamdown's own default is
 * `mode="streaming"` with `parseIncompleteMarkdown` on, which keeps "repairing" the text
 * (closing an unterminated `**`, an unclosed code fence, an unclosed link) forever, not
 * just while a message is mid-stream — with no signal telling it a message has settled.
 * That silently corrupts ordinary settled content this platform renders routinely (`2**8`
 * becomes `28`, a glob like `**` + `/*.tsx` loses a `*`, an unclosed link drops its trailing
 * clause). This component never hands Streamdown a message that's still arriving in the first place —
 * `isStreaming` (below) takes the plain-text branch instead — so by the time text reaches
 * Streamdown it is always settled, and `mode="static"` says so explicitly rather than
 * relying on the plain-text branch alone to keep the repair from ever running.
 *
 * SECURITY: `disallowedElements={['img', 'picture', 'source']}` blocks
 * `![](https://attacker.example/x)` and an HTML `<picture><source srcset="...">` —
 * without it, assistant markdown (model output, reachable via prompt injection through
 * user prompts/attachments/sandbox tool output) could fire a zero-click GET to an
 * arbitrary external host the instant the bubble paints, leaking this user's IP and
 * user-agent. `picture`/`source` alone can't fetch anything (the browser's image-selection
 * algorithm needs the `<img>` this blocklist removes) but are stripped too as insurance
 * against that arming later.
 *
 * This component passes no `rehypePlugins`, so Streamdown's raw HTML goes through its own
 * DEFAULT pipeline: `rehype-raw` (parses raw HTML into real elements) → `rehype-sanitize`
 * (allowlist-filters them against `hast-util-sanitize`'s default schema) → `rehype-harden`
 * (drops `javascript:`/`data:`/`vbscript:` URLs and off-origin image/link prefixes, though
 * with `allowedProtocols`/`allowedImagePrefixes`/`allowDataImages` left at Streamdown's
 * wide-open defaults — this raw-package build has no `security` prop to narrow them). That
 * is NOT "raw HTML is escaped" (react-markdown's old model, which this replaced) — it is
 * parse-then-allowlist, a materially different guarantee. `<script>`/`<iframe>`/`<style>`/
 * `on*` handlers are stripped by the schema; `<div>`/`<span>`/`<details>`/`<b>` and similar
 * are allowlisted through as real elements. MessageContent.test.tsx pins the discriminating
 * case (an allowlisted tag renders, a disallowed one doesn't) against the actually-installed
 * package, not assumed from docs.
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
            mode="static"
            // Streamdown's own default remarkPlugins (Object.values(defaultRemarkPlugins))
            // include a codeMeta plugin (fenced-code `meta` parsing, e.g. `startLine=`) — this
            // array REPLACES rather than extends that default, so spreading it back in first
            // keeps codeMeta alongside the gfm/breaks this component actually needs.
            remarkPlugins={[...Object.values(defaultRemarkPlugins), remarkBreaks]}
            disallowedElements={['img', 'picture', 'source']}
            unwrapDisallowed
            // No table copy/download controls: Streamdown's default CSV/TSV export is
            // unescaped and BOM-prefixed, so Excel opens it as a real CSV — a model-authored
            // cell beginning =, +, - or @ becomes a live formula the instant the file opens.
            // Model output is prompt-injection reachable, so that export has to stay off
            // rather than trust every cell to never start with one of those characters.
            controls={{ table: false }}
            components={{
              a: MarkdownLink,
              strong: MarkdownStrong,
              // A GFM table has no intrinsic wrap; without this a wide one turns the whole
              // transcript column (which sits in an overflow-y-auto ancestor, computing
              // overflow-x to auto) horizontal-scrollable the moment the model emits one.
              table: ({ node: _node, ...props }) => (
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
