/**
 * The deploy client's PARSE CONTRACT — the half `DeployControl.test.tsx` structurally cannot
 * cover, since it mocks the module and hands the component a ready-made `DeploymentView`, so
 * `toDeploymentView` never runs there.
 *
 * `unpublishedAt` is the second axis separating a live app from one an administrator took
 * down; a parser that drops it renders a green "Live" badge over a dead address — the exact
 * bug the review blocked on. `isLive` is retired and the server now folds it into
 * `publishState`, so this suite pins both that the field survives the parse and that no
 * helper derives from it.
 */
import { describe, it, expect, vi } from 'vitest'
import { getDeployment, startDeploy } from '../deployApi'
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
  // TOTAL — every response shape carries one, so a fixture without it is not a shape the
  // server can produce; `live_current` because this fixture IS a succeeded deploy serving.
  publishState: 'live_current',
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

  it('a takedown and a live deploy are told apart by the SERVER now, not by a predicate', async () => {
    // What `isLive` used to answer, answered where it belongs: the two responses differ in
    // the field itself, not in what the browser computes from them.
    const takenDown = await getDeployment(
      'p1',
      deps(
        vi.fn(async () =>
          ok({ ...BODY, publishState: 'taken_offline', unpublishedAt: '2026-08-12T10:00:00Z' }),
        ),
      ),
    )
    const serving = await getDeployment(
      'p1',
      deps(vi.fn(async () => ok({ ...BODY, publishState: 'live_current', unpublishedAt: null }))),
    )

    expect(takenDown.publishState).toBe('taken_offline')
    expect(takenDown.unpublishedAt).toBe('2026-08-12T10:00:00Z')
    expect(serving.publishState).toBe('live_current')
    expect(serving.unpublishedAt).toBeNull()
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
    approvedAt: null,
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
    // An unrecognised lineage must not be READ as self_publish — the one value authorising
    // the citizen to publish an approved version themselves — so null is the conservative
    // answer: every consumer branches on `=== 'self_publish'`, and "no claim" withholds the
    // affordance rather than granting it.
    //
    // It deliberately does NOT throw: that used to blank the whole Publish card over a field
    // the gate re-decides server-side anyway — strictly worse than declining to claim.
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

  it('carries WHEN it was approved beside WHICH commit was', async () => {
    // The approved states name the date first and mute the build code beside it. The pin
    // alone cannot be rendered as a version row, so the stamp has to survive the parse.
    // Mutation receipt: drop `approvedAt: optionalString(value.approvedAt)` from
    // toApprovalState and this goes red.
    const view = await getDeployment(
      'p1',
      deps(
        vi.fn(async () =>
          ok({
            ...BODY,
            approval: {
              ...APPROVAL,
              status: 'approved',
              approvedCommitSha: 'f9e8d7c6b5a4f9e8d7c6b5a4f9e8d7c6b5a4f9e8',
              approvedAt: '2026-08-20T09:14:00Z',
            },
          }),
        ),
      ),
    )

    expect(view.approval?.approvedAt).toBe('2026-08-20T09:14:00Z')
    expect(view.approval?.approvedCommitSha).toBe('f9e8d7c6b5a4f9e8d7c6b5a4f9e8d7c6b5a4f9e8')
  })
})

/**
 * THE ONE FIELD THE PUBLISH SURFACE BRANCHES ON. Every case here is about the
 * boundary refusing to invent a state: the surface IS this field, so there is no
 * conservative reading of an unrecognised value that is not itself a claim.
 */
describe('getDeployment parses the one publish state, and refuses to guess it', () => {
  const parse = async (over: Record<string, unknown>) =>
    getDeployment('p1', deps(vi.fn(async () => ok({ ...BODY, ...over }))))

  it('parses the drifted-live value with the live version stamp it renders beside', async () => {
    const view = await parse({
      publishState: 'live_newer_work',
      headSha: 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0',
    })

    expect(view.publishState).toBe('live_newer_work')
    expect(view.headSha).toBe('a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0')
    expect(view.finishedAt).toBe('2026-08-12T09:05:00Z')
  })

  it('parses the PLAIN live value, and it equals neither of the other two', async () => {
    // Asserting the inequalities is the point: a parser that collapsed any of these three
    // into another would render one state's sentence over another's — "nothing of yours is
    // waiting" is the exact false reassurance this feature has shipped four times.
    const view = await parse({ publishState: 'live_current' })

    expect(view.publishState).toBe('live_current')
    expect(view.publishState).not.toBe('live_newer_work')
    expect(view.publishState).not.toBe('live_drift_unknown')
  })

  it('parses the could-not-determine value as its own thing, never as plain live', async () => {
    const view = await parse({ publishState: 'live_drift_unknown' })

    expect(view.publishState).toBe('live_drift_unknown')
    expect(view.publishState).not.toBe('live_current')
  })

  it('parses a state with no live version and no approval stamp', async () => {
    const view = await parse({
      publishState: 'draft',
      headSha: null,
      finishedAt: null,
      url: null,
      approval: {
        status: 'draft',
        approvedCommitSha: null,
        approvedAt: null,
        approvalRoute: null,
        rejectionNote: null,
        submittedSha: null,
        submittedAt: null,
      },
    })

    expect(view.publishState).toBe('draft')
    expect(view.headSha).toBeNull()
    expect(view.approval?.approvedCommitSha).toBeNull()
    expect(view.approval?.approvedAt).toBeNull()
  })

  it('throws on an unrecognised value, with a message a person can read', async () => {
    // Mutation receipt: give `toPublishState` a fallback arm — any fallback — and this
    // goes red. There is deliberately no default and no exported escape hatch.
    const err = await parse({ publishState: 'live_probably' }).catch((e: unknown) => e)

    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toMatch(/publish state we could not read/i)
  })

  it('throws when the value is missing entirely — a total field with a hole is a bug', async () => {
    const { publishState: _dropped, ...withoutIt } = BODY
    const err = await getDeployment('p1', deps(vi.fn(async () => ok(withoutIt)))).catch(
      (e: unknown) => e,
    )

    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toMatch(/publish state we could not read/i)
  })

  /**
   * WHY the saved pair is absent, which the pair itself cannot say. Only `never_saved`
   * removes the rail's row, so it is the one value an unrecognised string must never be
   * read as.
   */
  it('★ parses each reason the saved pair can be absent', async () => {
    for (const state of ['saved', 'never_saved', 'store_unconfigured', 'storage_error'] as const) {
      const view = await parse({ savedState: state })
      expect(view.savedState, state).toBe(state)
    }
  })

  it('★ reads an unrecognised or missing reason as NO CLAIM, never as "never saved"', async () => {
    // The conservative reading `toApprovalRoute` already documents, and the direction matters:
    // the one value that DELETES a row must never be reachable by accident, so a server that
    // grows a fifth member fails towards saying too little.
    for (const wire of [{ savedState: 'never_saved_probably' }, { savedState: null }, {}]) {
      const view = await parse(wire)
      expect(view.savedState, JSON.stringify(wire)).toBeNull()
      // Liveness: an unreadable supplementary field must not blank the response it rode on.
      expect(view.publishState, JSON.stringify(wire)).toBe('live_current')
    }
  })
})

