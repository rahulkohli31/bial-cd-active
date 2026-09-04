import { describe, it, expect } from 'vitest'
import * as builderHistory from '../builderHistory'

describe('builderHistory', () => {
  /**
   * A GUARD, not deleted coverage (plan 001, unit 6). This file used to assert that `createBuild`
   * POSTed `{ id, projectId, kind: 'build', title, context }` to `/api/conversations` a full round
   * trip before the first turn. That route's only workspace awareness was a project-ownership
   * check, so a first message the workspace then refused left a real, titled, empty build chat
   * behind it. The row's parentage rides the turn itself now, and the wire assertion this file
   * made — the create body carries `kind: 'build'` — lives in `turnStreamApi.test.ts`, against
   * the request that actually carries it.
   *
   * What is left here is a READ store. Re-adding a create verb would rebuild the round trip
   * R-18 removed, so its absence is asserted rather than left silent.
   */
  it('exports no create verb — a build row is created by its first turn', () => {
    expect('createBuild' in builderHistory).toBe(false)
    expect(builderHistory.createBuild).toBeUndefined()
    // Paired with a liveness assertion so the absence above cannot false-green on a module
    // that failed to load anything at all.
    expect(typeof builderHistory.loadBuilds).toBe('function')
    expect(typeof builderHistory.getBuild).toBe('function')
    expect(typeof builderHistory.deriveTitle).toBe('function')
  })
})
