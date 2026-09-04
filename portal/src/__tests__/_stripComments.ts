/**
 * Strip comments before a source-scanning rule reads a file — shared, because the subtlety is.
 *
 * TWO GUARDS NEED THE SAME `://` EXEMPTION AND FOR THE SAME REASON. `//` only opens a comment
 * when it is not part of a URL scheme, and both callers read files that carry one:
 * `index.css` opens on a font `@import` URL, and `tailwind.config.js` and the primitives are
 * full of https links in prose. A copy of this regex that lost the `[^:]` guard would silently
 * eat the rest of every line holding a link, and the rule reading the result would go quiet
 * rather than red — so the two callers share one definition instead of two that can diverge.
 *
 * COMMENTS ARE NOT SOURCE, and in both callers that distinction has teeth rather than being
 * tidiness: `ui/pagination.tsx`'s docblock QUOTES the v4 tokens it deliberately avoided, and
 * `dialog.tsx`, `popover.tsx` and `button.tsx` each NAME the identifiers they dropped so the
 * next re-copy is recognised. Reporting those sentences would push someone to delete the one
 * comment in the tree that explains the trap.
 */
export function stripComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}
