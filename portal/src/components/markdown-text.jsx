"use client";

import { StreamdownTextPrimitive } from "@assistant-ui/react-streamdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import { defaultSchema } from "hast-util-sanitize";
import { harden } from "rehype-harden";
import { memo } from "react";

import { SyntaxHighlighter } from "@/components/shiki-highlighter";
import { cn } from "@/lib/utils";

// Streamdown renders raw HTML found in markdown by default ("allow all")
// with no built-in sanitization. Since this app streams AI-generated
// content, an unrestricted HTML passthrough is a real XSS vector (e.g. a
// stray <img onerror=...> or <script>). Lock it to a strict allowlist —
// only tags we actually want markdown to be able to express, nothing that
// can execute (no script/iframe/style/on* handlers) and no <img> at all.
//
// PR #35 comment 12: this used to be expressed via Streamdown's
// `allowedTags` prop, which never took effect (it only merges into the
// sanitize schema when `rehypePlugins` is untouched — passing our own
// `security` config always overrode it) — and wouldn't have achieved this
// anyway, since it's additive-only on top of hast-util-sanitize's default
// GitHub schema, which already permits <img>/<div>/<span>/<section>/etc.
// Also confirmed: rehypeRaw (which parses raw HTML into real elements) runs
// either way — Streamdown's own default pipeline includes it too — so a raw
// <img> written in the AI's text and a legitimate markdown ![]() image are
// indistinguishable by the time any tag schema applies. No <img> at all is
// the only way to close that off; this is a text-only planning assistant
// with no legitimate need to render images in its own replies.
const ALLOWED_TAG_NAMES = [
  "p", "strong", "em", "del", "code", "pre", "ul", "ol", "li", "a",
  "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead",
  "tbody", "tr", "th", "td", "br", "hr", "sup", "sub",
];

// Extends hast-util-sanitize's own default schema rather than hand-rolling
// attributes from scratch — inherits its per-tag nuances for every tag kept
// above, notably code's `className` matching /^language-./ (Shiki/
// SyntaxHighlighter reads this for language detection — see
// @assistant-ui/react-streamdown's code-adapter.js) and a's GFM-footnote/
// aria attributes (remarkGfm footnotes are enabled below). Only removes the
// rules for tags we're excluding (img, div, span, section, input, ...).
const SANITIZE_SCHEMA = {
  ...defaultSchema,
  tagNames: ALLOWED_TAG_NAMES,
  attributes: Object.fromEntries(
    Object.entries(defaultSchema.attributes ?? {}).filter(([tag]) => ALLOWED_TAG_NAMES.includes(tag)),
  ),
  protocols: { href: ["http", "https", "mailto"] }, // defense in depth alongside harden's own protocol check below
};

// Passed directly as rehypePlugins (bypassing Streamdown's `security`/
// `allowedTags` convenience props entirely, per the above) so this exact
// pipeline is what actually runs, not merged/overridden by anything else.
const REHYPE_PLUGINS = [
  rehypeRaw,
  [rehypeSanitize, SANITIZE_SCHEMA],
  [harden, {
    allowedProtocols: ["http", "https", "mailto"],
    allowDataImages: true, // moot once img is excluded above; kept as a documented, defense-in-depth value
    allowedImagePrefixes: [], // ditto — explicit rather than left unset
    // rehype-harden's OWN default (called directly here, not through
    // Streamdown's security-prop wrapper, which has its own `?? ["*"]`
    // fallback we don't get for free) is an EMPTY array — i.e. block every
    // link unless explicitly allowed. Deliberately wildcarded here: unlike
    // an auto-loaded image (fires with zero user interaction — the actual
    // exfiltration vector this fix closes), a link requires a user to click
    // it, and this assistant has a legitimate need to reference arbitrary
    // external URLs in its replies. allowedProtocols already blocks
    // javascript:/data: link targets.
    allowedLinkPrefixes: ["*"],
  }],
];

const MarkdownTextImpl = () => {
  return (
    <StreamdownTextPrimitive
      remarkPlugins={[remarkGfm]}
      rehypePlugins={REHYPE_PLUGINS}
      className="aui-md"
      components={defaultComponents}
      defer
    />
  );
};

export const MarkdownText = memo(MarkdownTextImpl);

