/**
 * The Chats panel on its own: tabs, search, sort, the pinned pager and the four answers it gives
 * in place of rows. It is handed its chats directly; the read behind them is `useProjectChats`'s
 * suite, and the panel inside the real workspace is `ChatHistoryRoute.test.tsx`'s.
 *
 * jsdom lays nothing out, so the pinned footer is asserted as structure — last child of the panel,
 * pushed down by `mt-auto` — and the pixels belong to the browser suite.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import ChatHistoryPanel from '../ChatHistoryPanel'
import type { ChatRow, ProjectChats } from '../../../hooks/useProjectChats'

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
  return { chats, loading: false, failed: false, capped: false, retry: vi.fn(), ...over }
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

  it('names an untitled chat by its kind', () => {
    renderPanel(ready([{ id: 'u', kind: 'plan', title: '', updatedAt: new Date().toISOString() }]))
    expect(titles()).toEqual(['Plan chatNew plan'])
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