const SHA = 'a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0'

describe('startDeploy names the version it is about', () => {
  const started = { outcome: 'started', deploymentId: 'd1', appId: 'a1', status: 'running' }
  const sent = (fetchImpl: ReturnType<typeof vi.fn>): unknown =>
    JSON.parse((fetchImpl.mock.calls[0][1] as { body: string }).body)

  it('sends the reviewed commit with the answers, and nothing else', async () => {
    const fetchImpl = vi.fn(async () => ok(started))
    const answers: DataClassificationAnswers = {
      credentialsSecrets: false,
      healthData: false,
      personalInformation: true,
      financialData: false,
      confidentialBusinessData: false,
      publicData: false,
      notes: 'Traveller names only.',
    }

    await startDeploy('p1', { commitSha: SHA, answers }, deps(fetchImpl))

    expect(sent(fetchImpl)).toEqual({ commitSha: SHA, answers })
  })

  it('sends the approved commit alone, with no answers to invent', async () => {
    const fetchImpl = vi.fn(async () => ok(started))

    await startDeploy('p1', { commitSha: SHA }, deps(fetchImpl))

    expect(sent(fetchImpl)).toEqual({ commitSha: SHA })
  })
})

describe('getDeployment carries the commit the one button republishes', () => {
  it('parses it when the server sets it, and reads its absence as null', async () => {
    const set = await getDeployment(
      'p1',
      deps(vi.fn(async () => ok({ ...BODY, publishState: 'did_not_start', approvedRetryCommit: SHA }))),
    )
    expect(set.approvedRetryCommit).toBe(SHA)

    const absent = await getDeployment('p1', deps(vi.fn(async () => ok(BODY))))
    expect(absent.approvedRetryCommit).toBeNull()
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
      { commitSha: SHA, answers },
      deps(vi.fn(async () => ok({ outcome: 'started', deploymentId: 'd1', appId: 'a1', status: 'running' }))),
    )

    expect(started.outcome).toBe('started')
    expect(started.outcome === 'started' && started.deploymentId).toBe('d1')
  })

  it('parses the 200 ROUTED shape, which carries no deploymentId at all', async () => {
    // The previous parser required `deploymentId` and would have thrown "we could not read"
    // on the routed body — turning the outcome the citizen asked for into a parse error.
    // Mutation receipt: delete the `outcome === 'routed_for_review'` branch in
    // `toDeployOutcome` and this goes red on a thrown ApiError.
    const routed = await startDeploy(
      'p1',
      { commitSha: SHA, answers },
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
      { commitSha: SHA, answers },
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

/**
 * INERTNESS, not coverage. `isLive`, `isRoutedForReview` and `stepLabel` were retired
 * together — three halves of one mistake, the browser re-deciding from parts what the
 * server had already decided. The guarantee they carried MOVED, not vanished: the
 * `failure_code` check is pinned in `backend/tests/api/v1/deploy/test_publish_state.py`,
 * its rendering in `PublishStatusChip.test.tsx`. This only asserts nothing of that shape
 * grows back in this module.
 */
describe('the module derives nothing, and must not start again', () => {
  it('exports no predicate over a deployment field', async () => {
    // Mutation receipt: re-export `isLive` (or any `isX`/`deriveX`/`computeX` over these
    // fields) and this goes red by name. Asserted against the REAL module, not a mock.
    const mod: Record<string, unknown> = await vi.importActual('../deployApi')

    expect(Object.keys(mod).filter((k) => /^(is|derive|compute)[A-Z]/.test(k))).toEqual([])
  })

  it('translates no pipeline phase token into citizen words', async () => {
    // `stepLabel` turned `claimed`/`packing`/`provisioning`/`starting` into the four phrases
    // below. That whole vocabulary is DELETED rather than restyled — while a publish runs
    // the chip just says "Starting up".
    const mod: Record<string, unknown> = await vi.importActual('../deployApi')

    expect(Object.keys(mod)).not.toContain('stepLabel')
    for (const phase of [
      'Getting ready',
      'Packaging your app',
      'Setting up the server',
      'Starting it up',
    ]) {
      expect(JSON.stringify(Object.values(mod))).not.toContain(phase)
    }
  })
})
