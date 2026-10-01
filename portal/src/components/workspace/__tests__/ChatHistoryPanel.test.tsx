/**
 * The Chats panel on its own: tabs, search, sort, the pinned pager, the four answers it gives in
 * place of rows, and each row's rename and delete. The list scenarios hand it its chats directly;
 * rename and delete run it over the real `useProjectChats` against a mocked server, because what
 * they prove is that the list is read again. The panel inside the real workspace is
 * `ChatHistoryRoute.test.tsx`'s.
 *
 * jsdom lays nothing out, so the pinned footer is asserted as structure — last child of the panel,
 * pushed down by `mt-auto` — and the pixels belong to the browser suite.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import ChatHistoryPanel from '../ChatHistoryPanel'
import { useProjectChats, type ChatRow, type ProjectChats } from '../../../hooks/useProjectChats'
import { ApiError } from '../../../utils/apiError'
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

const HOUR = 3_600_000

/** `n` chats, newest first, kinds alternating build / plan, an hour apart from now. */
function rows(n: number): ChatRow[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `c${i + 1}`,
    kind: i % 2 === 0 ? 'build' : 'plan',
    title: `Chat ${i + 1}`,
    updatedAt: new Date(Date.now() - (i + 1) * HOUR).toISOString(),
  }))
}

function ready(chats: ChatRow[], over: Partial<ProjectChats> = {}): ProjectChats {
  return { chats, loading: false, failed: false, capped: false, retry: vi.fn(), refresh: vi.fn(async () => {}), ...over }
}

function Where() {
  const location = useLocation()
  return (
    <>
      <span data-testid="where">{location.pathname + location.search}</span>
      <span data-testid="where-state">{JSON.stringify(location.state ?? null)}</span>
    </>
  )
}

function renderPanel(chats: ProjectChats, entry = '/projects/p1/chats', state?: unknown) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: entry.split('?')[0], search: entry.includes('?') ? `?${entry.split('?')[1]}` : '', state }]}>
      <Routes>
        <Route
          path="/projects/:projectId/chats"
          element={
            <>
              <ChatHistoryPanel projectId="p1" chats={chats} />
              <Where />
            </>
          }
        />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
}

const panel = () => screen.getByTestId('chat-history')
const chatRows = () => screen.queryAllByTestId('chat-row')
const titles = () => chatRows().map((row) => within(row).getAllByRole('link')[0].textContent)
const where = () => screen.getByTestId('where').textContent
const footer = () => screen.queryByTestId('chat-history-footer')
const tab = (name: string) => screen.getByRole('radio', { name })
const searchBox = () => screen.getByRole('searchbox')

afterEach(() => cleanup())

describe('the list as drawn', () => {
  it('shows page one: eight rows, newest first, each kind named and no status anywhere', () => {
    renderPanel(ready(rows(23)))

    // LIVENESS first: eight real rows, so the absences below are about rows that rendered.
    expect(chatRows()).toHaveLength(8)
    expect(titles()[0]).toContain('Chat 1')
    expect(titles()[7]).toContain('Chat 8')
    expect(within(chatRows()[0]).getByText('Build chat')).toBeTruthy()
    expect(within(chatRows()[1]).getByText('Plan chat')).toBeTruthy()
    // A chat only has a kind. No row, and nothing else in the panel, says what state it is in.
    expect(panel().textContent).not.toMatch(/\b(running|in progress|stopped|failed|completed|live|draft|status)\b/i)
  })

  it('heads the list with its back control, its name, its count, the tabs and the search', () => {
    renderPanel(ready(rows(23)))

    expect(screen.getByRole('heading', { name: 'Chats' })).toBeTruthy()
    expect(screen.getByTestId('chat-count').textContent).toBe('23')
    expect(['All', 'Plan', 'Build'].map((name) => tab(name).getAttribute('aria-checked'))).toEqual([
      'true',
      'false',
      'false',
    ])
    expect(searchBox().getAttribute('placeholder')).toBe('Search chats…')
    // Below `sm` the tabs take their own row under the title; above it they sit at its right end.
    expect(screen.getByTestId('chat-kind-tabs').className).toMatch(/\bbasis-full\b.*\bsm:basis-auto\b/)
    // A link, never a history step: the back control goes to the application itself.
    expect(screen.getByRole('link', { name: 'Back to the application' }).getAttribute('href')).toBe('/projects/p1')
  })

  it('shows a plan chat and a build chat of one application side by side', () => {
    renderPanel(ready(rows(2)))
    expect(within(chatRows()[0]).getByText('Build chat')).toBeTruthy()
    expect(within(chatRows()[1]).getByText('Plan chat')).toBeTruthy()
  })

  it('calls an untitled chat New chat whatever its kind', () => {
    renderPanel(ready([{ id: 'u', kind: 'plan', title: '', updatedAt: new Date().toISOString() }]))
    expect(titles()).toEqual(['Plan chatNew chat'])
  })

  it('★ keeps a long title to one line, whole in its tooltip, without pushing Updated aside', () => {
    const long = 'A very long chat title that goes on well past the width of any rail this list will ever sit in'
    renderPanel(ready([{ id: 'l', kind: 'build', title: long, updatedAt: new Date().toISOString() }]))

    const title = screen.getByTitle(long)
    expect(title.className).toMatch(/\btruncate\b/)
    expect(title.className).toMatch(/\bmin-w-0\b/)
    const updated = chatRows()[0].querySelectorAll('td')[1]
    expect(updated.className).toMatch(/\bwhitespace-nowrap\b/)
    expect(updated.className).toMatch(/\bw-\[\d+px\]/)
    expect(updated.textContent).toBe('just now')
    expect(screen.getByRole('table').className).toMatch(/\btable-fixed\b/)
  })
})

