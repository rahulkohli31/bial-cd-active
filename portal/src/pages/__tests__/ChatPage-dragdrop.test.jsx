/**
 * A2: drag-and-drop is net-new for the home chat's composer, added alongside the
 * assistant-ui migration. It must go through the exact same validation as the paperclip
 * picker (usePendingAttachments' shared `handleFiles`) — same accept/reject rules, same
 * toast wording, same preview row. These tests exercise the drop path specifically;
 * ChatPage-sendpath.test.jsx already covers the picker path and the send path itself.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, createEvent, cleanup, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

const h = vi.hoisted(() => ({
  sendMessage: vi.fn(),
  abort: vi.fn(),
  loadHistory: vi.fn(),
  newConversation: vi.fn(),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  deleteConversation: vi.fn(),
  listProjectConversations: vi.fn(),
}))

vi.mock('../../hooks/useClaudeAPI', () => ({
  useClaudeAPI: () => ({ sendMessage: h.sendMessage, error: null, clearError: vi.fn(), abort: h.abort }),
  getContextLimits: () => ({ soft: 1e9, hard: 1e9 }),
  estimateConversationTokens: () => 0,
}))
vi.mock('../../utils/chatHistory', () => ({
  loadHistory: h.loadHistory,
  newConversation: h.newConversation,
  createConversation: h.createConversation,
  getConversation: h.getConversation,
  deleteConversation: h.deleteConversation,
  relativeTime: () => 'now',
  deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/chat/MessageContent', () => ({ default: () => null }))
vi.mock('../../utils/conversationApi', () => ({
  listProjectConversations: h.listProjectConversations,
  uuidv7: () => '01900000-0000-7000-8000-000000000000',
}))

import ChatPage from '../ChatPage'

function renderChat(entry) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/chat/:chatId" element={<ChatPage projectId="p1" projectName="VIP Movement" />} />
        <Route path="/projects/:projectId" element={<div>project home</div>} />
        <Route path="/projects" element={<div>projects index</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

// A real file drop carries `types` alongside `files` — the composer gates EVERY drag handler
// on `types` (so a non-file drag is left to the browser entirely), so omitting it here would
// test a shape the browser never actually produces.
function dropFiles(target, files) {
  fireEvent.drop(target, { dataTransfer: { types: ['Files'], files } })
}

// dragenter/dragleave carry no file LIST (the spec withholds it until drop) — only `types`,
// which is what the composer's own guard reads to tell a file drag from a text selection drag.
const FILE_DRAG = { dataTransfer: { types: ['Files'] } }
const TEXT_DRAG = { dataTransfer: { types: ['text/plain'] } }

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  h.loadHistory.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.getConversation.mockResolvedValue(null)
  h.createConversation.mockResolvedValue({ id: 'chat-1', kind: 'planning', mode: 'plan' })
})
afterEach(() => cleanup())

describe('ChatPage — composer drag-and-drop (A2)', () => {
  it('accepts a valid dropped file — same preview row as the picker', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    const file = new File(['x'.repeat(100)], 'photo.png', { type: 'image/png' })
    dropFiles(composer, [file])

    expect(await screen.findByText('photo.png')).toBeTruthy()
  })

  it('rejects an oversized dropped file with the same toast as the picker', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    // MAX_FILE_SIZE is 4 MB for binary attachments (attachmentInput.ts) — one byte over.
    const big = new File([new Uint8Array(4 * 1024 * 1024 + 1)], 'huge.png', { type: 'image/png' })
    dropFiles(composer, [big])

    expect(await screen.findByText(/exceeds the 4 MB limit/i)).toBeTruthy()
    expect(screen.queryByText('huge.png')).toBeNull()
  })

  it('rejects a disallowed dropped file type with the same toast as the picker', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    const file = new File(['x'], 'archive.zip', { type: 'application/zip' })
    dropFiles(composer, [file])

    expect(await screen.findByText(/isn't supported/i)).toBeTruthy()
    expect(screen.queryByText('archive.zip')).toBeNull()
  })
})

/**
 * The drop TARGET has to be visible while a drag is in flight. Without it the composer
 * advertises "drop them anywhere in the composer" (the attach button's title) but shows
 * nothing that says where — and a drop that lands just outside the form falls through to
 * the browser, which navigates the tab to the file. That reads as a broken feature rather
 * than a missed target, which is exactly how it was first reported.
 */
