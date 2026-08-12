/**
 * ProjectCard — the open/delete affordances and the F-10 no-nested-interactive invariant.
 *
 * The component is purely presentational (the page injects `onOpen`/`onDelete`), so these
 * render it with plain spies and no router. The point of the test is the *structure*: the
 * card must expose open and delete without nesting one interactive control inside another.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import ProjectCard from '../ProjectCard'
import type { Project } from '../../../utils/projectApi'

const mkProject = (name: string, over: Partial<Project> = {}): Project => ({
  id: 'p1',
  name,
  description: 'A tool',
  appId: null,
  appStatus: null,
  hasRelaunchableSnapshot: null,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  ...over,
})

afterEach(() => cleanup())

describe('ProjectCard', () => {
  it('opens via the title control, without also firing delete', () => {
    const onOpen = vi.fn()
    const onDelete = vi.fn()
    render(<ProjectCard project={mkProject('Roster')} onOpen={onOpen} onDelete={onDelete} />)

    fireEvent.click(screen.getByRole('button', { name: 'Roster' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
    expect(onDelete).not.toHaveBeenCalled()
  })

  it('deletes via the delete control, without also triggering open', () => {
    const onOpen = vi.fn()
    const onDelete = vi.fn()
    render(<ProjectCard project={mkProject('Roster')} onOpen={onOpen} onDelete={onDelete} />)

    fireEvent.click(screen.getByRole('button', { name: /delete roster/i }))
    expect(onDelete).toHaveBeenCalledTimes(1)
    // Delete is a sibling of the open control, so activating it never opens the project.
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('exposes open and delete as two separate button tab stops', () => {
    render(<ProjectCard project={mkProject('Roster')} onOpen={vi.fn()} onDelete={vi.fn()} />)
    const open = screen.getByRole('button', { name: 'Roster' })
    const del = screen.getByRole('button', { name: /delete roster/i })
    // Both are real, natively keyboard-activatable <button>s (Enter/Space for free) — and they
    // are distinct elements, not one wrapping the other.
    expect(open.tagName).toBe('BUTTON')
    expect(del.tagName).toBe('BUTTON')
    expect(open).not.toBe(del)
    expect(open.contains(del)).toBe(false)
  })

  it('does not nest the delete control inside any interactive element (F-10)', () => {
    const { container } = render(
      <ProjectCard project={mkProject('Roster')} onOpen={vi.fn()} onDelete={vi.fn()} />,
    )
    const del = screen.getByRole('button', { name: /delete roster/i })
    // The old `div role="button"` wrapper is gone — nothing in the card claims the button role
    // beyond the two real <button>s themselves.
    expect(container.querySelector('[role="button"]')).toBeNull()
    // And Delete's only <button> ancestor is itself: no interactive element wraps it, so its
    // accessible name can never be absorbed by an outer role="button" (the strict-mode double
    // match that forced the e2e workaround).
    expect(del.parentElement?.closest('button')).toBeNull()
  })

  it('falls back to "Untitled project" for an empty name and still labels delete', () => {
    render(<ProjectCard project={mkProject('')} onOpen={vi.fn()} onDelete={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Untitled project' })).toBeTruthy()
    // The delete label degrades to a generic noun rather than an empty "Delete ".
    expect(screen.getByRole('button', { name: 'Delete project' })).toBeTruthy()
  })
})
