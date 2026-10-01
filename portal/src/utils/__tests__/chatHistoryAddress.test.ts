import { describe, it, expect } from 'vitest'
import {
  chatListOpenedFrom,
  chatListSearch,
  chatReturnedFrom,
  isChatHistoryPath,
  readChatListQuery,
} from '../chatHistoryAddress'

const read = (search: string) => readChatListQuery(new URLSearchParams(search))

describe('readChatListQuery', () => {
  const fresh = { kind: 'all', q: '', sort: 'newest', page: 1 }

  it('reads missing params as the fresh list', () => {
    expect(read('')).toEqual(fresh)
  })

  it('reads every param that is present', () => {
    expect(read('kind=build&q=fuel&sort=oldest&page=3')).toEqual({
      kind: 'build',
      q: 'fuel',
      sort: 'oldest',
      page: 3,
    })
    expect(read('kind=plan').kind).toBe('plan')
  })

  it('falls back to all for an unknown kind', () => {
    expect(read('kind=zzz').kind).toBe('all')
  })

  it('falls back to newest for an unknown sort', () => {
    expect(read('sort=up').sort).toBe('newest')
  })

  it.each([['page=0'], ['page=-3'], ['page=abc'], ['page=']])('falls back to page 1 for %s', (search) => {
    expect(read(search).page).toBe(1)
  })

  it('truncates a fractional page the way parseInt does', () => {
    expect(read('page=2.5').page).toBe(2)
  })
})

describe('chatListSearch', () => {
  it('leaves every default out, so the fresh list is the bare address', () => {
    expect(chatListSearch({ kind: 'all', q: '', sort: 'newest', page: 1 })).toBe('')
  })

  it('writes every non-default value', () => {
    expect(chatListSearch({ kind: 'plan', q: 'fuel', sort: 'oldest', page: 4 })).toBe(
      '?kind=plan&q=fuel&sort=oldest&page=4',
    )
  })

  it('round-trips a search holding spaces and an ampersand', () => {
    const query = { kind: 'build', q: 'fuel & gates', sort: 'oldest', page: 2 } as const
    const search = chatListSearch(query)
    expect(search).toBe('?kind=build&q=fuel+%26+gates&sort=oldest&page=2')
    expect(read(search)).toEqual(query)
  })
})

describe('isChatHistoryPath', () => {
  it.each(['/projects/p/chats', '/projects/p/chats/'])('accepts %s', (path) => {
    expect(isChatHistoryPath(path)).toBe(true)
  })

  it.each(['/projects/p', '/projects/p/chats/x', '/projects//chats', '/chat/c'])(
    'rejects %s',
    (path) => {
      expect(isChatHistoryPath(path)).toBe(false)
    },
  )
})

describe.each([
  ['chatListOpenedFrom', chatListOpenedFrom, 'chatList', '?kind=plan'],
  ['chatReturnedFrom', chatReturnedFrom, 'returnedFrom', 'chat-7'],
] as const)('%s', (_name, read, key, value) => {
  it.each([[null], [undefined], [5], [{ [key]: 5 }], [{}]])('reads %j as nothing carried', (state) => {
    expect(read(state)).toBeNull()
  })

  it('reads a string value back', () => {
    expect(read({ [key]: value })).toBe(value)
  })

  it('reads an empty string as carried', () => {
    expect(read({ [key]: '' })).toBe('')
  })
})