describe('the pager', () => {
  it('reads "Showing a–b of N" and moves forward and back a page', () => {
    renderPanel(ready(rows(23)))
    const status = within(footer() as HTMLElement).getByRole('status')
    expect(status.textContent).toBe('Showing 1–8 of 23')

    fireEvent.click(screen.getByRole('button', { name: 'Go to next page' }))
    expect(where()).toBe('/projects/p1/chats?page=2')
    expect(titles()[0]).toContain('Chat 9')
    expect(status.textContent).toBe('Showing 9–16 of 23')
    expect(screen.getByRole('button', { name: '2' }).getAttribute('aria-current')).toBe('page')

    fireEvent.click(screen.getByRole('button', { name: 'Go to previous page' }))
    expect(where()).toBe('/projects/p1/chats')
    expect(titles()[0]).toContain('Chat 1')
  })

  it('jumps to a numbered page, and the edges refuse a step past them', () => {
    renderPanel(ready(rows(23)))
    const previous = screen.getByRole('button', { name: 'Go to previous page' })
    expect(previous.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(previous)
    expect(where()).toBe('/projects/p1/chats')

    fireEvent.click(screen.getByRole('button', { name: '3' }))
    expect(where()).toBe('/projects/p1/chats?page=3')
    expect(chatRows()).toHaveLength(7)
    expect(within(footer() as HTMLElement).getByRole('status').textContent).toBe('Showing 17–23 of 23')
    const next = screen.getByRole('button', { name: 'Go to next page' })
    expect(next.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(next)
    expect(where()).toBe('/projects/p1/chats?page=3')
  })

  it('is the compact one: no jumps to the ends, and "a of b" for widths the numbers do not fit', () => {
    renderPanel(ready(rows(23)))
    const pager = screen.getByRole('navigation', { name: 'Chats pagination' })
    expect(within(pager).queryByRole('button', { name: 'First page' })).toBeNull()
    expect(within(pager).queryByRole('button', { name: 'Last page' })).toBeNull()
    expect(within(pager).getByText('1 of 3').className).toMatch(/\bsm:hidden\b/)
    expect(within(pager).getByRole('button', { name: '1' }).parentElement?.className).toMatch(/\bhidden sm:list-item\b/)
  })

  it('makes one page of exactly eight chats, with no second page to go to', () => {
    renderPanel(ready(rows(8)))
    expect(chatRows()).toHaveLength(8)
    expect(screen.queryByRole('button', { name: '2' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Go to next page' }).getAttribute('aria-disabled')).toBe('true')
  })

  it('reads "Showing 1 of 1" for one chat', () => {
    renderPanel(ready(rows(1)))
    expect(within(footer() as HTMLElement).getByRole('status').textContent).toBe('Showing 1 of 1')
  })

  it('brings a page past the end back to the last page, replacing the address', () => {
    renderPanel(ready(rows(23)), '/projects/p1/chats?page=9')
    expect(where()).toBe('/projects/p1/chats?page=3')
    expect(chatRows()).toHaveLength(7)
  })

  it('keeps the page in the address while the list failed, and shows its rows once Retry succeeds', async () => {
    h.list.mockRejectedValueOnce(new ApiError('Internal error', 500))
    render(
      <MemoryRouter initialEntries={['/projects/p1/chats?page=3']}>
        <Routes>
          <Route
            path="/projects/:projectId/chats"
            element={
              <>
                <LivePanel />
                <Where />
              </>
            }
          />
        </Routes>
      </MemoryRouter>,
    )
    await screen.findByText('Couldn’t load the chats')
    expect(where()).toBe('/projects/p1/chats?page=3')

    server = serverChats(23)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findAllByTestId('chat-row')
    expect(where()).toBe('/projects/p1/chats?page=3')
    expect(titles()[0]).toContain('Chat 17')
    expect(chatRows()).toHaveLength(7)
  })

  it.each([
    ['one chat', 1],
    ['a full page', 8],
    ['several pages', 23],
  ])('★ the footer is the panel\'s last child and is pushed to its foot — %s', (_label, n) => {
    renderPanel(ready(rows(n)))
    expect(panel().lastElementChild).toBe(footer())
    expect(footer()?.className).toMatch(/\bmt-auto\b/)
    expect(panel().className).toMatch(/\bflex-1\b/)
    expect(panel().className).toMatch(/\bflex-col\b/)
  })
})

describe('tabs, search and sort', () => {
  it('filters to Plan, writes it to the address and starts again from page one', () => {
    renderPanel(ready(rows(23)), '/projects/p1/chats?page=2')

    fireEvent.click(tab('Plan'))
    expect(where()).toBe('/projects/p1/chats?kind=plan')
    expect(chatRows().every((row) => within(row).queryByText('Plan chat') !== null)).toBe(true)
    expect(chatRows()).toHaveLength(8)
    expect(screen.getByTestId('chat-count').textContent).toBe('11')

    fireEvent.click(tab('All'))
    expect(where()).toBe('/projects/p1/chats')
    expect(screen.getByTestId('chat-count').textContent).toBe('23')
  })

  it('searches titles without regard to case, and starts again from page one', () => {
    const chats: ChatRow[] = [
      ...rows(10),
      { id: 's', kind: 'plan', title: 'Should the desk SIGN people out?', updatedAt: new Date(Date.now() - 3 * 24 * HOUR).toISOString() },
    ]
    renderPanel(ready(chats), '/projects/p1/chats?page=2')

    fireEvent.change(searchBox(), { target: { value: 'sign' } })
    expect(where()).toBe('/projects/p1/chats?q=sign')
    expect(titles()).toEqual(['Plan chatShould the desk SIGN people out?'])
    expect(screen.getByTestId('chat-count').textContent).toBe('1')
    expect(within(footer() as HTMLElement).getByRole('status').textContent).toBe('Showing 1 of 1')
  })

  it('★ a search that finds nothing says so, keeps the tabs and the search, and Clear filters undoes both', () => {
    renderPanel(ready(rows(5)), '/projects/p1/chats?kind=build&q=sign-out')

    expect(screen.getByText('No chats match')).toBeTruthy()
    expect(screen.getByText('Nothing matches “sign-out” with those filters.')).toBeTruthy()
    expect(screen.getByTestId('chat-count').textContent).toBe('0')
    expect(tab('Build').getAttribute('aria-checked')).toBe('true')
    expect((searchBox() as HTMLInputElement).value).toBe('sign-out')
    expect(chatRows()).toHaveLength(0)

    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(where()).toBe('/projects/p1/chats')
    expect(chatRows()).toHaveLength(5)
    expect(tab('All').getAttribute('aria-checked')).toBe('true')
    expect((searchBox() as HTMLInputElement).value).toBe('')
  })

  it('turns Updated from newest first to oldest first, and back', () => {
    renderPanel(ready(rows(10)), '/projects/p1/chats?page=2')
    const header = () => screen.getByRole('columnheader', { name: /updated/i })
    expect(header().getAttribute('aria-sort')).toBe('descending')

    fireEvent.click(screen.getByTestId('sort-updated'))
    expect(where()).toBe('/projects/p1/chats?sort=oldest')
    expect(header().getAttribute('aria-sort')).toBe('ascending')
    expect(titles()[0]).toContain('Chat 10')

    fireEvent.click(screen.getByTestId('sort-updated'))
    expect(where()).toBe('/projects/p1/chats')
    expect(titles()[0]).toContain('Chat 1')
  })

  it('★ opens on the tab, search, sort and page its address names', () => {
    renderPanel(ready(rows(30)), '/projects/p1/chats?kind=build&sort=oldest&page=2')
    expect(tab('Build').getAttribute('aria-checked')).toBe('true')
    expect(screen.getByRole('button', { name: '2' }).getAttribute('aria-current')).toBe('page')
    // Fifteen build chats, oldest first: page two starts at the ninth oldest.
    expect(titles()[0]).toContain('Chat 13')
  })
})

describe('the latest 200', () => {
  const full = () => ready(rows(200), { capped: true })

  it('★ counts a full answer as "200+" and says the list is the latest 200', () => {
    renderPanel(full())
    expect(screen.getByTestId('chat-count').textContent).toBe('200+')
    expect(footer()?.textContent).toContain('Showing the latest 200 chats')
    expect(searchBox().getAttribute('placeholder')).toBe('Search the latest 200 chats…')
    expect(searchBox().getAttribute('aria-label')).toBe('Search the latest 200 chats')
  })

  it('★ offers no oldest-first sort, even when the address asks for it', () => {
    renderPanel(full(), '/projects/p1/chats?sort=oldest')
    expect(screen.queryByTestId('sort-updated')).toBeNull()
    expect(screen.getByRole('columnheader', { name: /updated/i }).getAttribute('aria-sort')).toBe('descending')
    expect(titles()[0]).toContain('Chat 1')
  })

  it('a list one short of the cap is counted exactly, with no notice', () => {
    renderPanel(ready(rows(199)))
    expect(screen.getByTestId('chat-count').textContent).toBe('199')
    expect(footer()?.textContent).not.toContain('latest')
    expect(screen.getByTestId('sort-updated')).toBeTruthy()
  })
})

describe('the answers in place of rows', () => {
  it('shows a skeleton while loading, with the tabs and the search already there', () => {
    renderPanel(ready([], { loading: true }))
    expect(screen.getByTestId('chat-history-loading').getAttribute('aria-busy')).toBe('true')
    expect(tab('All')).toBeTruthy()
    expect(searchBox()).toBeTruthy()
    expect(footer()).toBeNull()
    expect(screen.getByTestId('chat-count').textContent).toBe('')
  })

  it('★ says it could not load, keeps the tabs and the search, and Retry reads again', () => {
    const retry = vi.fn()
    renderPanel(ready([], { failed: true, retry }))

    expect(screen.getByText('Couldn’t load the chats')).toBeTruthy()
    expect(tab('All')).toBeTruthy()
    expect(searchBox()).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(retry).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('No chats yet')).toBeNull()
  })

  it('★ an application with no chats says so and offers the way back', () => {
    renderPanel(ready([]))

    expect(screen.getByText('No chats yet')).toBeTruthy()
    expect(screen.getByText('Start a plan or build chat from the box under the app.')).toBeTruthy()
    const action = within(screen.getByTestId('chat-history-message')).getByRole('link', { name: 'Back to the application' })
    expect(action.getAttribute('href')).toBe('/projects/p1')
    expect(tab('All')).toBeTruthy()
    expect(searchBox()).toBeTruthy()
  })

  it('★ in every answer the message fills the panel, where the rows and footer would be', () => {
    for (const chats of [ready([]), ready([], { failed: true }), ready(rows(3))]) {
      renderPanel(chats, chats.chats.length > 0 ? '/projects/p1/chats?q=zzz' : '/projects/p1/chats')
      const message = screen.getByTestId('chat-history-message')
      expect(panel().lastElementChild).toBe(message)
      expect(message.className).toMatch(/\bflex-1\b/)
      expect(footer()).toBeNull()
      cleanup()
    }
  })
})

describe('opening a chat, and coming back', () => {
  it('★ each row is one real link to its chat, carrying the list to return to', () => {
    renderPanel(ready(rows(23)), '/projects/p1/chats?kind=build&page=2')

    const link = within(chatRows()[0]).getAllByRole('link')[0]
    // A real anchor: Enter follows it as natively as a click does.
    expect(link.tagName).toBe('A')
    expect(link.getAttribute('href')).toBe('/chat/c17')
    // One link per row, stretched over that row alone: the overlay is bounded by the row, so a
    // click on the age opens this chat and never the one below it.
    expect(within(chatRows()[0]).getAllByRole('link')).toHaveLength(1)
    expect(link.className).toMatch(/\bafter:absolute\b/)
    expect(link.className).toMatch(/\bafter:inset-0\b/)
    expect(chatRows()[0].className).toMatch(/\brelative\b/)

    fireEvent.click(link)
    expect(where()).toBe('/chat/c17')
    expect(JSON.parse(screen.getByTestId('where-state').textContent ?? 'null')).toEqual({
      chatList: '?kind=build&page=2',
    })
  })

  it('★ puts focus on its heading when it opens', () => {
    renderPanel(ready(rows(3)))
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Chats' }))
  })

  it('★ coming back from a chat puts focus on that chat\'s row', () => {
    renderPanel(ready(rows(5)), '/projects/p1/chats', { returnedFrom: 'c3' })
    expect(document.activeElement?.getAttribute('href')).toBe('/chat/c3')
  })
})

/**
 * RENAME AND DELETE, over the real read. `server` is what the mocked API holds: the list call
 * answers from it, a rename or a delete changes it, so a row only changes on screen if the panel
 * reads the list again — which is the no-optimism rule these scenarios hold the panel to.
 */
let server: ConversationHeader[] = []

function serverChats(n: number): ConversationHeader[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `c${i + 1}`,
    kind: i % 2 === 0 ? 'build' : 'plan',
    projectId: 'p1',
    title: `Chat ${i + 1}`,
    createdAt: '2026-09-01T00:00:00Z',
    updatedAt: new Date(Date.now() - (i + 1) * HOUR).toISOString(),
  }))
}

function LivePanel() {
  const chats = useProjectChats('p1')
  return <ChatHistoryPanel projectId="p1" chats={chats} />
}

async function renderLive(entry = '/projects/p1/chats') {
  const [pathname, search] = entry.split('?')
  render(
    <MemoryRouter initialEntries={[{ pathname, search: search ? `?${search}` : '' }]}>
      <Routes>
        <Route
          path="/projects/:projectId/chats"
          element={
            <>
              <LivePanel />
              <Where />
            </>
          }
        />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
  await screen.findAllByTestId('chat-row')
}

const rowOf = (id: string) => {
  const row = chatRows().find((candidate) => candidate.querySelector(`[data-chat-menu="${id}"]`) !== null)
  if (!row) throw new Error(`no row for ${id}`)
  return row
}
const menuOf = (id: string) => rowOf(id).querySelector<HTMLButtonElement>(`[data-chat-menu="${id}"]`) as HTMLButtonElement
const editor = () => screen.getByRole('textbox', { name: 'Chat name' }) as HTMLInputElement

/** Radix opens the menu on pointer-down, or on Enter from the keyboard. */
async function choose(id: string, item: 'Rename' | 'Delete…', via: 'pointer' | 'keyboard' = 'pointer') {
  if (via === 'pointer') fireEvent.pointerDown(menuOf(id))
  else fireEvent.keyDown(menuOf(id), { key: 'Enter' })
  fireEvent.click(await screen.findByRole('menuitem', { name: item }))
}

async function startRename(id: string) {
  await choose(id, 'Rename')
  await waitFor(() => expect(document.activeElement).toBe(editor()))
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

beforeEach(() => {
  server = serverChats(5)
  h.list.mockReset()
  h.rename.mockReset()
  h.remove.mockReset()
  h.list.mockImplementation(async () => server.map((chat) => ({ ...chat })))
  h.rename.mockImplementation(async (id: string, title: string) => {
    server = server.map((chat) => (chat.id === id ? { ...chat, title } : chat))
  })
  h.remove.mockImplementation(async (id: string) => {
    server = server.filter((chat) => chat.id !== id)
  })
})

describe('the row menu', () => {
  it('★ ends every row, always visible, beside the row link and never inside it', async () => {
    await renderLive()

    // LIVENESS: five real rows, each with its link, before anything is said about the menu.
    expect(chatRows()).toHaveLength(5)
    for (const row of chatRows()) {
      const link = within(row).getByRole('link')
      const menu = within(row).getByRole('button', { name: /^More actions for / })
      expect(link.contains(menu)).toBe(false)
      expect(menu.closest('a')).toBeNull()
      // Raised above the link's row-wide overlay, and drawn without waiting for a hover.
      expect(menu.className).toMatch(/\brelative\b/)
      expect(menu.className).toMatch(/\bz-10\b/)
      expect(menu.className).not.toMatch(/\b(opacity-0|invisible|hidden)\b|group-hover/)
      expect(menu.className).toContain('narrow:min-h-[44px]')
      expect(menu.className).toContain('narrow:min-w-[44px]')
    }
    expect(menuOf('c1').getAttribute('aria-label')).toBe('More actions for Chat 1')
  })

  it('★ opens from the keyboard, offers Rename, a separator and Delete… in red, and never opens the chat', async () => {
    await renderLive()

    fireEvent.keyDown(menuOf('c2'), { key: 'Enter' })
    const items = await screen.findAllByRole('menuitem')
    expect(items.map((item) => item.textContent)).toEqual(['Rename', 'Delete…'])
    expect(screen.getByRole('separator')).toBeTruthy()
    expect(items[1].className).toMatch(/\btext-red-700\b/)
    expect(where()).toBe('/projects/p1/chats')

    cleanup()
    await renderLive()
    fireEvent.pointerDown(menuOf('c2'))
    await screen.findByRole('menuitem', { name: 'Rename' })
    fireEvent.click(menuOf('c2'))
    expect(where()).toBe('/projects/p1/chats')
  })

  it('★ a rename whose re-read fails keeps the rows on screen, with no error state', async () => {
    await renderLive()
    h.list.mockRejectedValueOnce(new Error('offline'))
    await startRename('c1')
    fireEvent.change(editor(), { target: { value: 'Renamed' } })
    fireEvent.keyDown(editor(), { key: 'Enter' })

    await waitFor(() => expect(h.list).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Chat name' })).toBeNull())
    expect(chatRows()).toHaveLength(5)
    expect(screen.queryByText('Couldn’t load the chats')).toBeNull()
  })

  it('★ every read of the list names the application', async () => {
    await renderLive()
    await startRename('c1')
    fireEvent.change(editor(), { target: { value: 'Renamed' } })
    fireEvent.keyDown(editor(), { key: 'Enter' })
    await waitFor(() => expect(h.list).toHaveBeenCalledTimes(2))
    expect(h.list.mock.calls.every(([projectId]) => projectId === 'p1')).toBe(true)
  })
})

describe('renaming a chat in its row', () => {
  it('★ swaps the title for a focused input in a tinted row, with the hint, and the row link is gone', async () => {
    await renderLive()
    await startRename('c3')

    expect(editor().value).toBe('Chat 3')
    expect(screen.getByText('Enter to save · Esc to cancel')).toBeTruthy()
    expect(rowOf('c3').className).toMatch(/\bbg-canvas-savedirty\b/)
    // The row link does not exist while the title is being edited, so nothing in the row opens the chat.
    expect(within(rowOf('c3')).queryByRole('link')).toBeNull()
    expect(within(rowOf('c2')).getByRole('link')).toBeTruthy()
    // The menu handed focus back to its trigger as it closed; the editor kept it.
    await act(async () => new Promise((resolve) => setTimeout(resolve, 20)))
    expect(document.activeElement).toBe(editor())
  })

  it('★ Enter saves the trimmed name: the row shows it in the same place, with the same age, and focus is back on ⋯', async () => {
    await renderLive()
    const ageBefore = rowOf('c3').querySelectorAll('td')[1].textContent
    await startRename('c3')

    fireEvent.change(editor(), { target: { value: '  Should the desk sign people out?  ' } })
    fireEvent.keyDown(editor(), { key: 'Enter' })

    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Chat name' })).toBeNull())
    expect(h.rename).toHaveBeenCalledTimes(1)
    expect(h.rename).toHaveBeenCalledWith('c3', 'Should the desk sign people out?')
    expect(titles()[2]).toBe('Build chatShould the desk sign people out?')
    expect(rowOf('c3').querySelectorAll('td')[1].textContent).toBe(ageBefore)
    await waitFor(() => expect(document.activeElement).toBe(menuOf('c3')))
  })

  it('★ no rename is optimistic: the editor stays, read-only, until the list shows the new name', async () => {
    const held = deferred<void>()
    h.rename.mockImplementation(async (id: string, title: string) => {
      await held.promise
      server = server.map((chat) => (chat.id === id ? { ...chat, title } : chat))
    })
    await renderLive()
    await startRename('c2')

    fireEvent.change(editor(), { target: { value: 'Renamed' } })
    fireEvent.keyDown(editor(), { key: 'Enter' })
    expect(editor().readOnly).toBe(true)
    expect(screen.queryByText('Renamed')).toBeNull()

    await act(async () => held.resolve())
    await waitFor(() => expect(titles()[1]).toBe('Plan chatRenamed'))
  })

  it('★ Esc cancels: the old name stays, nothing is sent, and focus is back on ⋯', async () => {
    await renderLive()
    await startRename('c2')
    fireEvent.change(editor(), { target: { value: 'Something else' } })
    fireEvent.keyDown(editor(), { key: 'Escape' })

    expect(screen.queryByRole('textbox', { name: 'Chat name' })).toBeNull()
    expect(titles()[1]).toBe('Plan chatChat 2')
    expect(h.rename).not.toHaveBeenCalled()
    await waitFor(() => expect(document.activeElement).toBe(menuOf('c2')))
  })

  it('★ clicking away cancels, and nothing is ever saved on blur', async () => {
    await renderLive()
    await startRename('c2')
    fireEvent.change(editor(), { target: { value: 'Typed, then abandoned' } })
    fireEvent.blur(editor())

    expect(screen.queryByRole('textbox', { name: 'Chat name' })).toBeNull()
    expect(titles()[1]).toBe('Plan chatChat 2')
    expect(h.rename).not.toHaveBeenCalled()
  })

  it('★ Enter and then a blur send exactly one request', async () => {
    const held = deferred<void>()
    h.rename.mockImplementation(async (id: string, title: string) => {
      await held.promise
      server = server.map((chat) => (chat.id === id ? { ...chat, title } : chat))
    })
    await renderLive()
    await startRename('c1')
    fireEvent.change(editor(), { target: { value: 'Once only' } })
    fireEvent.keyDown(editor(), { key: 'Enter' })
    fireEvent.blur(editor())
    fireEvent.keyDown(editor(), { key: 'Enter' })

    await act(async () => held.resolve())
    await waitFor(() => expect(titles()[0]).toBe('Build chatOnce only'))
    expect(h.rename).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['empty', '', 'Give the chat a name'],
    ['whitespace only', '    ', 'Give the chat a name'],
    ['over 120 characters', 'x'.repeat(121), 'Keep the name under 120 characters'],
  ])('★ refuses an %s name before sending it, with the message in place of the hint', async (_label, typed, message) => {
    await renderLive()
    await startRename('c1')
    fireEvent.change(editor(), { target: { value: typed } })
    fireEvent.keyDown(editor(), { key: 'Enter' })

    const line = screen.getByText(message)
    expect(line.getAttribute('aria-live')).toBe('polite')
    expect(editor().getAttribute('aria-describedby')).toBe(line.id)
    expect(editor().getAttribute('aria-invalid')).toBe('true')
    expect(screen.queryByText('Enter to save · Esc to cancel')).toBeNull()
    expect(h.rename).not.toHaveBeenCalled()
    expect(editor().value).toBe(typed)
  })

  it('accepts a name of exactly 120 characters, counted after trimming', async () => {
    await renderLive()
    await startRename('c1')
    fireEvent.change(editor(), { target: { value: ` ${'x'.repeat(120)} ` } })
    fireEvent.keyDown(editor(), { key: 'Enter' })
    await waitFor(() => expect(h.rename).toHaveBeenCalledWith('c1', 'x'.repeat(120)))
  })

  it('★ a server failure keeps the editor open with the typed value, and says so', async () => {
    h.rename.mockRejectedValue(new ApiError('Internal error', 500))
    await renderLive()
    await startRename('c1')
    fireEvent.change(editor(), { target: { value: 'Kept as typed' } })
    fireEvent.keyDown(editor(), { key: 'Enter' })

    expect(await screen.findByText('Could not rename the chat. Try again.')).toBeTruthy()
    expect(editor().value).toBe('Kept as typed')
    expect(editor().readOnly).toBe(false)
    expect(editor().getAttribute('aria-invalid')).toBe('true')
    // The list was not read again: nothing landed, so nothing on screen changes.
    expect(h.list).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['title_required', 'Give the chat a name'],
    ['title_too_long', 'Keep the name under 120 characters'],
    ['title_invalid', 'The name cannot contain line breaks or control characters'],
  ])('reads the server refusal %s as the matching message', async (code, message) => {
    h.rename.mockRejectedValue(new ApiError('Refused', 400, code))
    await renderLive()
    await startRename('c1')
    fireEvent.change(editor(), { target: { value: 'Looks fine here' } })
    fireEvent.keyDown(editor(), { key: 'Enter' })
    expect(await screen.findByText(message)).toBeTruthy()
  })

  it('a chat gone before the rename lands closes the editor and reads the list again', async () => {
    h.rename.mockImplementation(async (id: string) => {
      server = server.filter((chat) => chat.id !== id)
      throw new ApiError('Not found', 404)
    })
    await renderLive()
    await startRename('c2')
    fireEvent.change(editor(), { target: { value: 'Too late' } })
    fireEvent.keyDown(editor(), { key: 'Enter' })

    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Chat name' })).toBeNull())
    expect(h.list).toHaveBeenCalledTimes(2)
    expect(chatRows()).toHaveLength(4)
    expect(screen.queryByText('Could not rename the chat. Try again.')).toBeNull()
  })

  it('a rename that settles after another row\'s editor opened leaves that editor open', async () => {
    const held = deferred<void>()
    h.rename.mockImplementation(async (id: string, title: string) => {
      await held.promise
      server = server.map((chat) => (chat.id === id ? { ...chat, title } : chat))
    })
    await renderLive()
    await startRename('c1')
    fireEvent.change(editor(), { target: { value: 'First' } })
    fireEvent.keyDown(editor(), { key: 'Enter' })
    await startRename('c2')
    fireEvent.change(editor(), { target: { value: 'Second, still typing' } })

    await act(async () => held.resolve())
    await waitFor(() => expect(rowOf('c1').textContent).toContain('First'))
    expect(editor().value).toBe('Second, still typing')
    expect(rowOf('c2').className).toMatch(/\bbg-canvas-savedirty\b/)
  })

  it('typing after a refusal brings the hint back', async () => {
    await renderLive()
    await startRename('c1')
    fireEvent.change(editor(), { target: { value: '' } })
    fireEvent.keyDown(editor(), { key: 'Enter' })
    expect(screen.getByText('Give the chat a name')).toBeTruthy()
    fireEvent.change(editor(), { target: { value: 'A' } })
    expect(screen.getByText('Enter to save · Esc to cancel')).toBeTruthy()
    expect(editor().getAttribute('aria-invalid')).toBe('false')
  })

  it('opens an untitled chat\'s editor empty, with New chat as the placeholder', async () => {
    server = [{ ...serverChats(1)[0], title: '' }]
    await renderLive()
    await startRename('c1')
    expect(editor().value).toBe('')
    expect(editor().getAttribute('placeholder')).toBe('New chat')
  })
})

describe('deleting a chat', () => {
  const dialog = () => screen.getByRole('dialog')
  const confirmButton = () => screen.getByTestId('delete-chat-confirm') as HTMLButtonElement

  it('★ asks once, naming the chat and saying the application is not affected', async () => {
    await renderLive()
    await choose('c2', 'Delete…')

    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(within(dialog()).getByText('Delete this chat?')).toBeTruthy()
    expect(dialog().textContent).toContain(
      '“Chat 2” and all of its messages will be deleted. Your application and its saved versions are not affected. This cannot be undone.',
    )
    expect(within(dialog()).getByRole('button', { name: 'Cancel' })).toBeTruthy()
    expect(confirmButton().textContent?.trim()).toBe('Delete chat')
    expect(confirmButton().className).toMatch(/\bbg-red-600\b/)
    expect(h.remove).not.toHaveBeenCalled()
  })

  it('★ Cancel changes nothing and puts focus back on the row\'s ⋯', async () => {
    await renderLive()
    await choose('c2', 'Delete…')
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(h.remove).not.toHaveBeenCalled()
    expect(chatRows()).toHaveLength(5)
    await waitFor(() => expect(document.activeElement).toBe(menuOf('c2')))
  })

  it('★ removes the row, recounts, and moves focus to the next row\'s ⋯', async () => {
    server = serverChats(23)
    await renderLive()
    expect(screen.getByTestId('chat-count').textContent).toBe('23')

    await choose('c3', 'Delete…')
    fireEvent.click(await screen.findByTestId('delete-chat-confirm'))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(h.remove).toHaveBeenCalledWith('c3')
    expect(screen.getByTestId('chat-count').textContent).toBe('22')
    expect(chatRows().some((row) => row.querySelector('[data-chat-menu="c3"]') !== null)).toBe(false)
    expect(within(footer() as HTMLElement).getByRole('status').textContent).toBe('Showing 1–8 of 22')
    await waitFor(() => expect(document.activeElement).toBe(menuOf('c4')))
  })

  it('★ deleting the last row of the last page steps back exactly one page, and focus goes to the row before it', async () => {
    server = serverChats(17)
    await renderLive('/projects/p1/chats?page=3')
    expect(chatRows()).toHaveLength(1)

    await choose('c17', 'Delete…')
    fireEvent.click(await screen.findByTestId('delete-chat-confirm'))

    await waitFor(() => expect(where()).toBe('/projects/p1/chats?page=2'))
    expect(chatRows()).toHaveLength(8)
    expect(screen.getByRole('button', { name: '2' }).getAttribute('aria-current')).toBe('page')
    await waitFor(() => expect(document.activeElement).toBe(menuOf('c16')))
  })

  it('deleting the only chat answers "No chats yet", with focus on the heading', async () => {
    server = serverChats(1)
    await renderLive()
    await choose('c1', 'Delete…')
    fireEvent.click(await screen.findByTestId('delete-chat-confirm'))

    expect(await screen.findByText('No chats yet')).toBeTruthy()
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Chats' })))
  })

  it('★ a running chat is refused inside the open dialog, which stays open and can be pressed again', async () => {
    h.remove.mockRejectedValue(new ApiError('Busy', 409, 'conversation_running'))
    await renderLive()
    await choose('c2', 'Delete…')
    fireEvent.click(await screen.findByTestId('delete-chat-confirm'))

    const alert = await within(dialog()).findByRole('alert')
    expect(alert.textContent).toBe('This chat is still running. Stop it first, then delete it.')
    await waitFor(() => expect(confirmButton().disabled).toBe(false))
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(menuOf('c2')).toBeTruthy()
    expect(chatRows()).toHaveLength(5)

    h.remove.mockReset()
    h.remove.mockImplementation(async (id: string) => {
      server = server.filter((chat) => chat.id !== id)
    })
    fireEvent.click(confirmButton())
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(chatRows()).toHaveLength(4)
  })

  it('a 409 with another code is the generic failure, not "still running"', async () => {
    h.remove.mockRejectedValue(new ApiError('Conflict', 409, 'something_else'))
    await renderLive()
    await choose('c2', 'Delete…')
    fireEvent.click(await screen.findByTestId('delete-chat-confirm'))

    expect((await within(dialog()).findByRole('alert')).textContent).toBe('Could not delete the chat. Try again.')
    expect(chatRows()).toHaveLength(5)
  })

  it('a chat already gone closes the dialog and reads the list again', async () => {
    h.remove.mockImplementation(async (id: string) => {
      server = server.filter((chat) => chat.id !== id)
      throw new ApiError('Not found', 404)
    })
    await renderLive()
    await choose('c2', 'Delete…')
    fireEvent.click(await screen.findByTestId('delete-chat-confirm'))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(h.list).toHaveBeenCalledTimes(2)
    expect(chatRows()).toHaveLength(4)
  })

  it('any other failure stays in the open dialog, the row kept', async () => {
    h.remove.mockRejectedValue(new ApiError('Internal error', 500))
    await renderLive()
    await choose('c2', 'Delete…')
    fireEvent.click(await screen.findByTestId('delete-chat-confirm'))

    expect((await within(dialog()).findByRole('alert')).textContent).toBe('Could not delete the chat. Try again.')
    expect(chatRows()).toHaveLength(5)
  })
})
