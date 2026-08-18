/**
 * The deploy client's PARSE CONTRACT — the half `DeployControl.test.tsx` structurally cannot
 * cover, because it mocks the module and hands the component a ready-made `DeploymentView`,
 * so `toDeploymentView` never runs there.
 *
 * `unpublishedAt` is the field that matters: it is the only thing separating a live app from
 * one an administrator took down (`isLive` reads nothing else), so a parser that drops it
 * renders a green "Live" badge over a dead address — the exact bug the review blocked on.
 */
import { describe, it, expect, vi } from 'vitest'
import { getDeployment, isLive } from '../deployApi'

const deps = (fetchImpl: unknown) => ({
  fetchImpl,
  getToken: () => null,
  refresh: vi.fn(),
}) as never

// authFetch peeks a 403 body through res.clone(), so a faked Response must be cloneable.
const res = (init: Record<string, unknown>): Record<string, unknown> => ({
  ...init,
  clone: () => res(init),
})
const ok = (json: unknown) => res({ ok: true, status: 200, json: async () => json })

const BODY = {
  deploymentId: 'd1',
  appId: 'a1',
  status: 'succeeded',
  step: null,
  url: 'https://pub-abc.example/',
  headSha: null,
  failureCode: null,
  failureDetail: null,
  startedAt: '2026-08-12T09:00:00Z',
  finishedAt: '2026-08-12T09:05:00Z',
}

describe('getDeployment parses the takedown axis', () => {
  it('carries unpublishedAt through from the wire', async () => {
    // Mutation receipt: `unpublishedAt: optionalString(body.unpublishedAt)` -> `null` in
    // toDeploymentView and this goes red.
    const fetchImpl = vi.fn(async (_url: string, _init?: unknown) =>
      ok({ ...BODY, unpublishedAt: '2026-08-12T10:00:00Z' }),
    )

    const view = await getDeployment('p1', deps(fetchImpl))

    expect(view.unpublishedAt).toBe('2026-08-12T10:00:00Z')
    expect(fetchImpl.mock.calls[0][0]).toBe('/api/projects/p1/deployment')
  })

  it('a succeeded deploy carrying a takedown is NOT live', async () => {
    // The consequence, pinned end to end: parse + predicate together are what stop the portal
    // offering a clickable link to a container that no longer exists.
    const fetchImpl = vi.fn(async () => ok({ ...BODY, unpublishedAt: '2026-08-12T10:00:00Z' }))

    expect(isLive(await getDeployment('p1', deps(fetchImpl)))).toBe(false)
  })

  it('a succeeded deploy with no takedown is live', async () => {
    const fetchImpl = vi.fn(async () => ok({ ...BODY, unpublishedAt: null }))

    const view = await getDeployment('p1', deps(fetchImpl))

    expect(view.unpublishedAt).toBeNull()
    expect(isLive(view)).toBe(true)
  })

  it('a missing unpublishedAt key reads as null rather than undefined', async () => {
    // The server omits the key entirely for a row that was never taken down; `optionalString`
    // is what normalizes that, and `DeploymentView` declares `string | null`, never undefined.
    const view = await getDeployment('p1', deps(vi.fn(async () => ok(BODY))))

    expect(view.unpublishedAt).toBeNull()
  })
})
