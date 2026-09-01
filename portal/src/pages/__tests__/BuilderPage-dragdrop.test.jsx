/**
 * Drag-and-drop wiring on the conversation surface (`usePendingAttachments`, A2).
 *
 * THIS WAS ONE HALF OF A PAIR. Two pages shared the hook and only the planning one had
 * coverage, so this file was written to cover the other against a different child set — a
 * pending-attachment preview row sharing a wrapper with a banners block, a switcher and a
 * composer-gate note. There is ONE surface now and one child set; the sibling suite went with
 * its page. The coverage is worth keeping, but it is no longer "the other half" of anything.
 *
 * (The file name still says `BuilderPage`. Every suite in this directory named that way now
 * renders `ConversationSurface` — see its own docstring for why one file absorbed both pages.
 * Renaming seventeen files is a change of its own and has not been made.)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, createEvent, cleanup } from '@testing-library/react'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(), relaunchPreview: vi.fn(),
  acquireLock: vi.fn(), releaseLock: vi.fn(),
  notifyUsageChanged: vi.fn(),
}))

vi.mock('../../utils/usage', () => ({ notifyUsageChanged: h.notifyUsageChanged }))
vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({ ...(await orig()), buildUserParts: h.buildUserParts }))
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import { makeClient, primeClient, primeTurn, renderBuilder, waitForGateOpen } from './_builderSession.jsx'

const dragDeps = () => ({ deps: { client: makeClient(h), eventSourceFactory: () => ({ close: () => {} }) } })

function dropFiles(target, files) {
  fireEvent.drop(target, { dataTransfer: { types: ['Files'], files } })
}
const FILE_DRAG = { dataTransfer: { types: ['Files'] } }
const TEXT_DRAG = { dataTransfer: { types: ['text/plain'] } }

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  primeTurn(h)
  h.newBuild.mockReturnValue('build-Y')
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue({ id: 'build-X', kind: 'builder', mode: 'plan', messages: [] })
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (t) => [{ type: 'text', text: t }])
})
afterEach(() => cleanup())

async function renderReady() {
  const { deps } = dragDeps()
  renderBuilder({ deps })
  await waitForGateOpen()
  return screen.getByTestId('composer')
}

describe('BuilderPage — composer drag-and-drop', () => {
  it('accepts a valid dropped file — same preview row as the picker', async () => {
    const composer = await renderReady()
    const file = new File(['x'.repeat(100)], 'photo.png', { type: 'image/png' })
    dropFiles(composer, [file])
    expect(await screen.findByText('photo.png')).toBeTruthy()
  })

  it('rejects an oversized dropped file with the same toast as the picker', async () => {
    const composer = await renderReady()
    const big = new File([new Uint8Array(4 * 1024 * 1024 + 1)], 'huge.png', { type: 'image/png' })
    dropFiles(composer, [big])
    expect(await screen.findByText(/exceeds the 4 MB limit/i)).toBeTruthy()
    expect(screen.queryByText('huge.png')).toBeNull()
  })
})

describe('BuilderPage — drop-target feedback', () => {
  it('marks the composer while a file drag is over it, and clears on leave', async () => {
    const composer = await renderReady()
    expect(composer.getAttribute('data-dragging')).toBeNull()
    fireEvent.dragEnter(composer, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBe('true')
    fireEvent.dragLeave(composer, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBeNull()
  })

  it('leaves a non-file drag to the browser entirely — never captures then silently drops it', async () => {
    const composer = await renderReady()
    const over = createEvent.dragOver(composer, TEXT_DRAG)
    fireEvent(composer, over)
    expect(over.defaultPrevented).toBe(false)

    const fileOver = createEvent.dragOver(composer, FILE_DRAG)
    fireEvent(composer, fileOver)
    expect(fileOver.defaultPrevented).toBe(true)
  })

  it('clears the mark on drop — no matching dragleave ever arrives', async () => {
    const composer = await renderReady()
    fireEvent.dragEnter(composer, FILE_DRAG)
    expect(composer.getAttribute('data-dragging')).toBe('true')

    dropFiles(composer, [new File(['x'.repeat(100)], 'photo.png', { type: 'image/png' })])

    expect(await screen.findByText('photo.png')).toBeTruthy()
    expect(composer.getAttribute('data-dragging')).toBeNull()
  })

  // The composer wrapper here carries SessionBanners, the mode switcher, and the
  // composer-gate note ABOVE the actual input row — none of them are inside the plain
  // textarea/button row. Same class of fix as ChatPage's: the drag handlers have to live on
  // the wrapper that encloses ALL of that, not just the input row, or a drop landing on the
  // pending-attachment chips falls through to the browser's default navigate-to-file handler.
  it('claims a drop on the pending-attachment row too, not only the input row beneath it', async () => {
    const composer = await renderReady()
    dropFiles(composer, [new File(['x'.repeat(100)], 'photo.png', { type: 'image/png' })])
    const chip = await screen.findByText('photo.png')
    const chipRow = chip.closest('div')
    expect(composer.contains(chipRow)).toBe(true)

    const secondFile = new File(['y'.repeat(100)], 'second.png', { type: 'image/png' })
    const drop = createEvent.drop(chipRow, { dataTransfer: { types: ['Files'], files: [secondFile] } })
    fireEvent(chipRow, drop)

    expect(drop.defaultPrevented).toBe(true)
    expect(await screen.findByText('second.png')).toBeTruthy()
  })
})