describe('ChatPage — drop-target feedback', () => {
  it('marks the composer while a file drag is over it, and clears on leave', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    expect(composer.getAttribute('data-dragging')).toBeNull()
    fireEvent.dragEnter(composer, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBe('true')
    fireEvent.dragLeave(composer, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBeNull()
  })

  it('stays marked while the pointer crosses the composer\'s own children', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')
    // A REAL descendant, not the form again — the whole point of the depth count is that
    // children re-fire these events at the form via bubbling, so a test that only dispatches
    // on the form itself proves nothing about the case that motivated the counter.
    const textarea = screen.getByPlaceholderText(/Describe what you're thinking/i)
    expect(composer.contains(textarea)).toBe(true)

    // Entering a child fires the CHILD's dragenter before the parent's dragleave for the
    // element just left (both bubble to the form). A bare boolean would flicker off on that
    // leave; the depth count keeps it on until the leave that actually exits the form.
    fireEvent.dragEnter(composer, FILE_DRAG)
    fireEvent.dragEnter(textarea, FILE_DRAG)
    fireEvent.dragLeave(composer, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBe('true')

    fireEvent.dragLeave(textarea, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBeNull()
  })

  it('ignores a drag that carries no files (e.g. dragging selected text)', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    // Not just "nothing changed" — the enter is left entirely unclaimed (no preventDefault),
    // matching its sibling test below. An assert-absence-only version of this test (checking
    // just data-dragging) can't tell "correctly ignored" apart from "silently swallowed".
    const enter = createEvent.dragEnter(composer, TEXT_DRAG)
    fireEvent(composer, enter)
    expect(enter.defaultPrevented).toBe(false)
    expect(composer.getAttribute('data-dragging')).toBeNull()
  })

  it('a non-file dragLeave does not decrement a depth a file drag actually opened', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    // A real file drag opens the highlight...
    fireEvent.dragEnter(composer, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBe('true')
    // ...a non-file dragLeave crossing the same element (e.g. a stray text-selection drag
    // starting and immediately leaving while a file drag highlight is already up) must be
    // left alone by the SAME `carriesFiles` guard as onDragEnter/onDragOver — without it,
    // this would decrement the real file drag's depth and could close the highlight under a
    // drag that's still genuinely in progress.
    fireEvent.dragLeave(composer, TEXT_DRAG)
    expect(composer.getAttribute('data-dragging')).toBe('true')

    // The real file drag's own leave still works normally afterwards.
    fireEvent.dragLeave(composer, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBeNull()
  })

  it('a drop resets the highlight itself, not only via the window-level backstop', async () => {
    // Neuter the window-level drag/drop backstop (the Firefox-window-exit fallback) so this
    // test isolates the composer's OWN onDrop handler's `setDragDepth(0)` — otherwise the
    // backstop's `window.addEventListener('drop', clear)` would reset the highlight on its
    // own (via native bubbling to `window`, independent of React's delegated listeners on the
    // render container) and mask the composer handler's reset being absent entirely.
    const originalAdd = window.addEventListener.bind(window)
    const addSpy = vi.spyOn(window, 'addEventListener').mockImplementation((type, listener, options) => {
      if (type === 'drop' || type === 'dragend' || type === 'dragover') return undefined
      return originalAdd(type, listener, options)
    })

    try {
      renderChat('/chat/chat-1?projectId=p1&kind=planning')
      const composer = await screen.findByTestId('composer')

      fireEvent.dragEnter(composer, FILE_DRAG)
      expect(composer.getAttribute('data-dragging')).toBe('true')

      dropFiles(composer, [new File(['x'.repeat(100)], 'photo.png', { type: 'image/png' })])

      expect(await screen.findByText('photo.png')).toBeTruthy()
      expect(composer.getAttribute('data-dragging')).toBeNull()
    } finally {
      addSpy.mockRestore()
    }
  })

  it('leaves a non-file drag to the browser entirely — never captures then silently drops it', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    // preventDefault on dragover is what claims an element AS a drop target. Withholding it
    // for a text/link drag is what hands the drag back to the browser's native handling; if
    // we prevented it anyway the drop would land on us and vanish into a no-op, which looks
    // exactly like the broken-feature bug the highlight was added to fix.
    const over = createEvent.dragOver(composer, TEXT_DRAG)
    fireEvent(composer, over)
    expect(over.defaultPrevented).toBe(false)

    const drop = createEvent.drop(composer, TEXT_DRAG)
    fireEvent(composer, drop)
    expect(drop.defaultPrevented).toBe(false)

    // A file drag, by contrast, IS claimed.
    const fileOver = createEvent.dragOver(composer, FILE_DRAG)
    fireEvent(composer, fileOver)
    expect(fileOver.defaultPrevented).toBe(true)
  })

  it('clears the mark immediately when a cancelled drag reports itself (Escape, drop elsewhere)', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    fireEvent.dragEnter(composer, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBe('true')
    fireEvent.dragEnd(window, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBeNull()

    fireEvent.dragEnter(composer, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBe('true')
    fireEvent.drop(window, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBeNull()
  })

  it('clears a stranded mark when the drag vanishes silently (Firefox window exit)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      renderChat('/chat/chat-1?projectId=p1&kind=planning')
      const composer = await screen.findByTestId('composer')

      fireEvent.dragEnter(composer, FILE_DRAG)
      expect(composer.getAttribute('data-dragging')).toBe('true')

      // A live drag keeps re-firing dragover (the spec's own 350ms loop), so the highlight
      // must survive a user simply holding still over the composer. Deliberately run this
      // WELL past the idle timeout in total (4 x 400ms = 1600ms > 1000ms): each dragover has
      // to actually RESET the timer, not merely coexist with it — a heartbeat that failed to
      // reset would expire here, and a shorter run would pass either way.
      for (let i = 0; i < 4; i++) {
        act(() => {
          vi.advanceTimersByTime(400)
        })
        fireEvent.dragOver(window, FILE_DRAG)
      }
      expect(composer.getAttribute('data-dragging')).toBe('true')

      // Firefox delivers NO dragleave (and no dragend, for an OS file drag) when the pointer
      // leaves the browser window — bug 656164. The dragover heartbeat simply stops, which is
      // the only signal there is; without this backstop the highlight would stay lit forever.
      act(() => {
        vi.advanceTimersByTime(1000)
      })
      expect(composer.getAttribute('data-dragging')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears the mark on drop — no matching dragleave ever arrives', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    fireEvent.dragEnter(composer, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBe('true')

    dropFiles(composer, [new File(['x'.repeat(100)], 'photo.png', { type: 'image/png' })])

    expect(await screen.findByText('photo.png')).toBeTruthy()
    expect(composer.getAttribute('data-dragging')).toBeNull()
  })

  // The attach button's title advertises "drop them anywhere in the composer" — the
  // pending-attachment chip row renders ABOVE ComposerPrimitive.Root's <form>, not inside it,
  // so a drop landing there (exactly where someone who just staged one file is looking, to
  // stage a second) has to be claimed by the SAME drag-drop wiring as the form itself. Before
  // this was fixed, only the form carried the handlers, so a drop on the chip row fell through
  // to the browser's default handler unclaimed — which navigates the tab to the file, losing
  // the typed draft, every staged attachment, and any in-flight turn.
  it('claims a drop on the pending-attachment row too, not only the form beneath it', async () => {
    renderChat('/chat/chat-1?projectId=p1&kind=planning')
    const composer = await screen.findByTestId('composer')

    dropFiles(composer, [new File(['x'.repeat(100)], 'photo.png', { type: 'image/png' })])
    const chip = await screen.findByText('photo.png')
    const chipRow = chip.closest('div')
    // Confirms the row really is OUTSIDE the <form> — otherwise this test would pass even
    // without the fix, having accidentally exercised the form's own handlers instead.
    const form = composer.querySelector('form')
    expect(form).toBeTruthy()
    expect(form.contains(chipRow)).toBe(false)
    expect(composer.contains(chipRow)).toBe(true)

    const secondFile = new File(['y'.repeat(100)], 'second.png', { type: 'image/png' })
    const drop = createEvent.drop(chipRow, { dataTransfer: { types: ['Files'], files: [secondFile] } })
    fireEvent(chipRow, drop)

    expect(drop.defaultPrevented).toBe(true)
    expect(await screen.findByText('second.png')).toBeTruthy()
  })
})
