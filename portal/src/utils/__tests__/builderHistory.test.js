import { describe, it, expect } from 'vitest'
import * as builderHistory from '../builderHistory'

describe('builderHistory', () => {
  /**
   * A GUARD, not deleted coverage. `createBuild` used to POST to `/api/conversations` from
   * HERE, and its removal is what this asserts — not that nothing creates a row.
   *
   * SOMETHING DOES AGAIN, just not this store: the send path calls
   * `conversationApi.createConversation` a round trip before the first turn, because an upload
   * has to name a conversation the server has already written. It lives there because that is
   * the only place that knows a chat is about to receive its first file; a create verb on a
   * READ store invites creating a row merely by listing or opening one.
   *
   * So the absence asserted below is still the right shape, and re-adding a create verb here is
   * a decision rather than an accident.
   */
  it('exports no create verb — the send path creates the row, not this store', () => {
    expect('createBuild' in builderHistory).toBe(false)
    expect(builderHistory.createBuild).toBeUndefined()
    // Paired with a liveness assertion so the absence above cannot false-green on a module
    // that failed to load anything at all.
    expect(typeof builderHistory.loadBuilds).toBe('function')
    expect(typeof builderHistory.getBuild).toBe('function')
    expect(typeof builderHistory.deriveTitle).toBe('function')
  })
})
