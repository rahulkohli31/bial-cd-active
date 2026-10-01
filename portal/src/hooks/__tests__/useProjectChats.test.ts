/**
 * The chats read and the table model the chat list pages through. The read is mocked at the API
 * module, so what is asserted is what was ASKED for as well as what came back.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useChatHistoryTable, useProjectChats, type ChatRow } from '../useProjectChats'
import { createChatHistoryColumns, type ChatRowActions } from '../../components/workspace/chatHistoryColumns'
import type { ChatListQuery } from '../../utils/chatHistoryAddress'
import type { ConversationHeader } from '../../utils/conversationApi'

const h = vi.hoisted(() => ({ list: vi.fn() }))

vi.mock('../../utils/conversationApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/conversationApi')>()),
  listProjectConversations: h.list,
}))
vi.mock('../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/auth')>()),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
}))

const HOUR = 3_600_000
const NOW = Date.parse('2026-09-30T12:00:00Z')

/** The model never presses a row's menu; these only have to exist. */
const NO_ACTIONS: ChatRowActions = {
  editingId: null,
  startRename: () => {},
  saveTitle: async () => {},
  endRename: () => {},
  refresh: async () => {},
  startDelete: () => {},
}

function header(over: Partial<ConversationHeader> & { id: string }): ConversationHeader {
  return {
    kind: 'build',
    projectId: 'p1',
    title: `Chat ${over.id}`,
    createdAt: '2026-09-01T00:00:00Z',
    updatedAt: '2026-09-01T00:00:00Z',
    ...over,
  }
}

/** `n` chats, newest first, kinds alternating build / plan, one hour apart. */
function rows(n: number): ChatRow[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `c${i + 1}`,
    kind: i % 2 === 0 ? 'build' : 'plan',
    title: `Chat ${i + 1}`,
    updatedAt: new Date(NOW - i * HOUR).toISOString(),
  }))
}

/** Never settles, so a read can be held in flight for as long as a scenario needs. */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

beforeEach(() => {
  h.list.mockReset()
})

