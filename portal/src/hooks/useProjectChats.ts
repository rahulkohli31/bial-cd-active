/**
 * One application's chats, read once, and the table model the chat list pages through.
 *
 * The read always carries the application's id, so a chat that belongs to no application never
 * reaches the list. It returns at most `CONVERSATION_LIST_CAP` rows, newest first, and a full
 * answer is flagged rather than quoted as a total. Filtering, sorting and paging happen here over
 * those rows, as the admin tables do it; the list's state belongs to the caller.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table'
import type {
  ColumnDef,
  ColumnFiltersState,
  PaginationState,
  SortingState,
  Table,
  Updater,
} from '@tanstack/react-table'
import { CONVERSATION_LIST_CAP, listProjectConversations } from '../utils/conversationApi'
import type { ChatKindTab, ChatListQuery } from '../utils/chatHistoryAddress'

export interface ChatRow {
  id: string
  /** The stored wire value, presented through `utils/chatKind.ts`. */
  kind: string
  /** `''` for a chat whose first message never landed. */
  title: string
  updatedAt: string
}

export interface ProjectChats {
  /** Newest first. Empty while loading and after a failure. */
  chats: ChatRow[]
  loading: boolean
  failed: boolean
  /** The read came back full, so older chats may exist beyond these. */
  capped: boolean
  /** Read again from a loading state, after a failure. */
  retry: () => void
  /** Read again with the current rows left on screen; settles once the new answer is in. */
  refresh: () => Promise<void>
}

interface Answer {
  projectId: string
  chats: ChatRow[]
  capped: boolean
  failed: boolean
}

const NO_CHATS: ChatRow[] = []

export function useProjectChats(projectId: string): ProjectChats {
  const [answer, setAnswer] = useState<Answer | null>(null)
  const latest = useRef(0)

  const load = useCallback((id: string): Promise<void> => {
    latest.current += 1
    const request = latest.current
    return listProjectConversations(id).then(
      (headers) => {
        if (latest.current !== request) return
        const chats = headers.flatMap((header) =>
          header !== null && header.projectId === id
            ? [{ id: header.id, kind: header.kind, title: header.title, updatedAt: header.updatedAt }]
            : [],
        )
        setAnswer({ projectId: id, chats, capped: headers.length >= CONVERSATION_LIST_CAP, failed: false })
      },
      () => {
        if (latest.current === request) setAnswer({ projectId: id, chats: NO_CHATS, capped: false, failed: true })
      },
    )
  }, [])

  useEffect(() => {
    void load(projectId)
  }, [projectId, load])

  const retry = useCallback(() => {
    setAnswer(null)
    void load(projectId)
  }, [load, projectId])

  const refresh = useCallback(() => load(projectId), [load, projectId])

  // An answer about another application is no answer: a switch reads as loading, never as its rows.
  const current = answer !== null && answer.projectId === projectId ? answer : null
  return {
    chats: current?.chats ?? NO_CHATS,
    loading: current === null,
    failed: current?.failed ?? false,
    capped: current?.capped ?? false,
    retry,
    refresh,
  }
}

export interface ChatTableOptions {
  chats: ChatRow[]
  columns: ColumnDef<ChatRow>[]
  query: ChatListQuery
  /** Writes a change back to wherever the query lives. Every filter and sort change carries page 1. */
  onQueryChange: (patch: Partial<ChatListQuery>) => void
  pageSize: number
  /** Only the newest rows are loaded, so oldest-first would start part-way through the list. */
  capped: boolean
}

function resolve<T>(updater: Updater<T>, current: T): T {
  return typeof updater === 'function' ? (updater as (old: T) => T)(current) : updater
}

function tabFor(value: unknown): ChatKindTab {
  return value === 'plan' || value === 'build' ? value : 'all'
}

/**
 * The list as a TanStack table: the kind tab is a filter on the `kind` column, the search a global
 * filter over the `chat` column, and Updated the one sort, newest or oldest first.
 */
export function useChatHistoryTable({
  chats,
  columns,
  query,
  onQueryChange,
  pageSize,
  capped,
}: ChatTableOptions): Table<ChatRow> {
  const columnFilters: ColumnFiltersState = query.kind === 'all' ? [] : [{ id: 'kind', value: query.kind }]
  const sorting: SortingState = [{ id: 'updated', desc: capped || query.sort === 'newest' }]
  const pagination: PaginationState = { pageIndex: query.page - 1, pageSize }

  return useReactTable({
    data: chats,
    columns,
    state: {
      columnFilters,
      globalFilter: query.q.trim(),
      sorting,
      pagination,
      columnVisibility: { kind: false },
    },
    getRowId: (row) => row.id,
    globalFilterFn: 'includesString',
    getColumnCanGlobalFilter: (column) => column.id === 'chat',
    enableSorting: !capped,
    enableSortingRemoval: false,
    // The page moves only when the reader moves it; a filter or a sort resets it explicitly below.
    autoResetPageIndex: false,
    onColumnFiltersChange: (updater) => {
      const next = resolve(updater, columnFilters)
      onQueryChange({ kind: tabFor(next.find((filter) => filter.id === 'kind')?.value), page: 1 })
    },
    onGlobalFilterChange: (updater) => {
      onQueryChange({ q: String(resolve<unknown>(updater, query.q) ?? ''), page: 1 })
    },
    onSortingChange: (updater) => {
      const next = resolve(updater, sorting)
      onQueryChange({ sort: next[0]?.desc === false ? 'oldest' : 'newest', page: 1 })
    },
    onPaginationChange: (updater) => {
      onQueryChange({ page: resolve(updater, pagination).pageIndex + 1 })
    },
    getCoreRowModel: getCoreRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  })
}
