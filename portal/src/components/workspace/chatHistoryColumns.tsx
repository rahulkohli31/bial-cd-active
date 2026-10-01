/**
 * The chat list's columns, and the kind marks every surface that names a chat draws.
 *
 * Its own file rather than the admin `columns.tsx`, whose rows are people and limits: a chat row
 * is a kind tile and a title that together are the row's link, then an age. Cells render through
 * the table, so a per-row control arrives as one more column beside the link, never inside it.
 */
import type { Column, ColumnDef } from '@tanstack/react-table'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { chatKindFor, type ChatKindPresentation } from '../../utils/chatKind'
import { openedFromChatList } from '../../utils/chatHistoryAddress'
import { relativeTime } from '../../utils/relativeTime'
import type { ChatRow } from '../../hooks/useProjectChats'

/** What a chat with no title yet is called, here and in the toolbar alike. */
export function untitledChatName(kind: ChatKindPresentation): string {
  return `New ${kind.word.toLowerCase()}`
}

export function chatName(chat: Pick<ChatRow, 'kind' | 'title'>): string {
  return chat.title || untitledChatName(chatKindFor(chat.kind))
}

/** The kind as a square tile, for rows. Its name is spoken; the glyph is not. */
export function KindTile({ kind }: { kind: string }) {
  const look = chatKindFor(kind)
  const name = `${look.word}${look.completion}`
  return (
    <span
      title={name}
      className={`inline-flex h-[26px] w-[26px] flex-shrink-0 items-center justify-center rounded-lg ${look.pill}`}
    >
      <look.Icon size={13} aria-hidden="true" />
      <span className="sr-only">{name}</span>
    </span>
  )
}

/** The kind as a caps pill, for the Last chat card and the chat's own strip. */
export function KindChip({ kind }: { kind: string }) {
  const look = chatKindFor(kind)
  return (
    <span
      data-testid="chat-kind-chip"
      className={`inline-flex flex-shrink-0 items-center gap-1 whitespace-nowrap rounded-full px-2 py-[3px] text-[10px] font-bold uppercase tracking-[.3px] ${look.pill}`}
    >
      <look.Icon size={10} aria-hidden="true" />
      {look.word}
      {look.completion && <span className="sr-only">{look.completion}</span>}
    </span>
  )
}

function UpdatedHeader({ column }: { column: Column<ChatRow, unknown> }) {
  const newestFirst = column.getIsSorted() !== 'asc'
  const Chevron = newestFirst ? ChevronDown : ChevronUp
  const label = (
    <>
      Updated
      <Chevron size={11} aria-hidden="true" />
    </>
  )
  if (!column.getCanSort()) {
    return (
      <span className="inline-flex items-center gap-1 whitespace-nowrap text-primary-900" title="Newest first">
        {label}
      </span>
    )
  }
  return (
    <button
      type="button"
      data-testid="sort-updated"
      onClick={() => column.toggleSorting(!newestFirst)}
      title={newestFirst ? 'Show the oldest first' : 'Show the newest first'}
      className="inline-flex items-center gap-1 whitespace-nowrap uppercase text-primary-900 transition hover:text-primary"
    >
      {label}
    </button>
  )
}

/**
 * The list's columns. `listSearch` is the list's own search string, carried on every row's link so
 * the chat it opens can link back to this same tab, search, sort and page.
 */
export function createChatHistoryColumns(listSearch: string): ColumnDef<ChatRow>[] {
  return [
    {
      id: 'chat',
      accessorFn: chatName,
      header: 'Chat',
      enableSorting: false,
      cell: ({ row, getValue }) => {
        const name = getValue<string>()
        return (
          <Link
            to={`/chat/${row.original.id}`}
            state={openedFromChatList(listSearch)}
            data-chat-id={row.original.id}
            // The overlay stretches the link over its row (the row is `relative`), so the row is one
            // target; a control in a later cell has to be raised above it, never put inside it.
            className="flex min-w-0 items-center gap-[9px] after:absolute after:inset-0 focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-inset focus-visible:after:ring-primary/40"
          >
            <KindTile kind={row.original.kind} />
            <span title={name} className="min-w-0 truncate text-[12.5px] font-semibold text-primary-900">
              {name}
            </span>
          </Link>
        )
      },
    },
    {
      id: 'kind',
      accessorKey: 'kind',
      filterFn: 'equals',
      enableSorting: false,
      enableGlobalFilter: false,
    },
    {
      id: 'updated',
      accessorFn: (row) => Date.parse(row.updatedAt) || 0,
      header: ({ column }) => <UpdatedHeader column={column} />,
      sortingFn: 'basic',
      enableGlobalFilter: false,
      cell: ({ row }) => relativeTime(row.original.updatedAt),
      meta: { className: 'w-[96px] whitespace-nowrap text-xs tabular-nums text-neutral' },
    },
  ]
}
