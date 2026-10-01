/**
 * THE RAIL'S CONTENTS — the composer, the last chat under it, the chat list in its place on the
 * list address, and the things that must NOT be here.
 *
 * This suite is deliberately narrow. Everything about the rail's WIDTH, its collapse and its
 * relationship to the pane is a claim about the shell and lives in `ProjectWorkspace.test.tsx`,
 * which renders through the real one.
 *
 * Every absence is PAIRED with the composer being really on screen. A rail that failed to render
 * at all passes an unpaired `toBeNull()` and reads as a clean removal.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import WorkspaceRail from '../WorkspaceRail'
import type { Project } from '../../../utils/projectApi'
import type { ConversationHeader } from '../../../utils/conversationApi'

const h = vi.hoisted(() => ({ list: vi.fn(), rename: vi.fn(), remove: vi.fn() }))

vi.mock('../../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/conversationApi')>()),
  listProjectConversations: h.list,
  renameConversation: h.rename,
  deleteConversation: h.remove,
}))
vi.mock('../../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/auth')>()),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
}))

const PROJECT: Project = {
  id: 'p1',
  name: 'VIP Movement',
  description: 'A tracked movement.',
  appId: 'a1',
  appStatus: null,
  hasRelaunchableSnapshot: true,
  hasSavedSnapshot: null,
  isServing: false,
  isPublishing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
}

const HOUR = 3_600_000

function header(over: Partial<ConversationHeader> & { id: string }): ConversationHeader {
  return {
    kind: 'build',
    projectId: 'p1',
    title: `Chat ${over.id}`,
    createdAt: '2026-09-01T00:00:00Z',
    updatedAt: new Date(Date.now() - 2 * HOUR).toISOString(),
    ...over,
  }
}

/** Both of the rail's addresses, with ONE rail element serving them, as `ProjectPage` does. */
function renderRail(entry = '/projects/p1') {
  const rail = <WorkspaceRail project={PROJECT} />
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/projects/:projectId" element={rail} />
        <Route path="/projects/:projectId/chats" element={rail} />
      </Routes>
    </MemoryRouter>,
  )
}

/** The composer really rendered — the liveness half every absence below is paired with. */
function composer(): HTMLElement {
  return screen.getByPlaceholderText(/Describe the change you need/i)
}

beforeEach(() => {
  h.list.mockReset()
  h.list.mockResolvedValue([])
})

afterEach(() => cleanup())

