/**
 * A STAGED FILE IS A CHIP, NEVER PROSE — the guarantee the rebuilt transcript nearly lost.
 *
 * ══ WHY THIS FILE EXISTS ══
 *
 * `buildUserParts` encodes an attached csv/txt as `{ type:'text', text: <THE WHOLE FILE>,
 * attachment:{…} }`. The `text` field is the file's own bytes, not a word the citizen typed. Every
 * consumer that renders prose has therefore always filtered it — `partsToText` does it with an
 * explicit `!p.attachment` — and the chip is drawn from the descriptor instead.
 *
 * The converter did not carry that filter across. A text part became a text part regardless, so a
 * 4,000-row CSV rendered into the bubble verbatim; and `file` parts (image/PDF) converted to
 * `null`, so those attachments rendered as nothing at all. Both are reachable by attaching a file
 * and pressing Send — the ordinary path, not an edge case.
 *
 * ══ WHY IT ASSERTS AT BOTH LEVELS ══
 *
 * The converter is the guarantee; the DOM is only its symptom. A test that checked the bubble
 * alone would keep passing if someone later re-introduced the content into the prose stream but
 * happened to clip it visually.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react'
import type { FC } from 'react'

// An image chip fetches its own thumbnail on mount. That transport is `AttachmentChips`' business
// and has its own tests; here it would only add an unhandled rejection (jsdom has no base URL for a
// relative fetch) to a file about what the transcript renders. It resolves to a URL rather than
// `null` so the chip takes its ordinary path instead of the "attachment unavailable" one.
vi.mock('../../../utils/attachmentApi', () => ({
  fetchAttachmentObjectUrl: vi.fn(async () => 'blob:thumb'),
}))

import { Thread, type ThreadComponents } from '../../assistant-ui/thread'
import { convertMessage } from '../runtime/convertMessage'
import MessageContent from '../MessageContent'
import AttachmentChips from '../../AttachmentChips'
import type { ChatMessage } from '../../../utils/messageTypes'

afterEach(cleanup)

/** A real CSV body — the thing that must never reach the bubble. */
const CSV_BODY = 'id,name,salary\n1,Priya,240000\n2,Arun,310000'

const withCsv = (): ChatMessage => ({
  id: 'u1',
  role: 'user',
  parts: [
    { type: 'text', text: CSV_BODY, attachment: { attachmentId: 'att-1', name: 'payroll.csv', mediaType: 'text/csv', size: 51 } },
    { type: 'text', text: 'What is the total?' },
  ],
  seq: 1,
})

const withImage = (): ChatMessage => ({
  id: 'u2',
  role: 'user',
  parts: [
    { type: 'file', kind: 'image', attachmentId: 'att-2', key: 'k/2', name: 'floorplan.png', mediaType: 'image/png', size: 900 },
    { type: 'text', text: 'Use this layout.' },
  ],
  seq: 2,
})

const TextPart: ThreadComponents['TextPart'] = ({ text, isUser }) => (
  <MessageContent parts={text} isUser={isUser} />
)
const ToolGroup: ThreadComponents['ToolGroup'] = ({ children }) => <div>{children}</div>
const ToolPart: ThreadComponents['ToolPart'] = () => null

const ReasoningGroup: ThreadComponents['ReasoningGroup'] = () => null

const components: ThreadComponents = {
  TextPart,
  ToolGroup,
  ToolPart,
  ReasoningGroup,
  UserAttachments: AttachmentChips,
}

const Harness: FC<{ messages: ChatMessage[] }> = ({ messages }) => {
  const runtime = useExternalStoreRuntime<ChatMessage>({
    messages,
    isRunning: false,
    onNew: async () => undefined,
    convertMessage,
  })
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread components={components} />
    </AssistantRuntimeProvider>
  )
}

describe('the converter keeps file content out of the prose stream', () => {
  it('drops an attachment-bearing text part instead of passing its body through as text', () => {
    const converted = convertMessage(withCsv())
    const texts = (converted.content as { type: string; text?: string }[])
      .filter((p) => p.type === 'text')
      .map((p) => p.text)
    expect(texts).toEqual(['What is the total?'])
    // Stated separately from the equality above, so the failure message names the actual defect
    // rather than just showing two unequal arrays.
    expect(texts.join('')).not.toContain('240000')
  })

  it('carries the descriptors the chips are drawn from', () => {
    const attachments = convertMessage(withCsv()).metadata?.custom?.attachments
    expect(attachments).toEqual([
      { attachmentId: 'att-1', kind: 'text', name: 'payroll.csv', mediaType: 'text/csv' },
    ])
  })

  it('carries them for a `file` part too — the kind that converts to no library part at all', () => {
    const attachments = convertMessage(withImage()).metadata?.custom?.attachments
    expect(attachments).toEqual([
      { attachmentId: 'att-2', kind: 'image', name: 'floorplan.png', mediaType: 'image/png' },
    ])
  })

  it('adds no metadata to a message that carries no attachment', () => {
    const plain: ChatMessage = { id: 'u3', role: 'user', parts: [{ type: 'text', text: 'hello' }], seq: 3 }
    expect(convertMessage(plain).metadata).toBeUndefined()
  })
})

describe('the transcript draws the chip and not the file', () => {
  it('a staged CSV shows its NAME, and none of its contents', () => {
    render(<Harness messages={[withCsv()]} />)

    expect(screen.getByText('payroll.csv')).toBeTruthy()
    // The salary figure is the load-bearing assertion: it is in the file and nowhere else, so its
    // presence anywhere in the bubble means the body was rendered.
    const bubble = screen.getByTestId('user-message')
    expect(bubble.textContent).not.toContain('240000')
    expect(bubble.textContent).not.toContain('id,name,salary')
    // LIVENESS — the citizen's own question still renders, so the absences above are a filter
    // rather than a message that failed to draw.
    expect(bubble.textContent).toContain('What is the total?')
  })

  it('draws each chip ONCE, and draws it from the thread\'s own slot', () => {
    render(<Harness messages={[withCsv()]} />)
    // `getAllByText` with a length, not `getByText`, so a second chip fails as "2, expected 1"
    // rather than as a multiple-match throw that reads like a selector mistake. The slot is
    // `UserAttachments` (thread.tsx:307) and it is the user branch's alone — nothing in this
    // product puts an attachment on an assistant message.
    expect(screen.getAllByText('payroll.csv')).toHaveLength(1)
  })

  it('and MessageContent draws no chip of its own, given the array form that used to make it', () => {
    // THE HALF THE TRANSCRIPT ABOVE CANNOT SEE, and the one with teeth. `MessageContent` used to
    // render chips itself from the same descriptors; the case above would not notice it coming
    // back, because the thread's text slot hands down a plain STRING and the in-bubble render read
    // the array. The union is still accepted — the 21 parity cases pass arrays — so nothing in the
    // type system stops it, and this is what does.
    const { container } = render(<MessageContent parts={withCsv().parts} />)
    expect(container.textContent).not.toContain('payroll.csv')
    // LIVENESS — the prose still renders, so the absence above is a filter and not a dead render.
    expect(container.textContent).toContain('What is the total?')
  })

  it('an image attachment is visible at all — it converts to no library part', async () => {
    render(<Harness messages={[withImage()]} />)
    const bubble = screen.getByTestId('user-message')
    // An image chip names itself in `alt`, not in visible text — it IS the thumbnail.
    expect(await screen.findByAltText('floorplan.png')).toBeTruthy()
    expect(bubble.textContent).toContain('Use this layout.')
  })
})
