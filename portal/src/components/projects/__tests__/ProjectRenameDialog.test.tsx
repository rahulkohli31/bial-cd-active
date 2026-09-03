/**
 * RENAMING A PROJECT — the guards, kept where the control moved to (plan 002, U2).
 *
 * These assertions lived in `ProjectPage.test.tsx`, against the pencil in the rail's header. That
 * header is surrendered to the shell's toolbar row and the editor is this dialog, so the guards
 * come here rather than going quiet with the markup they used to describe.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

const h = vi.hoisted(() => ({ patchProject: vi.fn() }))
vi.mock('../../../utils/projectApi', () => ({ patchProject: h.patchProject }))

import ProjectRenameDialog from '../ProjectRenameDialog'
import { ApiError } from '../../../utils/apiError'
import type { Project } from '../../../utils/projectApi'

const PROJECT = {
  id: 'p1',
  name: 'VIP Movement',
  description: '',
  appId: null,
  appStatus: null,
  hasRelaunchableSnapshot: false,
} as unknown as Project

beforeEach(() => {
  vi.clearAllMocks()
  h.patchProject.mockResolvedValue({ ...PROJECT, name: 'Renamed' })
})
afterEach(() => cleanup())

function renderDialog(onProjectUpdate = vi.fn(), onClose = vi.fn()) {
  render(<ProjectRenameDialog project={PROJECT} onProjectUpdate={onProjectUpdate} onClose={onClose} />)
  return { onProjectUpdate, onClose, input: screen.getByRole('textbox', { name: /project name/i }) }
}

describe('renaming a project', () => {
  it('blocks "" and "   " before any request fires', async () => {
    const { input } = renderDialog()

    fireEvent.change(input, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.patchProject).not.toHaveBeenCalled()
    expect(await screen.findByRole('alert')).toBeTruthy()

    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.patchProject).not.toHaveBeenCalled()
  })

  it('sends the trimmed name and hands the updated project back', async () => {
    const { onProjectUpdate, onClose, input } = renderDialog()
    fireEvent.change(input, { target: { value: '  Renamed  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(h.patchProject).toHaveBeenCalledWith('p1', { name: 'Renamed' }))
    await waitFor(() => expect(onProjectUpdate).toHaveBeenCalledWith(expect.objectContaining({ name: 'Renamed' })))
    expect(onClose).toHaveBeenCalled()
  })

  it('closes without a request when the name did not change', () => {
    const { onClose } = renderDialog()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(h.patchProject).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalled()
  })

  it('says why a rename failed, and stays open so the typed name is not lost', async () => {
    h.patchProject.mockRejectedValue(new ApiError('That name is already taken.', 409))
    const { onClose, input } = renderDialog()
    fireEvent.change(input, { target: { value: 'Taken' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'That name is already taken.')
    expect(onClose).not.toHaveBeenCalled()
    // Paired with a liveness check: an "it did not close" assertion passes just as happily when
    // the component crashed and rendered nothing at all.
    expect(screen.getByRole('textbox', { name: /project name/i })).toHaveProperty('value', 'Taken')
  })

  it('renders no control with a real disabled attribute', () => {
    renderDialog()
    for (const el of screen.getAllByRole('button')) expect(el.hasAttribute('disabled')).toBe(false)
  })
})
