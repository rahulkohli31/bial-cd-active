/**
 * SharedProjectCard — the recipient's row on "Shared with me": name, description, and
 * "Shared by {name}" attribution, display name only, never email. Shipped with zero tests
 * before this file.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

import SharedProjectCard from '../SharedProjectCard'
import type { SharedProject } from '../../../utils/sharingApi'

const mkProject = (over: Partial<SharedProject> = {}): SharedProject => ({
  projectId: 'p1',
  projectName: 'Gate Pass Log',
  projectDescription: 'Tracks VIP movement requests airside.',
  sharedByDisplayName: 'Priya K',
  sharedAt: new Date().toISOString(),
  ...over,
})

afterEach(() => cleanup())

describe('SharedProjectCard', () => {
  it('opens via the title control', () => {
    const onOpen = vi.fn()
    render(<SharedProjectCard project={mkProject()} onOpen={onOpen} />)
    fireEvent.click(screen.getByRole('button', { name: 'Gate Pass Log' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('names who shared it, by display name', () => {
    render(<SharedProjectCard project={mkProject({ sharedByDisplayName: 'Priya K' })} onOpen={vi.fn()} />)
    expect(screen.getByText('Shared by Priya K')).toBeTruthy()
  })

  it('falls back to "a colleague" when the sharer has no display name', () => {
    render(<SharedProjectCard project={mkProject({ sharedByDisplayName: null })} onOpen={vi.fn()} />)
    expect(screen.getByText('Shared by a colleague')).toBeTruthy()
  })

  it('never renders an email address', () => {
    render(<SharedProjectCard project={mkProject()} onOpen={vi.fn()} />)
    expect(screen.queryByText(/@/)).toBeNull()
  })

  it('shows the description when present', () => {
    render(
      <SharedProjectCard
        project={mkProject({ projectDescription: 'Tracks VIP movement requests airside.' })}
        onOpen={vi.fn()}
      />,
    )
    expect(screen.getByText('Tracks VIP movement requests airside.')).toBeTruthy()
  })

  it('shows a plain placeholder when there is no description, not a blank space', () => {
    render(<SharedProjectCard project={mkProject({ projectDescription: null })} onOpen={vi.fn()} />)
    expect(screen.getByText('No description yet')).toBeTruthy()
  })

  it('treats a whitespace-only description the same as none', () => {
    render(<SharedProjectCard project={mkProject({ projectDescription: '   ' })} onOpen={vi.fn()} />)
    expect(screen.getByText('No description yet')).toBeTruthy()
  })

  it('falls back to "Untitled project" for an empty name', () => {
    render(<SharedProjectCard project={mkProject({ projectName: '' })} onOpen={vi.fn()} />)
    expect(screen.getByText('Untitled project')).toBeTruthy()
  })

  it('exposes no delete affordance — a recipient cannot delete someone else\'s project', () => {
    render(<SharedProjectCard project={mkProject()} onOpen={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /delete/i })).toBeNull()
  })
})
