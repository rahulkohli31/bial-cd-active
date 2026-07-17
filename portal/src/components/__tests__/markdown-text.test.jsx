/**
 * Pins the sanitization behavior (PR #35 comment 5) against the REAL
 * pipeline markdown-text.jsx builds — REHYPE_PLUGINS, exported for exactly
 * this purpose — not a parallel reimplementation that could drift and stay
 * green even if the real file regresses.
 *
 * Given comment 12 (the old allowedTags/security pair never actually ran),
 * this test matters doubly: today's real protection is whatever this exact
 * schema says, and a future dependency bump or edit here could silently
 * change it with nothing to catch it.
 *
 * Renders through a standalone unified pipeline (remark parse + gfm +
 * remark-rehype, matching StreamdownTextPrimitive's own defaults) rather
 * than the full assistant-ui component tree — MarkdownText itself reads
 * its text via assistant-ui's useMessagePartText() context, so exercising
 * the actual sanitization logic directly (REHYPE_PLUGINS) is both simpler
 * and a tighter test of what this comment is actually about.
 */
import { describe, it, expect } from 'vitest'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'
import remarkRehype from 'remark-rehype'
import { toHtml } from 'hast-util-to-html'

import { REHYPE_PLUGINS } from '../markdown-text'

function renderMarkdown(md) {
  const processor = unified().use(remarkParse).use(remarkGfm).use(remarkRehype, { allowDangerousHtml: true })
  for (const plugin of REHYPE_PLUGINS) {
    if (Array.isArray(plugin)) processor.use(...plugin)
    else processor.use(plugin)
  }
  return toHtml(processor.runSync(processor.parse(md)))
}

describe('markdown-text sanitization (PR #35 comments 5 + 12)', () => {
  it('strips a raw <script> tag', () => {
    expect(renderMarkdown('<script>alert(1)</script>')).not.toContain('<script')
  })

  it('strips an onerror-bearing <img> tag', () => {
    const html = renderMarkdown('<img src=x onerror=alert(1)>')
    expect(html).not.toContain('onerror')
    expect(html).not.toContain('<img')
  })

  it('blocks a javascript: link', () => {
    const html = renderMarkdown('[x](javascript:alert(1))')
    expect(html).not.toContain('javascript:')
    expect(html).not.toContain('<a')
  })

  it('strips a raw <img> pointing at an external URL (the beacon-pixel vector from comment 12)', () => {
    const html = renderMarkdown('<img src="https://evil.example/beacon.png">')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('evil.example')
  })

  it('also blocks a legitimate-syntax markdown image (the deliberate no-images decision from comment 12)', () => {
    const html = renderMarkdown('![alt](https://evil.example/beacon.png)')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('evil.example')
  })

  it('still renders a normal external link', () => {
    const html = renderMarkdown('[hello](https://example.com)')
    expect(html).toContain('<a')
    expect(html).toContain('https://example.com')
  })

  it('still tags a fenced code block with its language class for Shiki', () => {
    const html = renderMarkdown('```js\nconsole.log(1)\n```')
    expect(html).toContain('language-js')
  })

  it('still renders GFM footnotes', () => {
    const html = renderMarkdown('Text[^1]\n\n[^1]: note')
    expect(html).toContain('data-footnote-ref')
  })
})
