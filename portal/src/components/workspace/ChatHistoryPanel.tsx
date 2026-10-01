/**
 * THE CHATS PANEL — one application's chats in the rail: kind tabs, a title search, the Updated
 * sort, and a footer pinned to the panel's foot whatever the row count.
 *
 * New because no existing list fits a rail: `AdminDataTable` brings a page-size select and admin
 * chrome, and the application lists page on the server. What it is built from is shared — TanStack
 * through `useChatHistoryTable`, shadcn `Table`, the composer's `ToggleGroup`, and `ListPager` in
 * its compact form, so no pager here is drawn by hand.
 *
 * Its tab, search, sort and page live in the address, so Back and a chat's "All chats" link both
 * land on the view that was left.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import type { AriaAttributes } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { flexRender, type Column } from '@tanstack/react-table'
import { ArrowLeft, Search } from 'lucide-react'
import { cn } from '../../lib/utils'
import { Input } from '../ui/input'
import { Skeleton } from '../ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../ui/table'
import { ToggleGroup, ToggleGroupItem } from '../ui/toggle-group'
import { ListPager } from '../projects/listChrome'
import { useChatHistoryTable, type ChatRow, type ProjectChats } from '../../hooks/useProjectChats'
import { chatKindFor } from '../../utils/chatKind'
import { CONVERSATION_LIST_CAP } from '../../utils/conversationApi'
import {
  chatListSearch,
  chatReturnedFrom,
  readChatListQuery,
  type ChatKindTab,
  type ChatListQuery,
} from '../../utils/chatHistoryAddress'
import { createChatHistoryColumns } from './chatHistoryColumns'

/** Fixed, as the boards draw it: the rail has no room for a page-size select. */
export const CHAT_PAGE_SIZE = 8

const TABS: readonly ChatKindTab[] = ['all', 'plan', 'build']

const ACTION =
  'mt-3 inline-flex h-8 items-center rounded-lg border border-bial-border bg-white px-3 text-[12.5px] font-semibold text-primary-900 shadow-sm transition hover:text-primary'

function ariaSortOf(column: Column<ChatRow, unknown>): AriaAttributes['aria-sort'] {
  if (column.id !== 'updated') return undefined
  return column.getIsSorted() === 'asc' ? 'ascending' : 'descending'
}

/** The panel's answer in place of rows: a heading, one sentence and one way forward. */
function ListMessage({ heading, sentence, action }: { heading: string; sentence: string; action: ReactNode }) {
  return (
    <div
      data-testid="chat-history-message"
      className="flex flex-1 flex-col items-center justify-center rounded-xl border border-bial-border bg-white p-6 text-center"
    >
      <p className="text-sm font-bold text-tertiary">{heading}</p>
      <p className="mt-1.5 max-w-[260px] text-[12.5px] leading-relaxed text-neutral">{sentence}</p>
      {action}
    </div>
  )
}

function LoadingRows() {
  return (
    <div
      role="status"
      aria-busy="true"
      data-testid="chat-history-loading"
      className="overflow-hidden rounded-xl border border-bial-border bg-white"
    >
      <span className="sr-only">Loading chats…</span>
      {Array.from({ length: CHAT_PAGE_SIZE }, (_, i) => (
        <div key={i} className="flex items-center gap-[9px] border-b border-bial-border px-3 py-[11px] last:border-0">
          <Skeleton className="h-[26px] w-[26px] flex-shrink-0 rounded-lg" />
          <Skeleton className="h-3 flex-1" />
          <Skeleton className="h-3 w-10 flex-shrink-0" />
        </div>
      ))}
    </div>
  )
}

export interface ChatHistoryPanelProps {
  projectId: string
  chats: ProjectChats
}