describe('useProjectChats — the read', () => {
  it('asks for this application by id and drops the empty entries the API can send', async () => {
    h.list.mockResolvedValue([null, header({ id: 'a' }), null, header({ id: 'b', kind: 'plan', title: '' })])
    const { result } = renderHook(() => useProjectChats('p1'))

    expect(result.current.loading).toBe(true)
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(h.list).toHaveBeenCalledTimes(1)
    expect(h.list).toHaveBeenCalledWith('p1')
    expect(result.current.chats.map((chat) => chat.id)).toEqual(['a', 'b'])
    expect(result.current.chats[1]).toEqual({ id: 'b', kind: 'plan', title: '', updatedAt: '2026-09-01T00:00:00Z' })
    expect(result.current.failed).toBe(false)
  })

  it('never lists a chat that belongs to no application, or to another one', async () => {
    h.list.mockResolvedValue([
      header({ id: 'assistant', kind: 'generic', projectId: null }),
      header({ id: 'elsewhere', projectId: 'p2' }),
      header({ id: 'mine' }),
    ])
    const { result } = renderHook(() => useProjectChats('p1'))

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.chats.map((chat) => chat.id)).toEqual(['mine'])
  })

  it('flags a full answer as capped, and one short of it as the whole list', async () => {
    const full = Array.from({ length: 200 }, (_, i) => header({ id: `c${i}` }))
    h.list.mockResolvedValueOnce(full).mockResolvedValueOnce(full.slice(1))

    const capped = renderHook(() => useProjectChats('p1'))
    await waitFor(() => expect(capped.result.current.loading).toBe(false))
    expect(capped.result.current.capped).toBe(true)
    expect(capped.result.current.chats).toHaveLength(200)

    const whole = renderHook(() => useProjectChats('p1'))
    await waitFor(() => expect(whole.result.current.loading).toBe(false))
    expect(whole.result.current.capped).toBe(false)
  })

  it('answers zero chats as an empty list, not a failure', async () => {
    h.list.mockResolvedValue([])
    const { result } = renderHook(() => useProjectChats('p1'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.chats).toEqual([])
    expect(result.current.failed).toBe(false)
  })

  it('reports a failed read, and Retry reads again from a loading state', async () => {
    h.list.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce([header({ id: 'a' })])
    const { result } = renderHook(() => useProjectChats('p1'))

    await waitFor(() => expect(result.current.failed).toBe(true))
    expect(result.current.chats).toEqual([])
    expect(result.current.loading).toBe(false)

    act(() => result.current.retry())
    expect(result.current.loading).toBe(true)
    expect(result.current.failed).toBe(false)
    await waitFor(() => expect(result.current.chats.map((chat) => chat.id)).toEqual(['a']))
    expect(h.list).toHaveBeenCalledTimes(2)
  })

  it('★ refresh reads again with the rows left on screen, and settles once the new answer is in', async () => {
    const second = deferred<(ConversationHeader | null)[]>()
    h.list.mockResolvedValueOnce([header({ id: 'a', title: 'Old name' })]).mockReturnValueOnce(second.promise)
    const { result } = renderHook(() => useProjectChats('p1'))
    await waitFor(() => expect(result.current.chats).toHaveLength(1))

    let settled = false
    act(() => {
      void result.current.refresh().then(() => {
        settled = true
      })
    })
    // Never a loading state in between: the rows the reader is looking at stay put.
    expect(result.current.loading).toBe(false)
    expect(result.current.chats[0].title).toBe('Old name')
    expect(settled).toBe(false)

    await act(async () => second.resolve([header({ id: 'a', title: 'New name' })]))
    expect(settled).toBe(true)
    expect(result.current.chats[0].title).toBe('New name')
    expect(h.list).toHaveBeenLastCalledWith('p1')
  })

  it('★ a refresh that fails keeps the rows and does not report a failure', async () => {
    h.list.mockResolvedValueOnce([header({ id: 'a' })]).mockRejectedValueOnce(new Error('offline'))
    const { result } = renderHook(() => useProjectChats('p1'))
    await waitFor(() => expect(result.current.chats).toHaveLength(1))

    await act(async () => result.current.refresh())

    expect(h.list).toHaveBeenCalledTimes(2)
    expect(result.current.chats.map((chat) => chat.id)).toEqual(['a'])
    expect(result.current.failed).toBe(false)
  })

  it('ignores a late answer for the application it has moved away from', async () => {
    const first = deferred<(ConversationHeader | null)[]>()
    h.list.mockImplementation((projectId: string) =>
      projectId === 'p1' ? first.promise : Promise.resolve([header({ id: 'b1', projectId: 'p2' })]),
    )
    const { result, rerender } = renderHook(({ id }) => useProjectChats(id), { initialProps: { id: 'p1' } })

    rerender({ id: 'p2' })
    await waitFor(() => expect(result.current.chats.map((chat) => chat.id)).toEqual(['b1']))

    await act(async () => first.resolve([header({ id: 'a1' })]))
    expect(result.current.chats.map((chat) => chat.id)).toEqual(['b1'])
  })

  it('shows a switch as loading, never as the previous application\'s rows', async () => {
    const second = deferred<(ConversationHeader | null)[]>()
    h.list.mockImplementation((projectId: string) =>
      projectId === 'p1' ? Promise.resolve([header({ id: 'a1' })]) : second.promise,
    )
    const { result, rerender } = renderHook(({ id }) => useProjectChats(id), { initialProps: { id: 'p1' } })
    await waitFor(() => expect(result.current.chats).toHaveLength(1))

    rerender({ id: 'p2' })
    expect(result.current.loading).toBe(true)
    expect(result.current.chats).toEqual([])
    expect(h.list).toHaveBeenLastCalledWith('p2')
  })
})

