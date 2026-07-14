"use client";

import { StreamdownTextPrimitive } from "@assistant-ui/react-streamdown";
import remarkGfm from "remark-gfm";
import { memo } from "react";

import { SyntaxHighlighter } from "@/components/shiki-highlighter";
import { cn } from "@/lib/utils";

// Streamdown renders raw HTML found in markdown by default ("allow all")
// with no built-in sanitization. Since this app streams AI-generated
// content, an unrestricted HTML passthrough is a real XSS vector (e.g. a
// stray <img onerror=...> or <script>). Lock it to a strict allowlist —
// only tags/attrs we actually want markdown to be able to express, nothing
// that can execute (no script/iframe/style/on* handlers, no raw <img> —
// markdown's own ![]() image syntax is a SEPARATE code path, unaffected).
const ALLOWED_TAGS = {
  p: [],
  strong: [],
  em: [],
  del: [],
  code: [],
  pre: [],
  ul: [],
  ol: [],
  li: [],
  a: ["href"],
  blockquote: [],
  h1: [],
  h2: [],
  h3: [],
  h4: [],
  h5: [],
  h6: [],
  table: [],
  thead: [],
  tbody: [],
  tr: [],
  th: [],
  td: [],
  br: [],
  hr: [],
  sup: [],
  sub: [],
};

// Separate hardening axis: restricts link/image URL protocols (blocks
// javascript: URLs etc.) via Streamdown's rehype-harden integration.
// Overrides Streamdown's own default "allow all" policy.
const SECURITY_CONFIG = {
  allowedProtocols: ["http", "https", "mailto"],
  allowDataImages: true, // inline base64 images from markdown ![]() syntax
};

const MarkdownTextImpl = () => {
  return (
    <StreamdownTextPrimitive
      remarkPlugins={[remarkGfm]}
      className="aui-md"
      components={defaultComponents}
      allowedTags={ALLOWED_TAGS}
      security={SECURITY_CONFIG}
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