export default function ChatHistoryPanel({ projectId, chats }: ChatHistoryPanelProps) {
  const headingId = useId()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const rowsRef = useRef<HTMLTableSectionElement>(null)
  const location = useLocation()
  const [params, setParams] = useSearchParams()
  const query = readChatListQuery(params)

  const commit = useCallback(
    (patch: Partial<ChatListQuery>, entry: 'push' | 'replace') => {
      setParams((prev) => new URLSearchParams(chatListSearch({ ...readChatListQuery(prev), ...patch })), {
        replace: entry === 'replace',
      })
    },
    [setParams],
  )

  const listSearch = chatListSearch(query)
  const columns = useMemo(() => createChatHistoryColumns(listSearch), [listSearch])
  const table = useChatHistoryTable({
    chats: chats.chats,
    columns,
    query,
    // Typing replaces the entry, so Back does not step through every keystroke.
    onQueryChange: (patch) => commit(patch, 'q' in patch ? 'replace' : 'push'),
    pageSize: CHAT_PAGE_SIZE,
    capped: chats.capped,
  })

  const matched = table.getFilteredRowModel().rows.length
  const pageCount = Math.max(table.getPageCount(), 1)
  const page = Math.min(query.page, pageCount)

  useEffect(() => {
    if (!chats.loading && query.page > pageCount) commit({ page: pageCount }, 'replace')
  }, [chats.loading, query.page, pageCount, commit])

  // Read during the first render, while whatever opened the list still holds focus.
  const [opener] = useState(() => document.activeElement)
  useEffect(() => {
    headingRef.current?.focus()
    return () => {
      const lost = document.activeElement === null || document.activeElement === document.body
      if (lost && opener instanceof HTMLElement && opener !== document.body && opener.isConnected) opener.focus()
    }
  }, [opener])

  const returnedFrom = useRef(chatReturnedFrom(location.state))
  useEffect(() => {
    const chatId = returnedFrom.current
    if (chatId === null || chats.loading) return
    returnedFrom.current = null
    const links = rowsRef.current?.querySelectorAll<HTMLElement>('[data-chat-id]') ?? []
    Array.from(links)
      .find((link) => link.dataset.chatId === chatId)
      ?.focus()
  }, [chats.loading])

  const search = query.q.trim()
  const filtered = query.kind !== 'all' || search !== ''
  const count = chats.loading || chats.failed ? '' : chats.capped && !filtered ? `${CONVERSATION_LIST_CAP}+` : String(matched)
  const first = (page - 1) * CHAT_PAGE_SIZE + 1
  const last = Math.min(page * CHAT_PAGE_SIZE, matched)
  const backToApplication = `/projects/${projectId}`

  let body: ReactNode
  if (chats.loading) {
    body = <LoadingRows />
  } else if (chats.failed) {
    body = (
      <ListMessage
        heading="Couldn’t load the chats"
        sentence="The list didn’t arrive. Try again in a moment."
        action={
          <button type="button" onClick={chats.retry} className={ACTION}>
            Retry
          </button>
        }
      />
    )
  } else if (chats.chats.length === 0) {
    body = (
      <ListMessage
        heading="No chats yet"
        sentence="Start a plan or build chat from the box under the app."
        action={
          <Link to={backToApplication} className={ACTION}>
            Back to the application
          </Link>
        }
      />
    )
  } else if (matched === 0) {
    body = (
      <ListMessage
        heading="No chats match"
        sentence={search === '' ? 'Nothing matches those filters.' : `Nothing matches “${search}” with those filters.`}
        action={
          <button type="button" onClick={() => commit({ kind: 'all', q: '', page: 1 }, 'push')} className={ACTION}>
            Clear filters
          </button>
        }
      />
    )
  } else {
    body = (
      <>
        <div className="overflow-hidden rounded-xl border border-bial-border bg-white">
          <Table className="table-fixed">
            <TableHeader>
              {table.getHeaderGroups().map((group) => (
                <TableRow key={group.id} className="border-b border-bial-border bg-bial-bg/60">
                  {group.headers.map((header) => (
                    <TableHead
                      key={header.id}
                      aria-sort={ariaSortOf(header.column)}
                      className={cn('px-3 py-[9px]', header.column.columnDef.meta?.className, 'text-[10px] text-primary-900')}
                    >
                      {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                    </TableHead>
                  ))}
                </TableRow>
              ))}
            </TableHeader>
            <TableBody ref={rowsRef}>
              {table.getRowModel().rows.map((row) => (
                <TableRow
                  key={row.id}
                  data-testid="chat-row"
                  className="relative hover:bg-bial-bg/60 focus-within:bg-bial-bg/60"
                >
                  {row.getVisibleCells().map((cell) => (
                    <TableCell key={cell.id} className={cn('px-3 py-[11px]', cell.column.columnDef.meta?.className)}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
        <div
          data-testid="chat-history-footer"
          className="mt-auto flex items-center justify-between gap-2.5 pt-3 text-xs text-neutral"
        >
          <div className="min-w-0">
            <p role="status" aria-live="polite" className="tabular-nums">
              {first === last ? `Showing ${first} of ${matched}` : `Showing ${first}–${last} of ${matched}`}
            </p>
            {chats.capped && <p className="mt-0.5">Showing the latest {CONVERSATION_LIST_CAP} chats</p>}
          </div>
          <ListPager
            compact
            page={page}
            activePage={page}
            totalPages={pageCount}
            onGo={(next) => table.setPageIndex(next - 1)}
            label="Chats pagination"
          />
        </div>
      </>
    )
  }

  const searchLabel = chats.capped ? `Search the latest ${CONVERSATION_LIST_CAP} chats` : 'Search chats'

  return (
    <section
      aria-labelledby={headingId}
      data-testid="chat-history"
      className="flex flex-1 flex-col bg-white px-[18px] pb-3.5 pt-4"
    >
      <div className="mb-3.5 flex flex-wrap items-center gap-2.5">
        <Link
          to={backToApplication}
          aria-label="Back to the application"
          title="Back to the application"
          className="inline-flex flex-shrink-0 items-center justify-center rounded-full narrow:min-h-[44px] narrow:min-w-[44px]"
        >
          <span className="inline-flex h-[30px] w-[30px] items-center justify-center rounded-full border border-bial-border bg-white text-gray-600 shadow-[0_2px_8px_rgba(16,24,40,.08)] transition hover:text-primary">
            <ArrowLeft size={15} aria-hidden="true" />
          </span>
        </Link>
        <h2
          id={headingId}
          ref={headingRef}
          tabIndex={-1}
          className="text-[15px] font-extrabold tracking-[-.2px] text-tertiary focus:outline-none"
        >
          Chats
        </h2>
        <span data-testid="chat-count" className="text-xs tabular-nums text-neutral">
          {count}
        </span>
        {/* On a phone-width rail the tabs take a row of their own under the title. */}
        <div data-testid="chat-kind-tabs" className="flex basis-full sm:ml-auto sm:basis-auto">
          <ToggleGroup
            type="single"
            value={query.kind}
            onValueChange={(next) => {
              // Radix hands back '' when the pressed tab is pressed again; a tab is always chosen.
              if (next) table.getColumn('kind')?.setFilterValue(next === 'all' ? undefined : next)
            }}
            size="sm"
            aria-label="Which chats"
            className="inline-flex gap-[3px] rounded-[9px] bg-bial-bg p-[3px]"
          >
            {TABS.map((tab) => (
              <ToggleGroupItem
                key={tab}
                value={tab}
                className="rounded-[7px] px-[11px] py-[5px] text-[11.5px] font-semibold data-[state=on]:font-bold data-[state=on]:text-primary-900"
              >
                {tab === 'all' ? 'All' : chatKindFor(tab).word}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </div>
      </div>

      <div className="relative mb-2.5">
        <Search
          size={14}
          aria-hidden="true"
          className="pointer-events-none absolute left-[11px] top-1/2 -translate-y-1/2 text-neutral"
        />
        <Input
          type="search"
          value={query.q}
          onChange={(e) => table.setGlobalFilter(e.target.value)}
          aria-label={searchLabel}
          placeholder={`${searchLabel}…`}
          className="h-[34px] rounded-md border-bial-border bg-white pl-[33px] pr-3 text-[13px] font-medium text-tertiary placeholder:text-canvas-label md:text-[13px]"
        />
      </div>

      {body}
    </section>
  )
}
