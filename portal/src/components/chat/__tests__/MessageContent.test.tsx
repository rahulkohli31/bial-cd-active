/**
 * MessageContent renders assistant text via Streamdown (react-markdown's replacement) —
 * now hosted by the one conversation surface. Covers markdown rendering, the link-safety /
 * img-blocking XSS defenses, isStreaming, and (below) Streamdown-specific coverage upstream
 * didn't need: code-block/Shiki chrome, and react-markdown's removal from package.json.
 *
 * `isStreaming`'s exact rendering behaviour is a deliberately open decision — to be settled
 * with a measurement on a long reply rather than from principle. Do not firm it up without one.
 *
 * NOTHING HERE CAN ASK WHO WROTE THE MESSAGE, because the renderer is not told: one pipeline
 * serves every message. The citizen's own path is asserted where authorship exists —
 * `ChatThread.parity.test.tsx`, through the thread's user-message bubble.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { render, cleanup, screen } from '@testing-library/react'
import MessageContent from '../MessageContent'
import type { TextPart } from '../../../utils/messageTypes'

afterEach(cleanup)

const textPart = (text: string): TextPart[] => [{ type: 'text', text }]
const packageJsonPath = resolve(process.cwd(), 'package.json')

describe('MessageContent — markdown rendering', () => {
  it('renders assistant prose as markdown: bold and a list', () => {
    render(<MessageContent parts={textPart('**bold**\n- one\n- two')} />)
    expect(screen.getByText('bold').tagName).toBe('STRONG')
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
  })

  it('a build chat\'s first message, stored before this change, renders its heading and list on read — nothing is backfilled', () => {
    render(<MessageContent parts={textPart('# Welcome\n- first\n- second')} />)
    expect(screen.getByRole('heading', { level: 1, name: 'Welcome' })).toBeTruthy()
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
  })

  it('a single newline still renders as two visual lines (remark-breaks)', () => {
    const { container } = render(<MessageContent parts={textPart('line one\nline two')} />)
    // CommonMark alone would collapse this into one run-on paragraph with a space —
    // remark-breaks turns the bare newline into a <br>, which this pins.
    expect(container.querySelector('br')).toBeTruthy()
    expect(container.textContent).toContain('line one')
    expect(container.textContent).toContain('line two')
  })

  it('a blank-line-separated GFM table renders as a real table (remark-gfm)', () => {
    const { container } = render(
      <MessageContent parts={textPart('| a | b |\n| - | - |\n| 1 | 2 |')} />,
    )
    expect(container.querySelector('table')).toBeTruthy()
  })

  it('a table is wrapped in an overflow-x-auto container — a wide table must not scroll the whole transcript', () => {
    const { container } = render(
      <MessageContent parts={textPart('| a | b |\n| - | - |\n| 1 | 2 |')} />,
    )
    const table = container.querySelector('table')
    expect(table?.parentElement?.className).toContain('overflow-x-auto')
  })

  it('a fenced code block pasted by a citizen stays within the bubble\'s width — Streamdown wraps its body in overflow-x-auto', () => {
    const text = ['```js', 'const line = "long enough to need a horizontal scroll, not a bubble overflow"', '```'].join('\n')
    const { container } = render(<MessageContent parts={textPart(text)} />)
    const body = container.querySelector('[data-streamdown="code-block-body"]')
    expect(body).toBeTruthy()
    expect(body?.className).toContain('overflow-x-auto')
  })

  it('an image in any message never renders an <img> (zero-click GET prevention)', () => {
    const { container } = render(
      <MessageContent parts={textPart('before ![alt text](https://attacker.example/x.png) after')} />,
    )
    expect(container.querySelector('img')).toBeNull()
    // unwrapDisallowed: the surrounding prose survives, only the element is dropped.
    expect(container.textContent).toContain('before')
    expect(container.textContent).toContain('after')
  })

  it('an https link renders with target=_blank and a safe rel', () => {
    const { container } = render(
      <MessageContent parts={textPart('[click here](https://example.com/page)')} />,
    )
    const link = container.querySelector('a')
    expect(link).toBeTruthy()
    expect(link?.getAttribute('target')).toBe('_blank')
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer nofollow ugc')
  })

  it('a fragment-only footnote link gets no target/rel — target="_blank" would open a new tab at the current URL, not scroll', () => {
    const { container } = render(
      <MessageContent parts={textPart('see the note[^1]\n\n[^1]: detail')} />,
    )
    const links = [...container.querySelectorAll('a')]
    const fragmentLink = links.find((a) => (a.getAttribute('href') || '').startsWith('#'))
    expect(fragmentLink).toBeTruthy()
    expect(fragmentLink?.getAttribute('target')).toBeNull()
    expect(fragmentLink?.getAttribute('rel')).toBeNull()
  })

  it('a relative link gets no target/rel — a new tab at the same URL would race a BuilderPage session reattach', () => {
    const { container } = render(
      <MessageContent parts={textPart('[the project page](/projects)')} />,
    )
    const link = container.querySelector('a')
    expect(link?.getAttribute('href')).toBe('/projects')
    expect(link?.getAttribute('target')).toBeNull()
    expect(link?.getAttribute('rel')).toBeNull()
  })

  it('isStreaming renders plain text, never markdown — the branch is author-blind, so one case covers a still-arriving message from either author', () => {
    const { container } = render(<MessageContent parts={textPart('**bold**')} isStreaming />)
    expect(container.querySelector('strong')).toBeNull()
    expect(screen.getByText('**bold**')).toBeTruthy()
  })

  it('raw HTML in assistant text is escaped, not rendered as an element', () => {
    const { container } = render(
      <MessageContent parts={textPart('<script>alert(1)</script>')} />,
    )
    expect(container.querySelector('script')).toBeNull()
  })

  // Streamdown's pipeline parses raw HTML into real elements and allowlist-filters them, unlike
  // the "raw HTML is escaped" framing above — `querySelector('script') === null` can't tell the
  // two models apart, since script is stripped by both. This pins the behaviour that does.
  it('an allowlisted raw HTML tag renders as a real element, a disallowed one does not', () => {
    const { container } = render(
      <MessageContent parts={textPart('<details><summary>s</summary>body</details><iframe src="https://evil.example"></iframe>')} />,
    )
    expect(container.querySelector('details')).toBeTruthy()
    expect(container.querySelector('summary')).toBeTruthy()
    expect(container.querySelector('iframe')).toBeNull()
  })

  it('an HTML <picture><source> cannot smuggle a fetch back in once <img> is blocked', () => {
    const { container } = render(
      <MessageContent parts={textPart('<picture><source srcset="https://attacker.example/x.png"></picture>')} />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('picture')).toBeNull()
    expect(container.querySelector('source')).toBeNull()
  })

  // mode="static" fixes settled-text corruption: Streamdown's default mode="streaming" +
  // parseIncompleteMarkdown=true keeps "repairing" text forever, not just while it arrives.
  // These pin the exact cases that were silently altered before the fix, verbatim.
  describe('settled (non-streaming) text renders verbatim — mode="static" stops the repair', () => {
    it('does not touch "**" inside ordinary prose (2**8, not 28)', () => {
      const { container } = render(<MessageContent parts={textPart('Use 2**8 to get 256')} />)
      expect(container.textContent).toContain('2**8')
    })

    it('does not drop a "*" from a glob pattern (**/*.tsx)', () => {
      const { container } = render(<MessageContent parts={textPart('Match files with **/*.tsx')} />)
      expect(container.textContent).toContain('**/*.tsx')
    })

    it('does not swallow an unclosed inline code backtick', () => {
      const { container } = render(<MessageContent parts={textPart('A snippet: `const x = 1 and more text')} />)
      expect(container.textContent).toContain('`const x = 1 and more text')
    })

    it('does not delete the trailing clause after an unclosed link', () => {
      const { container } = render(
        <MessageContent parts={textPart('Visit [docs](https://example.com and read the rest')} />,
      )
      expect(container.textContent).toContain('and read the rest')
    })
  })

  it('renders a fenced code block through Streamdown, with its language header and copy/download chrome', () => {
    const text = ['```js', 'const x = 1', '```'].join('\n')
    const { container } = render(<MessageContent parts={textPart(text)} />)
    expect(screen.getByText(/const x = 1/)).toBeTruthy()
    // Chrome only, no highlighting: `@streamdown/code`/Shiki are opt-in plugin packages the raw
    // `streamdown` import here doesn't pull in — just the language label and control buttons.
    const block = container.querySelector('[data-streamdown="code-block"]')
    expect(block).toBeTruthy()
    expect(block?.getAttribute('data-language')).toBe('js')
    expect(container.querySelector('[data-streamdown="code-block-copy-button"]')).toBeTruthy()
  })

  it('a table renders with no copy/download controls — an unescaped CSV/TSV export would be a formula-injection sink for model-authored cells', () => {
    render(<MessageContent parts={textPart('| a | b |\n| - | - |\n| =1+1 | 2 |')} />)
    expect(screen.getByRole('table')).toBeTruthy()
    expect(screen.queryByTitle(/Copy table/i)).toBeNull()
    expect(screen.queryByTitle(/Download table/i)).toBeNull()
  })

  it('drops react-markdown from the dependency list — streamdown is its only replacement', () => {
    const pkg = JSON.parse(readFileSync(packageJsonPath, 'utf-8'))
    expect(pkg.dependencies['react-markdown']).toBeUndefined()
    expect(pkg.dependencies.streamdown).toBeTruthy()
  })
})