describe('what the rail carries', () => {
  it('carries the composer with its kind picker', () => {
    renderRail()
    expect(composer()).toBeTruthy()
    expect(screen.getByRole('radio', { name: 'Build' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'START A CHAT' })).toBeTruthy()
  })

  it('★ centres the composer in the whole column, in the middle of three rows', () => {
    // Mutation receipt: drop the equal outer rows and the composer is no longer centred — and the
    // card arriving below it would push it up.
    renderRail()
    const column = composer().closest('main') as HTMLElement
    expect(column.className).toMatch(/\bgrid-rows-\[1fr_auto_1fr\]/)
    expect(composer().closest('section')?.className).toMatch(/\brow-start-2\b/)
  })

  it('★ carries NO start control — exactly one exists, and it is the pane\'s', () => {
    // A second Start button on the same screen satisfies "exactly one control starts it" with
    // two, and both would race the same idempotent endpoint.
    renderRail()
    expect(composer()).toBeTruthy()
    expect(screen.queryByRole('button', { name: /launch application/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
  })
})

describe('the last chat', () => {
  it('★ shows the newest chat — kind, title and age — with View all above it', async () => {
    h.list.mockResolvedValue([
      header({ id: 'c2', title: 'Add an out-time column' }),
      header({ id: 'c1', kind: 'plan', title: 'Out-time — what should it record?' }),
    ])
    renderRail()

    const card = await screen.findByTestId('last-chat-card')
    expect(h.list).toHaveBeenCalledWith('p1')
    expect(card.textContent).toContain('Add an out-time column')
    expect(card.textContent).toContain('2h ago')
    expect(screen.getByTestId('chat-kind-chip').textContent).toBe('Build chat')
    expect(card.getAttribute('href')).toBe('/chat/c2')
    expect(screen.getByRole('heading', { name: 'Last chat' })).toBeTruthy()
    expect(screen.getByRole('link', { name: /view all/i }).getAttribute('href')).toBe('/projects/p1/chats')
    expect(screen.queryByText('Out-time — what should it record?')).toBeNull()
    expect(composer()).toBeTruthy()
  })

  it('calls an untitled last chat New chat, and keeps a long title to one line', async () => {
    h.list.mockResolvedValue([header({ id: 'c1', title: '' })])
    renderRail()
    expect((await screen.findByTestId('last-chat-card')).textContent).toContain('New chat')

    cleanup()
    const long = 'A very long chat title that goes on well past the width of any rail this card will sit in'
    h.list.mockResolvedValue([header({ id: 'c1', title: long })])
    renderRail()
    const title = await screen.findByTitle(long)
    expect(title.className).toMatch(/\btruncate\b/)
    expect(screen.getByTestId('chat-kind-chip').className).toMatch(/\bflex-shrink-0\b/)
  })

  it.each([
    ['while the read is in flight', () => new Promise<never>(() => {})],
    ['when the read fails', () => Promise.reject(new Error('offline'))],
    ['when there are no chats', () => Promise.resolve([])],
  ])('★ is absent %s, and takes no room: the composer is the column\'s only child', async (_when, answer) => {
    h.list.mockImplementation(answer)
    renderRail()
    await waitFor(() => expect(h.list).toHaveBeenCalled())

    expect(composer()).toBeTruthy()
    expect(screen.queryByTestId('last-chat')).toBeNull()
    expect(screen.queryByText(/view all/i)).toBeNull()
    expect(composer().closest('main')?.children).toHaveLength(1)
  })

  it('★ never offers a chat that belongs to no application', async () => {
    h.list.mockResolvedValue([
      header({ id: 'assistant', kind: 'generic', projectId: null, title: 'An assistant question' }),
      header({ id: 'c1', title: 'Add an out-time column' }),
    ])
    renderRail()
    expect((await screen.findByTestId('last-chat-card')).textContent).toContain('Add an out-time column')
    expect(screen.queryByText('An assistant question')).toBeNull()
  })
})

describe('the last chat follows the list\'s renames and deletes', () => {
  let server: ConversationHeader[] = []

  beforeEach(() => {
    server = [
      header({ id: 'c2', title: 'Add an out-time column' }),
      header({ id: 'c1', kind: 'plan', title: 'Out-time — what should it record?' }),
    ]
    h.list.mockImplementation(async () => server.map((chat) => ({ ...chat })))
    h.rename.mockImplementation(async (id: string, title: string) => {
      server = server.map((chat) => (chat.id === id ? { ...chat, title } : chat))
    })
    h.remove.mockImplementation(async (id: string) => {
      server = server.filter((chat) => chat.id !== id)
    })
  })

  async function openList() {
    fireEvent.click(await screen.findByRole('link', { name: /view all/i }))
    await screen.findAllByTestId('chat-row')
  }

  async function backToComposer() {
    fireEvent.click(screen.getAllByRole('link', { name: 'Back to the application' })[0])
    return screen.findByTestId('last-chat-card')
  }

  it('★ carries no menu of its own: renaming and deleting happen in the list', async () => {
    renderRail()
    const card = await screen.findByTestId('last-chat-card')
    expect(card.textContent).toContain('Add an out-time column')
    expect(screen.queryByRole('button', { name: /more actions/i })).toBeNull()
  })

  it('★ shows a chat renamed in the list by its new name, with no reload', async () => {
    renderRail()
    await openList()
    fireEvent.pointerDown(document.querySelector('[data-chat-menu="c2"]') as HTMLElement)
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Rename' }))
    const input = await screen.findByRole('textbox', { name: 'Chat name' })
    fireEvent.change(input, { target: { value: 'Add an in-time column too' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Chat name' })).toBeNull())

    expect((await backToComposer()).textContent).toContain('Add an in-time column too')
  })

  it('★ deleting the newest chat moves the card on to the next one', async () => {
    renderRail()
    await openList()
    fireEvent.pointerDown(document.querySelector('[data-chat-menu="c2"]') as HTMLElement)
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete…' }))
    fireEvent.click(await screen.findByTestId('delete-chat-confirm'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    const card = await backToComposer()
    expect(card.textContent).toContain('Out-time — what should it record?')
    expect(card.getAttribute('href')).toBe('/chat/c1')
  })
})

describe('the chat list address', () => {
  it('★ holds the list in place of the composer', async () => {
    h.list.mockResolvedValue([header({ id: 'c1', title: 'Add an out-time column' })])
    renderRail('/projects/p1/chats')

    expect(await screen.findByTestId('chat-row')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Chats' })).toBeTruthy()
    expect(screen.queryByPlaceholderText(/Describe the change you need/i)).toBeNull()
    expect(screen.queryByTestId('last-chat')).toBeNull()
  })

  it('★ reads the chats once for the card and the list together', async () => {
    h.list.mockResolvedValue([header({ id: 'c1', title: 'Add an out-time column' })])
    renderRail()

    fireEvent.click(await screen.findByRole('link', { name: /view all/i }))
    expect(await screen.findByTestId('chat-row')).toBeTruthy()
    fireEvent.click(screen.getAllByRole('link', { name: 'Back to the application' })[0])
    expect(await screen.findByTestId('last-chat-card')).toBeTruthy()
    expect(h.list).toHaveBeenCalledTimes(1)
  })

  it('★ opening the list puts focus on its heading, and its back control returns it to View all', async () => {
    h.list.mockResolvedValue([header({ id: 'c1' })])
    renderRail()

    const viewAll = await screen.findByRole('link', { name: /view all/i })
    viewAll.focus()
    fireEvent.click(viewAll)
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Chats' }))

    fireEvent.click(screen.getByRole('link', { name: 'Back to the application' }))
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('link', { name: /view all/i })))
  })
})

/**
 * ★ THE SECTIONS THAT LEFT, and the one thing that makes their departure safe: each has a home in
 * that application's settings, reachable from the toolbar's menu and from the home list.
 */
describe('what the rail gave up', () => {
  it.each([
    ['rail-data', 'the data section'],
    ['rail-app-status', 'the app status panel'],
    ['description-rail', 'the description block'],
    ['rail-save-state', 'the save sentence'],
  ])('no longer renders %s (%s)', (testid) => {
    renderRail()
    expect(composer()).toBeTruthy()
    expect(screen.queryByTestId(testid)).toBeNull()
  })

  it('names no integrations door, because the rail is not one any more', () => {
    // The copy is the fifth link of a removal: a page that still SAYS "manage integrations" is a
    // page still offering the capability, whatever happened to the component behind it.
    renderRail()
    expect(composer()).toBeTruthy()
    expect(screen.queryByText(/integrations/i)).toBeNull()
  })
})
