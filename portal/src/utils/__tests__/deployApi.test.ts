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
import { getDeployment, isLive, isRoutedForReview, startDeploy } from '../deployApi'
import { ApiError } from '../apiError'
import type { DataClassificationAnswers } from '../deployApi'

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

describe('getDeployment parses the APPROVAL state riding on the same response', () => {
  const APPROVAL = {
    status: 'pending',
    approvedCommitSha: null,
    approvalRoute: 'self_publish',
    rejectionNote: 'Explain where the vendor key is stored.',
    submittedSha: 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0',
    submittedAt: '2026-08-19T10:00:00Z',
  }

  it('carries the whole lifecycle through from the wire', async () => {
    // This is what makes the two citizen publish surfaces agree without a second call.
    // Mutation receipt: `approval: toApprovalState(body.approval)` -> `null` in
    // toDeploymentView and this goes red.
    const view = await getDeployment(
      'p1',
      deps(vi.fn(async () => ok({ ...BODY, approval: APPROVAL }))),
    )

    expect(view.approval).toEqual(APPROVAL)
  })

  it('reads an absent approval as null — a project with no app yet, not a failure', async () => {
    const view = await getDeployment('p1', deps(vi.fn(async () => ok(BODY))))

    expect(view.approval).toBeNull()
  })

  it('throws on an unknown lifecycle status rather than let it sail past every branch', async () => {
    const call = getDeployment(
      'p1',
      deps(vi.fn(async () => ok({ ...BODY, approval: { ...APPROVAL, status: 'marinating' } }))),
    )
    const err = await call.catch((e: unknown) => e)

    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(500)
  })

  it('answers an unknown approval LINEAGE with null — never with self-publish', async () => {
    // An unrecognised lineage must not be READ as self_publish, because that is the one
    // value authorising the citizen to publish an approved version themselves (P5). Null
    // is the conservative answer: every consumer branches on `=== 'self_publish'`, so
    // "no claim" withholds the affordance rather than granting it.
    //
    // It deliberately does NOT throw. Throwing propagated through the deploy hook's
    // loadError and blanked the citizen's entire Publish card over a field the gate
    // re-decides server-side anyway — a strictly worse failure than declining to claim,
    // and the opposite of the policy the admin client applies to the same wire value.
    const view = await getDeployment(
      'p1',
      deps(vi.fn(async () => ok({ ...BODY, approval: { ...APPROVAL, approvalRoute: 'vibes' } }))),
    )

    expect(view.approval?.approvalRoute).toBeNull()
    // the rest of the card still parses — the point of not throwing
    expect(view.approval?.status).toBe(APPROVAL.status)
    expect(view.deploymentId).toBe('d1')
  })

  it('accepts a null lineage — a never-submitted draft genuinely has none', async () => {
    const view = await getDeployment(
      'p1',
      deps(
        vi.fn(async () =>
          ok({ ...BODY, approval: { ...APPROVAL, status: 'draft', approvalRoute: null } }),
        ),
      ),
    )

    expect(view.approval?.approvalRoute).toBeNull()
  })
})

describe('startDeploy has two success shapes, discriminated by outcome', () => {
  const answers: DataClassificationAnswers = {
    credentialsSecrets: false,
    healthData: false,
    personalInformation: false,
    financialData: false,
    confidentialBusinessData: false,
    publicData: false,
    notes: null,
  }

  it('parses the 202 started shape', async () => {
    const started = await startDeploy(
      'p1',
      { answers },
      deps(vi.fn(async () => ok({ outcome: 'started', deploymentId: 'd1', appId: 'a1', status: 'running' }))),
    )

    expect(started.outcome).toBe('started')
    expect(started.outcome === 'started' && started.deploymentId).toBe('d1')
  })

  it('parses the 200 ROUTED shape, which carries no deploymentId at all', async () => {
    // The pre-U9 parser required `deploymentId` and would have thrown "we could not read"
    // on the routed body — turning the outcome the citizen asked for into a parse error.
    // Mutation receipt: delete the `outcome === 'routed_for_review'` branch in
    // `toDeployOutcome` and this goes red on a thrown ApiError.
    const routed = await startDeploy(
      'p1',
      { answers },
      deps(
        vi.fn(async () =>
          ok({
            outcome: 'routed_for_review',
            appId: 'a1',
            submissionId: 'sub-1',
            commitSha: 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0',
            submittedAt: '2026-08-19T10:00:00Z',
            message: 'Sent to an administrator for review.',
          }),
        ),
      ),
    )

    expect(routed.outcome).toBe('routed_for_review')
    expect(routed.outcome === 'routed_for_review' && routed.message).toBe(
      'Sent to an administrator for review.',
    )
  })

  it('throws when a routed body is missing the version it pinned', async () => {
    const call = startDeploy(
      'p1',
      { answers },
      deps(
        vi.fn(async () =>
          ok({
            outcome: 'routed_for_review',
            appId: 'a1',
            submissionId: 'sub-1',
            submittedAt: '2026-08-19T10:00:00Z',
            message: 'Sent.',
          }),
        ),
      ),
    )
    const err = await call.catch((e: unknown) => e)

    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toMatch(/commitSha/)
  })
})

describe('isRoutedForReview separates a queued version from a broken one', () => {
  it('recognises the routed code', () => {
    expect(isRoutedForReview('routed_for_review')).toBe(true)
  })

  it('does not soften a real pipeline failure, or the retired refusal', () => {
    // The predicate has to DISCRIMINATE. A lookup that answered true for everything would
    // hide genuine build failures behind an informational banner, and would also quietly
    // resurrect the retired dead end as a non-red state.
    expect(isRoutedForReview('acr_build_failed')).toBe(false)
    expect(isRoutedForReview('classification_below_threshold')).toBe(false)
    expect(isRoutedForReview(null)).toBe(false)
    expect(isRoutedForReview(undefined)).toBe(false)
  })
})
