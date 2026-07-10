/**
 * ProjectPicker — the single gate in front of every project-less create entry.
 *
 * There is no Default project, so the picker's job is to make "which project?" a
 * question the user answers rather than a 400 they discover.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

const h = vi.hoisted(() => ({
  listProjects: vi.fn(),
  createProject: vi.fn(),
}))

vi.mock('../../../utils/projectApi', () => ({
  listProjects: h.listProjects,
  createProject: h.createProject,
}))

import ProjectPicker from '../ProjectPicker'
import type { Project } from '../../../utils/projectApi'

const mkProject = (id: string, name: string): Project => ({
  id,
  name,
  description: null,
  appId: null,
  appStatus: null,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
})

beforeEach(() => {
  vi.clearAllMocks()
  h.listProjects.mockResolvedValue({ items: [mkProject('p1', 'VIP Movement'), mkProject('p2', 'Gate Ops')], nextCursor: null, hasMore: false })
})
afterEach(() => cleanup())

const confirmButton = () => screen.getByRole('button', { name: 'Continue' })

describe('ProjectPicker', () => {
  it('lists the caller’s projects', async () => {
    render(<ProjectPicker onClose={vi.fn()} onPick={vi.fn()} />)
    expect(await screen.findByText('VIP Movement')).toBeTruthy()
    expect(screen.getByText('Gate Ops')).toBeTruthy()
  })

  it('keeps confirm disabled until a project is selected', async () => {
    const onPick = vi.fn()
    render(<ProjectPicker onClose={vi.fn()} onPick={onPick} />)
    await screen.findByText('VIP Movement')

    expect(confirmButton().hasAttribute('disabled')).toBe(true)
    fireEvent.click(confirmButton())
    expect(onPick).not.toHaveBeenCalled()

    fireEvent.click(screen.getByText('VIP Movement'))
    expect(confirmButton().hasAttribute('disabled')).toBe(false)
  })

  it('hands the selected project to onPick', async () => {
    const onPick = vi.fn()
    render(<ProjectPicker onClose={vi.fn()} onPick={onPick} />)
    await screen.findByText('Gate Ops')
    fireEvent.click(screen.getByText('Gate Ops'))
    fireEvent.click(confirmButton())
    expect(onPick).toHaveBeenCalledWith(expect.objectContaining({ id: 'p2', name: 'Gate Ops' }))
  })

  it('creates a project inline and picks it without a second confirm', async () => {
    const onPick = vi.fn()
    h.createProject.mockResolvedValue(mkProject('p3', 'Baggage Recon'))
    render(<ProjectPicker onClose={vi.fn()} onPick={onPick} />)
    await screen.findByText('VIP Movement')

    fireEvent.click(screen.getByRole('button', { name: /new project/i }))
    fireEvent.change(await screen.findByPlaceholderText(/VIP Movement Tracker/i), { target: { value: 'Baggage Recon' } })
    fireEvent.click(screen.getByRole('button', { name: /create project/i }))

    await waitFor(() => expect(onPick).toHaveBeenCalledWith(expect.objectContaining({ id: 'p3' })))
  })

  it('offers creation, not a dead end, when the user has no projects', async () => {
    h.listProjects.mockResolvedValue({ items: [], nextCursor: null, hasMore: false })
    render(<ProjectPicker onClose={vi.fn()} onPick={vi.fn()} />)
    expect(await screen.findByText(/don’t have a project yet/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /new project/i })).toBeTruthy()
    expect(confirmButton().hasAttribute('disabled')).toBe(true)
  })

  it('surfaces a load failure instead of showing an empty list', async () => {
    h.listProjects.mockRejectedValue(new Error('Failed to load projects (500).'))
    render(<ProjectPicker onClose={vi.fn()} onPick={vi.fn()} />)
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText(/Failed to load projects/)).toBeTruthy()
  })
})
