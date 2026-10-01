/**
 * The chat list's columns, and the kind marks every surface that names a chat draws.
 *
 * Its own file rather than the admin `columns.tsx`, whose rows are people and limits: a chat row
 * is a kind tile and a title that together are the row's link, then an age, then the row's `⋯`
 * menu — a sibling of the link, never inside it. Rename edits the title in place of that link.
 */
import { useEffect, useId, useRef, useState } from 'react'
import type { Column, ColumnDef } from '@tanstack/react-table'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronUp, MoreHorizontal, Pencil, Trash2 } from 'lucide-react'
import { Input } from '../ui/input'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '../ui/dropdown-menu'
import { ApiError } from '../../utils/apiError'
import { chatKindFor, type ChatKindPresentation } from '../../utils/chatKind'
import { openedFromChatList } from '../../utils/chatHistoryAddress'
import { relativeTime } from '../../utils/relativeTime'
import type { ChatRow } from '../../hooks/useProjectChats'

/** The server's limit on a chat's title, counted after trimming. */
export const CHAT_TITLE_MAX = 120

const NAME_REQUIRED = 'Give the chat a name'
const NAME_TOO_LONG = `Keep the name under ${CHAT_TITLE_MAX} characters`
const RENAME_FAILED = 'Could not rename the chat. Try again.'

/** What a row's menu and its rename editor call back into. */
export interface ChatRowActions {
  /** The chat whose title is being edited, or `null`. */
  editingId: string | null
  startRename: (chat: ChatRow) => void
  /** Settles once the list shows the new title; throws the server's refusal. */
  saveTitle: (chat: ChatRow, title: string) => Promise<void>
  endRename: (chat: ChatRow) => void
  startDelete: (chat: ChatRow) => void
}

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

/** Why a typed title cannot be sent, or `null`. Counted in code points, as the server counts. */
function titleProblem(title: string): string | null {
  if (title === '') return NAME_REQUIRED
  return [...title].length > CHAT_TITLE_MAX ? NAME_TOO_LONG : null
}

function renameFailure(error: unknown): string {
  if (error instanceof ApiError && error.code === 'title_required') return NAME_REQUIRED
  if (error instanceof ApiError && error.code === 'title_too_long') return NAME_TOO_LONG
  return RENAME_FAILED
}

/**
 * The title, editable in its row. Enter saves; Esc and clicking away cancel, and nothing is saved
 * on blur. No optimistic rename: the editor stays, read-only, until the list shows the new title.
 */
function ChatTitleEditor({ chat, actions }: { chat: ChatRow; actions: ChatRowActions }) {
  const hintId = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const [value, setValue] = useState(chat.title)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  // Refs as well as state: a blur can arrive in the same tick as the Enter that started a save.
  const savingRef = useRef(false)
  const finished = useRef(false)

  useEffect(() => {
    const input = inputRef.current
    input?.focus()
    input?.setSelectionRange(input.value.length, input.value.length)
  }, [])

  const finish = () => {
    if (finished.current) return
    finished.current = true
    actions.endRename(chat)
  }

  const save = async () => {
    if (savingRef.current || finished.current) return
    const title = value.trim()
    const problem = titleProblem(title)
    if (problem !== null) {
      setError(problem)
      return
    }
    savingRef.current = true
    setSaving(true)
    setError(null)
    try {
      await actions.saveTitle(chat, title)
      finish()
    } catch (caught) {
      setError(renameFailure(caught))
      savingRef.current = false
      setSaving(false)
    }
  }

  return (
    <div className="min-w-0 flex-1">
      <Input
        ref={inputRef}
        value={value}
        readOnly={saving}
        aria-label="Chat name"
        aria-invalid={error !== null}
        aria-describedby={hintId}
        placeholder={untitledChatName(chatKindFor(chat.kind))}
        onChange={(event) => {
          setValue(event.target.value)
          setError(null)
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault()
            void save()
          } else if (event.key === 'Escape') {
            event.preventDefault()
            if (!savingRef.current) finish()
          }
        }}
        onBlur={() => {
          if (!savingRef.current) finish()
        }}
        className="h-[30px] rounded-md border-primary bg-white px-2.5 py-0 text-[12.5px] font-semibold text-primary-900 shadow-none ring-[3px] ring-primary/15 focus-visible:ring-[3px] focus-visible:ring-primary/15 md:text-[12.5px]"
      />
      <p id={hintId} aria-live="polite" className={`mt-1 text-[10.5px] ${error === null ? 'text-neutral' : 'text-danger'}`}>
        {error ?? 'Enter to save · Esc to cancel'}
      </p>
    </div>
  )
}

const MENU_ITEM = 'gap-2 rounded-sm px-2 py-1.5 text-sm text-primary-900 focus:bg-surface-muted'

/**
 * The row's `⋯` menu: Rename, then Delete…. Always visible, never hover-only, and lifted above the
 * row link's overlay so pressing it never opens the chat.
 */
function ChatRowMenu({ chat, actions }: { chat: ChatRow; actions: ChatRowActions }) {
  const renaming = useRef(false)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          data-chat-menu={chat.id}
          aria-label={`More actions for ${chatName(chat)}`}
          title="More actions"
          className="relative z-10 inline-flex h-[26px] w-[26px] items-center justify-center rounded-lg text-neutral transition hover:bg-canvas-tile hover:text-primary-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 data-[state=open]:bg-canvas-tile data-[state=open]:text-primary-900 narrow:min-h-[44px] narrow:min-w-[44px]"
        >
          <MoreHorizontal size={16} aria-hidden="true" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="min-w-[200px] rounded-md border-bial-border bg-white p-1 shadow-lg"
        // The menu hands focus back to its trigger as it closes; after Rename it belongs in the editor.
        onCloseAutoFocus={(event) => {
          if (!renaming.current) return
          renaming.current = false
          event.preventDefault()
        }}
      >
        <DropdownMenuItem
          className={MENU_ITEM}
          onSelect={() => {
            renaming.current = true
            actions.startRename(chat)
          }}
        >
          <Pencil size={15} aria-hidden="true" />
          Rename
        </DropdownMenuItem>
        <DropdownMenuSeparator className="bg-bial-border" />
        <DropdownMenuItem
          className={`${MENU_ITEM} text-red-700 focus:text-red-700`}
          onSelect={() => actions.startDelete(chat)}
        >
          <Trash2 size={15} aria-hidden="true" />
          Delete…
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
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
export function createChatHistoryColumns(listSearch: string, actions: ChatRowActions): ColumnDef<ChatRow>[] {
  return [
    {
      id: 'chat',
      accessorFn: chatName,
      header: 'Chat',
      enableSorting: false,
      cell: ({ row, getValue }) => {
        const name = getValue<string>()
        if (actions.editingId === row.original.id) {
          return (
            <div className="flex min-w-0 items-start gap-[9px]">
              <KindTile kind={row.original.kind} />
              <ChatTitleEditor chat={row.original} actions={actions} />
            </div>
          )
        }
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
    {
      id: 'actions',
      header: () => <span className="sr-only">Actions</span>,
      enableSorting: false,
      enableGlobalFilter: false,
      cell: ({ row }) => <ChatRowMenu chat={row.original} actions={actions} />,
      meta: { className: 'w-[46px] px-2 text-right narrow:w-[60px]' },
    },
  ]
}
