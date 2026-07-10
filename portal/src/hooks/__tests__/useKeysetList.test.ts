import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useKeysetList, type KeysetPage } from '../useKeysetList'

interface Row {
  id: string
}

const page = (items: Row[], nextCursor: string | null, hasMore: boolean): KeysetPage<Row> => ({
  items,
  nextCursor,
  hasMore,
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useKeysetList — forward paging', () => {
  it('appends successive pages with no duplicates and stops when hasMore is false', async () => {
    const fetchPage = vi
      .fn(async (_args: { cursor: string | null; q: string; limit: number }) => page([], null, false))
      .mockResolvedValueOnce(page([{ id: 'a' }, { id: 'b' }], 'c1', true))
      .mockResolvedValueOnce(page([{ id: 'c' }, { id: 'd' }], null, false))
    const { result } = renderHook(() => useKeysetList<Row>({ fetchPage }))

    await act(async () => {
      result.current.loadMore()
    })
    expect(result.current.items).toEqual([{ id: 'a' }, { id: 'b' }])
    expect(fetchPage).toHaveBeenNthCalledWith(1, { cursor: null, q: '', limit: 25 })
    expect(result.current.hasMore).toBe(true)

    await act(async () => {
      result.current.loadMore()
    })
    expect(result.current.items).toEqual([{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }])
    expect(fetchPage).toHaveBeenNthCalledWith(2, { cursor: 'c1', q: '', limit: 25 })
    expect(result.current.hasMore).toBe(false)

    // hasMore:false must disable any further loadMore — fetchPage is not called a third time.
    await act(async () => {
      result.current.loadMore()
    })
    expect(fetchPage).toHaveBeenCalledTimes(2)
  })

  it('honors an explicit pageSize in the fetch args', async () => {
    const fetchPage = vi.fn(async (_args: { cursor: string | null; q: string; limit: number }) =>
      page([{ id: 'a' }], null, false),
    )
    const { result } = renderHook(() => useKeysetList<Row>({ fetchPage, pageSize: 50 }))
    await act(async () => {
      result.current.loadMore()
    })
    expect(fetchPage).toHaveBeenCalledWith({ cursor: null, q: '', limit: 50 })
  })
})

describe('useKeysetList — search', () => {
  it('resets the cursor and clears items when the query changes', async () => {
    vi.useFakeTimers()
    const fetchPage = vi
      .fn(async (_args: { cursor: string | null; q: string; limit: number }) => page([], null, false))
      .mockResolvedValueOnce(page([{ id: 'a' }, { id: 'b' }], 'c1', true))
      .mockResolvedValueOnce(page([{ id: 'x' }], null, false))
    const { result } = renderHook(() => useKeysetList<Row>({ fetchPage }))

    await act(async () => {
      result.current.loadMore()
    })
    expect(result.current.items).toEqual([{ id: 'a' }, { id: 'b' }])

    act(() => {
      result.current.setQuery('vip')
    })
    expect(result.current.q).toBe('vip') // immediate value, before any fetch

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300)
    })
    // The second request carries q and NO cursor — a cursor from the old filter is meaningless.
    expect(fetchPage).toHaveBeenNthCalledWith(2, { cursor: null, q: 'vip', limit: 25 })
    expect(result.current.items).toEqual([{ id: 'x' }])
  })

  it('debounces setQuery: three keystrokes fire exactly one request after 300ms', async () => {
    vi.useFakeTimers()
    const fetchPage = vi.fn(async (_args: { cursor: string | null; q: string; limit: number }) =>
      page([{ id: 'x' }], null, false),
    )
    const { result } = renderHook(() => useKeysetList<Row>({ fetchPage }))

    act(() => {
      result.current.setQuery('v')
    })
    act(() => {
      result.current.setQuery('vi')
    })
    act(() => {
      result.current.setQuery('vip')
    })
    expect(fetchPage).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300)
    })
    expect(fetchPage).toHaveBeenCalledTimes(1)
    expect(fetchPage).toHaveBeenCalledWith({ cursor: null, q: 'vip', limit: 25 })
  })
})

