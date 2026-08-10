import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup, screen } from '@testing-library/react'
import MessageContent from '../MessageContent'
import type { TextPart } from '../../../utils/messageTypes'

afterEach(cleanup)

const textPart = (text: string): TextPart[] => [{ type: 'text', text }]

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

  it('an image in assistant markdown never renders an <img> (zero-click GET prevention)', () => {
    const { container } = render(
      <MessageContent parts={textPart('before ![alt text](https://attacker.example/x.png) after')} />,
    )
    expect(container.querySelector('img')).toBeNull()
    // unwrapDisallowed: the surrounding prose survives, only the element is dropped.
    expect(container.textContent).toContain('before')
    expect(container.textContent).toContain('after')
  })

  it('a link renders with target=_blank and a safe rel', () => {
    const { container } = render(
      <MessageContent parts={textPart('[click here](https://example.com/page)')} />,
    )
    const link = container.querySelector('a')
    expect(link).toBeTruthy()
    expect(link?.getAttribute('target')).toBe('_blank')
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer nofollow ugc')
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
})
