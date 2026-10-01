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
 * land on the view that was left. Each row's `⋯` renames the chat in place or deletes it after one
 * confirmation; neither is optimistic — the list is read again and shows what the server holds.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { flexRender } from '@tanstack/react-table'
import { Search, Trash2 } from 'lucide-react'
import { cn } from '../../lib/utils'
import ConfirmDialog from '../ui/ConfirmDialog'
import { Input } from '../ui/input'
import { Skeleton } from '../ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../ui/table'
import { ToggleGroup, ToggleGroupItem } from '../ui/toggle-group'
import { ListPager } from '../projects/listChrome'
import { useChatHistoryTable, type ChatRow, type ProjectChats } from '../../hooks/useProjectChats'
import { ariaSortOf } from '../../utils/ariaSort'
import { chatKindFor } from '../../utils/chatKind'
import { ApiError } from '../../utils/apiError'
import { CONVERSATION_LIST_CAP, deleteConversation, renameConversation } from '../../utils/conversationApi'
import {
  chatListSearch,
  chatReturnedFrom,
  readChatListQuery,
  type ChatKindTab,
  type ChatListQuery,
} from '../../utils/chatHistoryAddress'
import { chatName, createChatHistoryColumns, RoundBackLink, type ChatRowActions } from './chatHistoryColumns'

/** Fixed, as the boards draw it: the rail has no room for a page-size select. */
export const CHAT_PAGE_SIZE = 8

const TABS: readonly ChatKindTab[] = ['all', 'plan', 'build']

const STILL_RUNNING = 'This chat is still running. Stop it first, then delete it.'
const DELETE_FAILED = 'Could not delete the chat. Try again.'

const ACTION =
  'mt-3 inline-flex h-8 items-center rounded-lg border border-bial-border bg-white px-3 text-[12.5px] font-semibold text-primary-900 shadow-sm transition hover:text-primary'

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

  const [editingId, setEditingId] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<ChatRow | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  // Where focus goes once a render has the rows it names: a row's `⋯`, or the heading for `null`.
  const focusNext = useRef<{ menu: string | null } | null>(null)

  const { refresh } = chats
  const actions = useMemo<ChatRowActions>(
    () => ({
      editingId,
      startRename: (chat) => setEditingId(chat.id),
      saveTitle: async (chat, title) => {
        await renameConversation(chat.id, title)
        await refresh()
      },
      endRename: (chat) => {
        focusNext.current = { menu: chat.id }
        setEditingId(null)
      },
      startDelete: (chat) => {
        setEditingId(null)
        setDeleteError(null)
        setDeleting(chat)
      },
    }),
    [editingId, refresh],
  )

  const listSearch = chatListSearch(query)
  const columns = useMemo(() => createChatHistoryColumns(listSearch, actions), [listSearch, actions])
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

  useEffect(() => {
    const target = focusNext.current
    if (target === null || chats.loading) return
    focusNext.current = null
    // A task later, so a closing dialog has handed focus back first; focus is moved only if the
    // control that held it has gone, never away from something the reader chose.
    window.setTimeout(() => {
      const active = document.activeElement
      if (active !== null && active !== document.body && active.isConnected) return
      const menus = rowsRef.current?.querySelectorAll<HTMLElement>('[data-chat-menu]') ?? []
      const menu = Array.from(menus).find((button) => button.dataset.chatMenu === target.menu)
      ;(menu ?? headingRef.current)?.focus()
    }, 0)
  })

  const confirmDelete = async () => {
    const chat = deleting
    if (chat === null) return
    setDeleteError(null)
    try {
      await deleteConversation(chat.id)
    } catch (caught) {
      const status = caught instanceof ApiError ? caught.status : null
      if (status === 409) {
        setDeleteError(STILL_RUNNING)
        return
      }
      // A 404 is a chat already gone: the list is read again as for a delete that landed.
      if (status !== 404) {
        setDeleteError(DELETE_FAILED)
        return
      }
    }
    const order = table.getPrePaginationRowModel().rows.map((row) => row.id)
    const at = order.indexOf(chat.id)
    await refresh()
    focusNext.current = { menu: at === -1 ? null : (order[at + 1] ?? order[at - 1] ?? null) }
    setDeleting(null)
  }

  const closeDelete = () => {
    if (deleting !== null) focusNext.current = { menu: deleting.id }
    setDeleting(null)
    setDeleteError(null)
  }

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
                  className={cn(
                    'relative',
                    row.id === editingId ? 'bg-canvas-savedirty' : 'hover:bg-bial-bg/60 focus-within:bg-bial-bg/60',
                  )}
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
        <RoundBackLink to={backToApplication} label="Back to the application" />
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

      {deleting !== null && (
        <ConfirmDialog
          title="Delete this chat?"
          body={
            <>
              “{chatName(deleting)}” and all of its messages will be deleted. Your application and its saved
              versions are not affected. This cannot be undone.
              {deleteError !== null && (
                <span role="alert" className="mt-2 block font-semibold text-danger">
                  {deleteError}
                </span>
              )}
            </>
          }
          icon={<Trash2 size={18} aria-hidden="true" className="text-red-700" />}
          iconClassName="bg-red-50"
          confirmLabel="Delete chat"
          tone="danger"
          testId="delete-chat"
          onClose={closeDelete}
          onConfirm={confirmDelete}
        />
      )}
    </section>
  )
}