describe('useChatHistoryTable — the model', () => {
  const FRESH: ChatListQuery = { kind: 'all', q: '', sort: 'newest', page: 1 }

  function model(chats: ChatRow[], query: Partial<ChatListQuery> = {}, capped = false) {
    const onQueryChange = vi.fn()
    const columns = createChatHistoryColumns('', NO_ACTIONS)
    const { result } = renderHook(() =>
      useChatHistoryTable({ chats, columns, query: { ...FRESH, ...query }, onQueryChange, pageSize: 8, capped }),
    )
    const ids = () => result.current.getRowModel().rows.map((row) => row.original.id)
    return { table: () => result.current, ids, onQueryChange }
  }

  it('pages 23 chats into three pages of eight, the newest eight first', () => {
    const { table, ids } = model(rows(23))
    expect(table().getPageCount()).toBe(3)
    expect(ids()).toEqual(['c1', 'c2', 'c3', 'c4', 'c5', 'c6', 'c7', 'c8'])
  })

  it('shows the last page short, from the query page', () => {
    const { ids } = model(rows(23), { page: 3 })
    expect(ids()).toEqual(['c17', 'c18', 'c19', 'c20', 'c21', 'c22', 'c23'])
  })

  it('sorts by Updated whatever order the rows arrived in, oldest first when asked', () => {
    const shuffled = [...rows(5)].reverse()
    expect(model(shuffled).ids()).toEqual(['c1', 'c2', 'c3', 'c4', 'c5'])
    expect(model(shuffled, { sort: 'oldest' }).ids()).toEqual(['c5', 'c4', 'c3', 'c2', 'c1'])
  })

  it('filters to one kind on the Plan tab, and All restores every row', () => {
    const plan = model(rows(6), { kind: 'plan' })
    expect(plan.ids()).toEqual(['c2', 'c4', 'c6'])
    expect(model(rows(6), { kind: 'all' }).ids()).toHaveLength(6)
  })

  it('searches titles without regard to case', () => {
    const chats: ChatRow[] = [
      { id: 'a', kind: 'plan', title: 'Should the desk SIGN people out?', updatedAt: '2026-09-30T10:00:00Z' },
      { id: 'b', kind: 'build', title: 'Add an out-time column', updatedAt: '2026-09-30T09:00:00Z' },
      { id: 'c', kind: 'build', title: 'Signage for the lobby', updatedAt: '2026-09-30T08:00:00Z' },
    ]
    expect(model(chats, { q: 'sign' }).ids()).toEqual(['a', 'c'])
    expect(model(chats, { q: '  sign  ' }).ids()).toEqual(['a', 'c'])
  })

  it('answers a search that matches nothing with no rows and no pages', () => {
    const { table, ids } = model(rows(5), { q: 'nothing like this' })
    expect(ids()).toEqual([])
    expect(table().getPageCount()).toBe(0)
  })

  it('calls an untitled chat New chat whatever its kind, and finds it by that name', () => {
    const chats: ChatRow[] = [
      { id: 'untitled', kind: 'plan', title: '', updatedAt: '2026-09-30T10:00:00Z' },
      { id: 'named', kind: 'build', title: 'Add an out-time column', updatedAt: '2026-09-30T09:00:00Z' },
    ]
    const { table } = model(chats, { q: 'new chat' })
    expect(table().getRowModel().rows.map((row) => row.getValue('chat'))).toEqual(['New chat'])
  })

  it('makes exactly one page of exactly eight chats, and none of none', () => {
    expect(model(rows(8)).table().getPageCount()).toBe(1)
    expect(model(rows(8)).table().getCanNextPage()).toBe(false)
    expect(model(rows(1)).table().getPageCount()).toBe(1)
    expect(model([]).table().getPageCount()).toBe(0)
  })

  it('★ a capped list cannot sort oldest first, whatever the address asks for', () => {
    // Oldest first over the newest 200 would start part-way through the history and call it the
    // beginning.
    const { table, ids } = model(rows(10), { sort: 'oldest' }, true)
    expect(table().getColumn('updated')?.getCanSort()).toBe(false)
    expect(ids()[0]).toBe('c1')
    // LIVENESS: the same rows, uncapped, do sort oldest first.
    expect(model(rows(10), { sort: 'oldest' }).ids()[0]).toBe('c10')
  })

  it('writes every change back as a query patch, and a filter or a sort carries page one', () => {
    const { table, onQueryChange } = model(rows(23), { page: 2 })

    act(() => table().getColumn('kind')?.setFilterValue('plan'))
    expect(onQueryChange).toHaveBeenLastCalledWith({ kind: 'plan', page: 1 })

    act(() => table().setGlobalFilter('sign'))
    expect(onQueryChange).toHaveBeenLastCalledWith({ q: 'sign', page: 1 })

    act(() => table().getColumn('updated')?.toggleSorting(false))
    expect(onQueryChange).toHaveBeenLastCalledWith({ sort: 'oldest', page: 1 })

    act(() => table().setPageIndex(2))
    expect(onQueryChange).toHaveBeenLastCalledWith({ page: 3 })

    act(() => table().getColumn('kind')?.setFilterValue(undefined))
    expect(onQueryChange).toHaveBeenLastCalledWith({ kind: 'all', page: 1 })
  })

  it('does not move the page itself when the rows change', async () => {
    const onQueryChange = vi.fn()
    const columns = createChatHistoryColumns('', NO_ACTIONS)
    const { rerender } = renderHook(
      ({ chats }) => {
        const table = useChatHistoryTable({ chats, columns, query: { ...FRESH, page: 2 }, onQueryChange, pageSize: 8, capped: false })
        // Read every render, as the panel does: TanStack only notices new rows when they are read.
        table.getRowModel()
        return table
      },
      { initialProps: { chats: rows(23) } },
    )
    // TanStack arms its own page reset on a microtask after the first read and fires it on one
    // after a later change; flushing both is what lets this test bite.
    await act(async () => {})
    rerender({ chats: rows(22) })
    await act(async () => {})
    expect(onQueryChange).not.toHaveBeenCalled()
  })
})
