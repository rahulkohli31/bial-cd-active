import { describe, it, expect } from 'vitest'
import * as claudeApi from '../../hooks/useClaudeAPI'

// The old client-side JSX/app-file builder (SYSTEM_PROMPT + buildSystemPrompt) was retired with
// the open-sandbox vibe-coding pivot (U11): apps are now built by the backend orchestrator in an
// open sandbox, not by a client-composed JSX-generation prompt. The planning chat forwards
// ChatPage's own PLANNING/SUMMARIZE system prompt straight through the hook (covered by the
// ChatPage send-path test). This is an inert guard against the old builder silently returning.
describe('the old client-side JSX builder is retired (U11)', () => {
  it('no longer exports buildSystemPrompt or the JSX SYSTEM_PROMPT', () => {
    expect(claudeApi.buildSystemPrompt).toBeUndefined()
    expect(claudeApi.SYSTEM_PROMPT).toBeUndefined()
  })

  it('still exports the live planning-chat surface (the hook must NOT be deleted)', () => {
    expect(typeof claudeApi.useClaudeAPI).toBe('function')
    expect(typeof claudeApi.fetchClaudeStream).toBe('function')
    expect(typeof claudeApi.getContextLimits).toBe('function')
  })
})
