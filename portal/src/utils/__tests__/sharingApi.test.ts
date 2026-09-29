import { describe, it, expect, vi } from 'vitest'
import { colleagueKey, listProjectShares, searchColleagues, shareProject } from '../sharingApi'
import type { Colleague } from '../sharingApi'

const jsonResponse = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const fetchReturning = (status: number, body: unknown) =>
  vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => jsonResponse(status, body))

const deps = (fetchImpl: typeof fetch) => ({ fetchImpl, getToken: () => null, refresh: async () => false })

const parseBody = (init: RequestInit | undefined): unknown => JSON.parse(String(init?.body))

const USER_ID = '0192f0a0-0000-7000-8000-000000000001'
const DIRECTORY_ID = '5b1c7d2e-3f4a-4b5c-8d6e-7f8091a2b3c4'

const shareRow = {
  id: 's1',
  sharedWithUserId: USER_ID,
  sharedWithDisplayName: 'Priya Sharma',
  sharedWithEmailLocalPart: 'priya.sharma',
  signedIn: false,
  createdAt: '2026-09-29T10:00:00Z',
}

describe('searchColleagues', () => {
  it('reads a user hit and a directory hit as the two kinds, each keyed by its own id', async () => {
    const fetchImpl = fetchReturning(200, {
      colleagues: [
        { id: USER_ID, directoryId: null, displayName: 'Asha Rao', emailLocalPart: 'asha.rao', signedIn: true },
        { id: null, directoryId: DIRECTORY_ID, displayName: 'Priya Sharma', emailLocalPart: 'priya.sharma', signedIn: false },
      ],
    })
    const colleagues = await searchColleagues('ash', deps(fetchImpl))
    expect(colleagues).toEqual([
      { kind: 'user', id: USER_ID, displayName: 'Asha Rao', emailLocalPart: 'asha.rao', signedIn: true },
      { kind: 'directory', directoryId: DIRECTORY_ID, displayName: 'Priya Sharma', emailLocalPart: 'priya.sharma', signedIn: false },
    ])
    expect(colleagues.map(colleagueKey)).toEqual([USER_ID, DIRECTORY_ID])
  })

  it('drops a hit carrying both ids, neither id, or an id that is not a string', async () => {
    const fetchImpl = fetchReturning(200, {
      colleagues: [
        { id: USER_ID, directoryId: DIRECTORY_ID, displayName: 'Both', emailLocalPart: 'both', signedIn: true },
        { id: null, directoryId: null, displayName: 'Neither', emailLocalPart: 'neither', signedIn: true },
        { id: 42, directoryId: DIRECTORY_ID, displayName: 'Numeric', emailLocalPart: 'numeric', signedIn: false },
        { id: '', directoryId: null, displayName: 'Empty', emailLocalPart: 'empty', signedIn: true },
        { id: USER_ID, directoryId: null, displayName: 'Kept', emailLocalPart: 'kept', signedIn: true },
      ],
    })
    const colleagues = await searchColleagues('kep', deps(fetchImpl))
    expect(colleagues.map((c) => c.displayName)).toEqual(['Kept'])
  })
})

describe('shareProject', () => {
  it('posts a user hit by its user id alone', async () => {
    const fetchImpl = fetchReturning(200, { ...shareRow, signedIn: true })
    const colleague: Colleague = { kind: 'user', id: USER_ID, displayName: 'Asha Rao', emailLocalPart: 'asha.rao', signedIn: true }
    await shareProject('p1', colleague, deps(fetchImpl))
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/projects/p1:share')
    expect(parseBody(fetchImpl.mock.calls[0][1])).toEqual({ sharedWithUserId: USER_ID })
  })

  it('posts a directory hit by its directory id alone and reads back the created user as not signed in', async () => {
    const fetchImpl = fetchReturning(200, shareRow)
    const colleague: Colleague = {
      kind: 'directory',
      directoryId: DIRECTORY_ID,
      displayName: 'Priya Sharma',
      emailLocalPart: 'priya.sharma',
      signedIn: false,
    }
    const share = await shareProject('p1', colleague, deps(fetchImpl))
    expect(parseBody(fetchImpl.mock.calls[0][1])).toEqual({ directoryId: DIRECTORY_ID })
    expect(share).toMatchObject({ sharedWithUserId: USER_ID, signedIn: false })
  })
})

describe('listProjectShares', () => {
  it('carries whether each colleague has ever signed in', async () => {
    const fetchImpl = fetchReturning(200, { shares: [shareRow, { ...shareRow, id: 's2', signedIn: true }] })
    const shares = await listProjectShares('p1', deps(fetchImpl))
    expect(shares.map((s) => s.signedIn)).toEqual([false, true])
  })
})
