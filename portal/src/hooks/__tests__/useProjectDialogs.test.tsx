/**
 * The Settings and Share doors both workspace surfaces publish to the toolbar's `⋯` menu.
 *
 * The three dialogs are stubbed: each has its own suite, and what this file pins is which one
 * opens, for which project, and where each of its exits leads.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { useProjectDialogs } from '../useProjectDialogs'
import type { Project } from '../../utils/projectApi'

const h = vi.hoisted(() => ({ deleteProject: vi.fn() }))

vi.mock('../../utils/projectApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/projectApi')>()),
  deleteProject: h.deleteProject,
}))
vi.mock('../../components/projects/AppSettingsDialog', () => ({
  default: ({ project, onProjectUpdate, onClose, onDelete }: {
    project: Project
    onProjectUpdate: (project: Project) => void
    onClose: () => void
    onDelete: () => void
  }) => (
    <div role="dialog" aria-label={`Settings for ${project.name}`}>
      <button type="button" onClick={() => onProjectUpdate({ ...project, name: 'Renamed' })}>rename</button>
      <button type="button" onClick={onDelete}>delete</button>
      <button type="button" onClick={onClose}>close</button>
    </div>
  ),
}))
vi.mock('../../components/projects/ProjectDeleteDialog', () => ({
  default: ({ project, onClose, onConfirm }: {
    project: Project
    onClose: () => void
    onConfirm: (remark: string) => Promise<void>
  }) => (
    <div role="dialog" aria-label={`Delete ${project.name}`}>
      <button type="button" onClick={() => void onConfirm('No longer needed')}>confirm</button>
      <button type="button" onClick={onClose}>cancel</button>
    </div>
  ),
}))
vi.mock('../../components/projects/SharePanel', () => ({
  default: ({ projectId, projectName, onClose }: { projectId: string; projectName: string; onClose: () => void }) => (
    <div role="dialog" aria-label={`Share ${projectName}`} data-project={projectId}>
      <button type="button" onClick={onClose}>close</button>
    </div>
  ),
}))

const PROJECT: Project = {
  id: 'p1',
  name: 'Visitor Log',
  description: null,
  appId: null,
  appStatus: null,
  hasRelaunchableSnapshot: null,
  hasSavedSnapshot: null,
  isServing: false,
  isPublishing: false,
  createdAt: '2026-09-01T00:00:00Z',
  updatedAt: '2026-09-01T00:00:00Z',
  access: 'owner',
}

function Surface({ project, onProjectUpdate }: { project: Project | null; onProjectUpdate: (p: Project) => void }) {
  const { settings, share, dialogs } = useProjectDialogs(project, onProjectUpdate)
  return (
    <div data-testid="surface">
      {settings && <button type="button" onClick={settings}>Settings…</button>}
      {share && <button type="button" onClick={share}>Share…</button>}
      {dialogs}
    </div>
  )
}

function renderSurface(project: Project | null, onProjectUpdate = vi.fn()) {
  return render(
    <MemoryRouter initialEntries={['/chat/c1']}>
      <Routes>
        <Route path="/chat/:chatId" element={<Surface project={project} onProjectUpdate={onProjectUpdate} />} />
        <Route path="/projects" element={<div data-testid="projects-list" />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  h.deleteProject.mockResolvedValue(undefined)
})
afterEach(() => cleanup())

describe('the Settings and Share doors', () => {
  it('offers neither door, and no dialog, before the project has loaded', () => {
    renderSurface(null)

    expect(screen.getByTestId('surface')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Settings…' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Share…' })).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('opens the settings dialog for the loaded project, and closes it', () => {
    renderSurface(PROJECT)

    fireEvent.click(screen.getByRole('button', { name: 'Settings…' }))
    expect(screen.getByRole('dialog', { name: 'Settings for Visitor Log' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('button', { name: 'Settings…' })).toBeTruthy()
  })

  it('opens the share dialog for the loaded project, and closes it', () => {
    renderSurface(PROJECT)

    fireEvent.click(screen.getByRole('button', { name: 'Share…' }))
    expect(screen.getByRole('dialog', { name: 'Share Visitor Log' }).getAttribute('data-project')).toBe('p1')

    fireEvent.click(screen.getByRole('button', { name: 'close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('button', { name: 'Share…' })).toBeTruthy()
  })

  it('hands a change made in settings to the owner of the project', () => {
    const onProjectUpdate = vi.fn()
    renderSurface(PROJECT, onProjectUpdate)

    fireEvent.click(screen.getByRole('button', { name: 'Settings…' }))
    fireEvent.click(screen.getByRole('button', { name: 'rename' }))

    expect(onProjectUpdate).toHaveBeenCalledWith({ ...PROJECT, name: 'Renamed' })
  })

  it('swaps settings for the delete confirmation, and a confirmed delete lands on the list', async () => {
    renderSurface(PROJECT)

    fireEvent.click(screen.getByRole('button', { name: 'Settings…' }))
    fireEvent.click(screen.getByRole('button', { name: 'delete' }))
    expect(screen.getAllByRole('dialog').map((d) => d.getAttribute('aria-label'))).toEqual(['Delete Visitor Log'])

    fireEvent.click(screen.getByRole('button', { name: 'confirm' }))

    expect(await screen.findByTestId('projects-list')).toBeTruthy()
    expect(h.deleteProject).toHaveBeenCalledWith('p1', 'No longer needed')
  })

  it('stays where it is when the delete is cancelled', async () => {
    renderSurface(PROJECT)

    fireEvent.click(screen.getByRole('button', { name: 'Settings…' }))
    fireEvent.click(screen.getByRole('button', { name: 'delete' }))
    fireEvent.click(screen.getByRole('button', { name: 'cancel' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByTestId('surface')).toBeTruthy()
    expect(h.deleteProject).not.toHaveBeenCalled()
  })
})
