/**
 * MessageContent renders assistant text via Streamdown (react-markdown's replacement, A2) —
 * shared by ChatPage and BuilderPage (PR #112). Covers markdown rendering, the link-safety /
 * img-blocking XSS defenses, isStreaming/compact, and (below) Streamdown-specific coverage
 * upstream didn't need: code-block/Shiki chrome, and react-markdown's removal from package.json.
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

  it('renders user prose verbatim — never as markdown', () => {
    render(<MessageContent parts={textPart('**not bold**')} isUser />)
    expect(screen.getByText('**not bold**')).toBeTruthy()
    expect(document.querySelector('strong')).toBeNull()
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

  it('an image in assistant markdown never renders an <img> (zero-click GET prevention)', () => {
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

  it('isStreaming renders plain text, never markdown, even for an assistant message', () => {
    const { container } = render(<MessageContent parts={textPart('**bold**')} isStreaming />)
    expect(container.querySelector('strong')).toBeNull()
    expect(screen.getByText('**bold**')).toBeTruthy()
  })

  it('compact appends the smaller prose typography classes', () => {
    const { container } = render(<MessageContent parts={textPart('hello')} compact />)
    const wrapper = container.querySelector('.prose')
    expect(wrapper?.className).toContain('prose-p:text-xs')
  })

  it('raw HTML in assistant text is escaped, not rendered as an element', () => {
    const { container } = render(
      <MessageContent parts={textPart('<script>alert(1)</script>')} />,
    )
    expect(container.querySelector('script')).toBeNull()
  })

  it('renders a fenced code block through Streamdown, with its highlighter chrome (copy button)', () => {
    const text = ['```js', 'const x = 1', '```'].join('\n')
    const { container } = render(<MessageContent parts={textPart(text)} />)
    expect(screen.getByText(/const x = 1/)).toBeTruthy()
    // A bare react-markdown passthrough would just be <pre><code> — no Streamdown
    // chrome (language header, copy/download buttons) at all.
    const block = container.querySelector('[data-streamdown="code-block"]')
    expect(block).toBeTruthy()
    expect(block?.getAttribute('data-language')).toBe('js')
    expect(container.querySelector('[data-streamdown="code-block-copy-button"]')).toBeTruthy()
  })

  it('drops react-markdown from the dependency list — streamdown is its only replacement', () => {
    const pkg = JSON.parse(readFileSync(packageJsonPath, 'utf-8'))
    expect(pkg.dependencies['react-markdown']).toBeUndefined()
    expect(pkg.dependencies.streamdown).toBeTruthy()
  })
})
