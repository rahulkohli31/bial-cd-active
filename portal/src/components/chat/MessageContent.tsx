import { Streamdown, defaultRemarkPlugins } from 'streamdown'
import remarkBreaks from 'remark-breaks'
import type { AnchorHTMLAttributes, HTMLAttributes } from 'react'
import { partsToText } from '../../utils/attachmentStore'
import type { MessagePart } from '../../utils/messageTypes'

export interface MessageContentProps {
  parts: MessagePart[] | string
  /** True only while THIS message is the one actively streaming in. An incomplete
   *  markdown document re-parses per token — bold popping in when the closing `**`
   *  lands, an unterminated code fence rendering the growing tail as a code block —
   *  so a streaming message renders as plain text until it settles, then switches to
   *  the real markdown render. */
  isStreaming?: boolean
}

/** A link inside assistant markdown: an EXTERNAL (`http(s)://`) link opens in a new tab,
 *  never trusted to carry `window.opener`/referrer back to this app — the URL came from
 *  model output, which is prompt-injection reachable. A fragment-only or relative href does
 *  NOT get `target="_blank"`: opening either in a new tab scrolls nowhere and just opens a
 *  second tab at the current URL, racing a second reattach to the same build session.
 *
 *  Streamdown still computes its own default `target="_blank"`/`rel="noopener noreferrer"`
 *  for EVERY link and passes them as props — a component override swaps whose function runs,
 *  not what gets passed to it. Verified directly: NOT destructuring `target`/`rel` out below
 *  leaks Streamdown's `target="_blank"` onto internal links regardless of `external`, because
 *  the plain `{...props}` spread carries them in before the conditional spread runs. */
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
 * PROSE ONLY: `partsToText` yields text parts and nothing else — attachment chips are the
 * THREAD's to draw (`UserAttachments`, `ChatThread.tsx`), not this component's. BOTH SHAPES
 * ARE REAL: the live call site hands down a plain string; `MessageContent.test.tsx`'s parity
 * cases hand down `TextPart[]` — `partsToText`'s union is load-bearing, not defensive.
 *
 * `mode="static"` IS LOAD-BEARING. Streamdown's default `mode="streaming"` keeps "repairing"
 * text (closing an unterminated `**`, an unclosed fence/link) FOREVER, not just mid-stream,
 * silently corrupting settled content this platform renders routinely (`2**8` becomes `28`,
 * a glob loses a `*`). This component never hands Streamdown a still-arriving message —
 * `isStreaming` takes the plain-text branch instead — so `mode="static"` says explicitly
 * what the plain-text branch alone would only imply.
 *
 * SECURITY: `disallowedElements={['img', 'picture', 'source']}` blocks
 * `![](https://attacker.example/x)` and `<picture><source srcset="...">` — without it,
 * assistant markdown (model output, prompt-injection reachable) could fire a zero-click GET
 * to an arbitrary host the instant the bubble paints, leaking this user's IP/user-agent.
 * `picture`/`source` alone can't fetch (needs the `<img>` this blocklist removes) but are
 * stripped too as insurance.
 *
 * No `rehypePlugins` passed, so raw HTML goes through Streamdown's DEFAULT pipeline:
 * `rehype-raw` → `rehype-sanitize` (allowlist against `hast-util-sanitize`'s default schema)
 * → `rehype-harden` (drops `javascript:`/`data:`/`vbscript:` URLs and off-origin
 * image/link prefixes; its allowedProtocols/allowedImagePrefixes/allowDataImages stay at
 * Streamdown's wide-open defaults — no `security` prop to narrow them in this raw-package
 * build). NOT "raw HTML is escaped" (the old react-markdown model) — parse-then-allowlist,
 * a materially different guarantee: `<script>`/`<iframe>`/`<style>`/`on*` are stripped,
 * `<div>`/`<span>`/`<details>`/`<b>` pass through. `MessageContent.test.tsx` pins the
 * discriminating case against the actually-installed package, not assumed from docs.
 */
export default function MessageContent({ parts, isStreaming }: MessageContentProps) {
  const text = partsToText(parts)
  return isStreaming ? (
    <div className="whitespace-pre-wrap break-words">{text}</div>
  ) : (
    <div className="prose prose-sm max-w-none prose-p:my-1 prose-ul:my-1 prose-ol:my-1 prose-li:my-0.5 prose-strong:text-tertiary prose-ul:pl-4 prose-ol:pl-4">
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
  )
}