describe('useKeysetList — out-of-order responses', () => {
  it('drops a stale loadMore response that resolves after a query change', async () => {
    vi.useFakeTimers()
    let resolveSlow: (value: KeysetPage<Row>) => void = () => {}
    const slow = new Promise<KeysetPage<Row>>((resolve) => {
      resolveSlow = resolve
    })
    let resolveFast: (value: KeysetPage<Row>) => void = () => {}
    const fast = new Promise<KeysetPage<Row>>((resolve) => {
      resolveFast = resolve
    })
    const fetchPage = vi
      .fn(async (_args: { cursor: string | null; q: string; limit: number }) => page([], null, false))
      .mockReturnValueOnce(slow) // the loadMore request — resolves LAST
      .mockReturnValueOnce(fast) // the query-change request — resolves FIRST
    const { result } = renderHook(() => useKeysetList<Row>({ fetchPage }))

    act(() => {
      result.current.loadMore() // request #1 (slow) starts
    })
    act(() => {
      result.current.setQuery('vip')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300) // request #2 (fast) starts
    })

    await act(async () => {
      resolveFast(page([{ id: 'x' }], null, false))
    })
    expect(result.current.items).toEqual([{ id: 'x' }])

    // The slow loadMore now resolves — it is stale and must NOT append.
    await act(async () => {
      resolveSlow(page([{ id: 'a' }, { id: 'b' }], 'c1', true))
    })
    expect(result.current.items).toEqual([{ id: 'x' }])
  })
})

describe('useKeysetList — local mutation and errors', () => {
  it('removeLocal drops a row without refetching; the next loadMore keeps the server cursor', async () => {
    const fetchPage = vi
      .fn(async (_args: { cursor: string | null; q: string; limit: number }) => page([], null, false))
      .mockResolvedValueOnce(page([{ id: 'a' }, { id: 'b' }], 'c1', true))
      .mockResolvedValueOnce(page([{ id: 'c' }], null, false))
    const { result } = renderHook(() => useKeysetList<Row>({ fetchPage }))

    await act(async () => {
      result.current.loadMore()
    })

    act(() => {
      result.current.removeLocal((row) => row.id === 'a')
    })
    expect(result.current.items).toEqual([{ id: 'b' }])
    expect(fetchPage).toHaveBeenCalledTimes(1) // no refetch

    await act(async () => {
      result.current.loadMore()
    })
    expect(fetchPage).toHaveBeenNthCalledWith(2, { cursor: 'c1', q: '', limit: 25 })
    expect(result.current.items).toEqual([{ id: 'b' }, { id: 'c' }])
  })

  it('surfaces a fetch rejection in error, clears loading, and leaves items intact', async () => {
    const fetchPage = vi
      .fn(async (_args: { cursor: string | null; q: string; limit: number }) => page([], null, false))
      .mockResolvedValueOnce(page([{ id: 'a' }], 'c1', true))
      .mockRejectedValueOnce(new Error('network boom'))
    const { result } = renderHook(() => useKeysetList<Row>({ fetchPage }))

    await act(async () => {
      result.current.loadMore()
    })
    expect(result.current.items).toEqual([{ id: 'a' }])

    await act(async () => {
      result.current.loadMore()
    })
    expect(result.current.error).toEqual(new Error('network boom'))
    expect(result.current.loading).toBe(false)
    expect(result.current.items).toEqual([{ id: 'a' }]) // never silently degrades to empty
  })
})

describe('useKeysetList — lastPage envelope', () => {
  it('exposes the raw page object so sibling keys (e.g. defaults) survive', async () => {
    interface RosterPage extends KeysetPage<Row> {
      defaults: { dailyTokenLimit: number }
    }
    const fetchPage = vi.fn(async (_args: { cursor: string | null; q: string; limit: number }): Promise<RosterPage> => ({
      items: [{ id: 'a' }],
      nextCursor: null,
      hasMore: false,
      defaults: { dailyTokenLimit: 100 },
    }))
    const { result } = renderHook(() => useKeysetList<Row, RosterPage>({ fetchPage }))

    await act(async () => {
      result.current.loadMore()
    })
    expect(result.current.lastPage?.defaults).toEqual({ dailyTokenLimit: 100 })
  })
})
