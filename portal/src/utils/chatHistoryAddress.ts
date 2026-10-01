/**
 * The chat list's address: its route, the list state its query carries, and the router state a
 * chat opened from the list travels with.
 *
 * The list is a route of its own, so the rail mode, the History button's pressed state and the
 * browser's Back all read one address. A chat keeps its flat `/chat/:id`, so the query of the list
 * it was opened from rides router state instead, and the chat's "All chats" link reads it back.
 */

export type ChatKindTab = 'all' | 'plan' | 'build'
export type ChatSort = 'newest' | 'oldest'

export interface ChatListQuery {
  kind: ChatKindTab
  q: string
  sort: ChatSort
  /** One-based. */
  page: number
}

const LIST_PATH = /^\/projects\/[^/]+\/chats\/?$/

export function chatHistoryPath(projectId: string): string {
  return `/projects/${projectId}/chats`
}

export function isChatHistoryPath(pathname: string): boolean {
  return LIST_PATH.test(pathname)
}

/** The list state a query string names. Anything unreadable falls back to the fresh list. */
export function readChatListQuery(params: URLSearchParams): ChatListQuery {
  const kind = params.get('kind')
  const page = Number.parseInt(params.get('page') ?? '', 10)
  return {
    kind: kind === 'plan' || kind === 'build' ? kind : 'all',
    q: params.get('q') ?? '',
    sort: params.get('sort') === 'oldest' ? 'oldest' : 'newest',
    page: Number.isFinite(page) && page > 0 ? page : 1,
  }
}

/** `query` as a search string, defaults left out, so the fresh list is the bare address. */
export function chatListSearch(query: ChatListQuery): string {
  const params = new URLSearchParams()
  if (query.kind !== 'all') params.set('kind', query.kind)
  if (query.q !== '') params.set('q', query.q)
  if (query.sort !== 'newest') params.set('sort', query.sort)
  if (query.page > 1) params.set('page', String(query.page))
  const search = params.toString()
  return search === '' ? '' : `?${search}`
}

/** Router state for a chat opened from the list: the list's search string to return to. */
export function openedFromChatList(listSearch: string): { chatList: string } {
  return { chatList: listSearch }
}

/** The list search a chat was opened from, or `null` when it was opened any other way. */
export function chatListOpenedFrom(state: unknown): string | null {
  const carried = (state as { chatList?: unknown } | null)?.chatList
  return typeof carried === 'string' ? carried : null
}

/** Router state for the way back to the list from a chat: which row to put focus on. */
export function returningFromChat(chatId: string): { returnedFrom: string } {
  return { returnedFrom: chatId }
}

export function chatReturnedFrom(state: unknown): string | null {
  const carried = (state as { returnedFrom?: unknown } | null)?.returnedFrom
  return typeof carried === 'string' ? carried : null
}
