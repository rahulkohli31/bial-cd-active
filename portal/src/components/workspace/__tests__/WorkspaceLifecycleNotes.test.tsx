/**
 * WHAT THE PLATFORM OWES A CITIZEN WHOSE APP NOW LIVES AND DIES WITHOUT ASKING THEM.
 *
 * The exit prompts are gone and the start control is gone; opening a project starts its app and
 * leaving takes it away. Two facts a screen cannot infer from that, and both are stated here: the
 * container has a ceiling nothing postpones, and an automatic write-back can be refused — in
 * which case the app comes back from the citizen's own saved version and looks, from the screen,
 * exactly like an ordinary reopen.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import WorkspaceLifecycleNotes from '../WorkspaceLifecycleNotes'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

const inMinutes = (n: number) => new Date(Date.now() + n * 60_000).toISOString()

describe('the closing-soon note', () => {
  it('names the time the app closes when the ceiling is near', () => {
    render(<WorkspaceLifecycleNotes drainingAt={inMinutes(10)} writeBackRefusedAt={null} />)

    expect(screen.getByTestId('closing-soon-note').textContent).toMatch(/closes at/i)
  })

  it('says NOTHING about a ceiling that is hours away', () => {
    // Every container has one. Announcing it on arrival turns a routine fact into a warning, and
    // a screen that always warns is a screen nobody reads.
    render(<WorkspaceLifecycleNotes drainingAt={inMinutes(115)} writeBackRefusedAt={null} />)

    expect(screen.queryByTestId('closing-soon-note')).toBeNull()
  })

  it('★ says nothing at all when no ceiling applies', () => {
    // `null` MEANS NO CEILING, NEVER "SOON". A screen that read it as imminent would announce a
    // collection that is not coming — and the flag ships off, so that is the ordinary case.
    render(<WorkspaceLifecycleNotes drainingAt={null} writeBackRefusedAt={null} />)

    expect(screen.queryByTestId('workspace-lifecycle-notes')).toBeNull()
  })

  it('says nothing when the instant is not readable as one', () => {
    render(<WorkspaceLifecycleNotes drainingAt="not-a-date" writeBackRefusedAt={null} />)

    expect(screen.queryByTestId('closing-soon-note')).toBeNull()
  })
})

describe('the refused write-back note', () => {
  it('★ states the refusal, where the work went, and what to do about it', () => {
    // THE SENTENCE THAT MAKES REMOVING THE EXIT PROMPTS HONEST. Without it, a citizen whose
    // write-back was refused reopens their app, finds older work, and has nothing on screen to
    // tell them why or that the newer tree still exists.
    render(<WorkspaceLifecycleNotes drainingAt={null} writeBackRefusedAt="2026-09-17T22:14:00Z" />)

    const note = screen.getByTestId('writeback-refused-note').textContent ?? ''
    expect(note).toMatch(/could not be saved back/i)
    expect(note).toMatch(/last saved version/i)
    expect(note).toMatch(/set aside/i)
  })

  it('is stated even when the ceiling is far away — they are independent facts', () => {
    render(<WorkspaceLifecycleNotes drainingAt={inMinutes(600)} writeBackRefusedAt="2026-09-17T22:14:00Z" />)

    expect(screen.getByTestId('writeback-refused-note')).toBeTruthy()
    expect(screen.queryByTestId('closing-soon-note')).toBeNull()
  })

  it('announces politely — neither of these interrupts anything', () => {
    render(<WorkspaceLifecycleNotes drainingAt={null} writeBackRefusedAt="2026-09-17T22:14:00Z" />)

    expect(screen.getByTestId('workspace-lifecycle-notes').getAttribute('aria-live')).toBe('polite')
  })
})