const defaultComponents = {
  h1: ({ className, ...props }) => (
    <h1
      className={cn(
        "aui-md-h1 mt-5 mb-2 scroll-m-20 text-xl font-semibold first:mt-0 last:mb-0",
        className
      )}
      {...props} />
  ),
  h2: ({ className, ...props }) => (
    <h2
      className={cn(
        "aui-md-h2 mt-5 mb-2 scroll-m-20 text-lg font-semibold first:mt-0 last:mb-0",
        className
      )}
      {...props} />
  ),
  h3: ({ className, ...props }) => (
    <h3
      className={cn(
        "aui-md-h3 mt-4 mb-1.5 scroll-m-20 text-base font-semibold first:mt-0 last:mb-0",
        className
      )}
      {...props} />
  ),
  h4: ({ className, ...props }) => (
    <h4
      className={cn(
        "aui-md-h4 mt-3.5 mb-1 scroll-m-20 text-base font-medium first:mt-0 last:mb-0",
        className
      )}
      {...props} />
  ),
  h5: ({ className, ...props }) => (
    <h5
      className={cn(
        "aui-md-h5 mt-3 mb-1 text-sm font-semibold first:mt-0 last:mb-0",
        className
      )}
      {...props} />
  ),
  h6: ({ className, ...props }) => (
    <h6
      className={cn("aui-md-h6 mt-3 mb-1 text-sm font-medium first:mt-0 last:mb-0", className)}
      {...props} />
  ),
  p: ({ className, ...props }) => (
    <p
      className={cn("aui-md-p my-3 leading-relaxed first:mt-0 last:mb-0", className)}
      {...props} />
  ),
  a: ({ className, ...props }) => (
    <a
      className={cn(
        "aui-md-a text-tertiary hover:text-tertiary underline underline-offset-2",
        className
      )}
      {...props} />
  ),
  blockquote: ({ className, ...props }) => (
    <blockquote
      className={cn(
        "aui-md-blockquote border-neutral text-neutral my-3 border-s-2 ps-4",
        className
      )}
      {...props} />
  ),
  ul: ({ className, ...props }) => (
    <ul
      className={cn(
        "aui-md-ul marker:text-neutral my-3 ms-5 list-disc [&>li]:mt-1",
        className
      )}
      {...props} />
  ),
  ol: ({ className, ...props }) => (
    <ol
      className={cn(
        "aui-md-ol marker:text-neutral my-3 ms-5 list-decimal [&>li]:mt-1",
        className
      )}
      {...props} />
  ),
  hr: ({ className, ...props }) => (
    <hr className={cn("aui-md-hr border-neutral my-3", className)} {...props} />
  ),
  table: ({ className, ...props }) => (
    <table
      className={cn(
        "aui-md-table my-3 w-full border-separate border-spacing-0 overflow-y-auto",
        className
      )}
      {...props} />
  ),
  th: ({ className, ...props }) => (
    <th
      className={cn(
        "aui-md-th bg-white px-3 py-1.5 text-start font-medium first:rounded-ss-lg last:rounded-se-lg [[align=center]]:text-center [[align=right]]:text-right",
        className
      )}
      {...props} />
  ),
  td: ({ className, ...props }) => (
    <td
      className={cn(
        "aui-md-td border-neutral border-s border-b px-3 py-1.5 text-start last:border-e [[align=center]]:text-center [[align=right]]:text-right",
        className
      )}
      {...props} />
  ),
  tr: ({ className, ...props }) => (
    <tr
      className={cn(
        "aui-md-tr m-0 border-b p-0 first:border-t [&:last-child>td:first-child]:rounded-es-lg [&:last-child>td:last-child]:rounded-ee-lg",
        className
      )}
      {...props} />
  ),
  li: ({ className, ...props }) => (
    <li className={cn("aui-md-li leading-relaxed", className)} {...props} />
  ),
  strong: ({ className, ...props }) => (
    <strong className={cn("aui-md-strong font-semibold", className)} {...props} />
  ),
  sup: ({ className, ...props }) => (
    <sup className={cn("aui-md-sup [&>a]:text-xs [&>a]:no-underline", className)} {...props} />
  ),
  pre: ({ className, ...props }) => (
    <pre
      className={cn(
        "aui-md-pre border-bial-border bg-white overflow-x-auto rounded-t-none rounded-b-xl border border-t-0 p-3.5 text-[13px] leading-relaxed",
        className
      )}
      {...props} />
  ),
  code: ({ className, ...props }) => (
    <code
      className={cn(
        "aui-md-inline-code bg-white rounded-md px-1.5 py-0.5 font-mono text-[0.85em]",
        className
      )}
      {...props} />
  ),
  SyntaxHighlighter,
};
