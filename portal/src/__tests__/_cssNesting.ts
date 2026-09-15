/**
 * Brace depth at an offset in a stylesheet — shared, because two guards need the same answer.
 *
 * Both `reducedMotion.test.ts` and `scrollbars.test.ts` assert that their rule sits OUTSIDE every
 * `@layer`, and both learned it the same way: count the braces opened and not yet closed before
 * the rule starts. Anything but zero means the rule is nested, which in this stylesheet means
 * Tailwind's own utilities win and the rule silently does nothing.
 *
 * Callers pass a COMMENT-STRIPPED source (`_stripComments.ts`). A brace inside a comment counts
 * here exactly like a real one, and both files' prose contains braces.
 */
export function nestingDepthAt(source: string, offset: number): number {
  let depth = 0
  for (const c of source.slice(0, offset)) {
    if (c === '{') depth += 1
    else if (c === '}') depth -= 1
  }
  return depth
}
