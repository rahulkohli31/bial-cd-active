/**
 * The settings dialog — the surface every scattered control drains into.
 *
 * TWO THINGS ARE WORTH MORE THAN THE REST HERE. Delete must exist in exactly ONE place in the
 * product, reached in two deliberate steps, because it is the only irreversible action a citizen
 * has; and a failed save must keep what they typed, because the moment a save fails is the moment
 * their text is least replaceable.
 *
 * EVERY TAB THE DIALOG OFFERS HAS A DESTINATION, and each is asserted rather than assumed: a tab
 * rendering a blank panel is a control lying about having one. Two of them read the server, and
 * neither may read it until it is chosen — a dialog that fired both on open would spend two
 * requests per application a citizen glances at the name of.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor, act } from '@testing-library/react'
import { MotionGlobalConfig } from 'motion/react'

const h = vi.hoisted(() => ({
  patchProject: vi.fn(),
  getDeployment: vi.fn(),
  listShares: vi.fn(),
  listProjectConnectors: vi.fn(),
  searchColleagues: vi.fn(),
  shareProject: vi.fn(),
  revokeShare: vi.fn(),
}))

vi.mock('../../../utils/projectApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  patchProject: h.patchProject,
}))
vi.mock('../../../utils/deployApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getDeployment: h.getDeployment,
}))
vi.mock('../../../utils/connectorApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listProjectConnectors: h.listProjectConnectors,
}))
vi.mock('../../../utils/sharingApi', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listShares: h.listShares,
  searchColleagues: h.searchColleagues,
  shareProject: h.shareProject,
  revokeShare: h.revokeShare,
}))
// The description editor has a suite of its own; what matters here is that the tab MOUNTS it
// rather than re-implementing a second description field.
vi.mock('../ProjectDescriptionEditor', () => ({
  default: () => <div data-testid="description-editor" />,
}))

import AppSettingsDialog from '../AppSettingsDialog'
import type { Project } from '../../../utils/projectApi'

MotionGlobalConfig.skipAnimations = true

const PROJECT: Project = {
  id: 'p1',
  name: 'Visitor Log',
  description: 'A short description',
  appId: null,
  appStatus: null,
  hasRelaunchableSnapshot: null,
  hasSavedSnapshot: null,
  isServing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
}

function open(over: Partial<React.ComponentProps<typeof AppSettingsDialog>> = {}) {
  const props = {
    project: PROJECT,
    onProjectUpdate: vi.fn(),
    onClose: vi.fn(),
    onDelete: vi.fn(),
    ...over,
  }
  render(<AppSettingsDialog {...props} />)
  return props
}

const settle = () => act(async () => { await new Promise((r) => setTimeout(r, 0)) })

beforeEach(() => {
  vi.clearAllMocks()
  h.listShares.mockResolvedValue([])
  h.getDeployment.mockResolvedValue(null)
  h.listProjectConnectors.mockResolvedValue([])
  h.searchColleagues.mockResolvedValue([])
  h.patchProject.mockImplementation((_id: string, patch: { name?: string }) =>
    Promise.resolve({ ...PROJECT, ...patch }),
  )
})
afterEach(() => cleanup())

describe('the dialog opens on General and names the application', () => {
  it('shows the name and mounts the description editor rather than a second field', async () => {
    open()
    expect((await screen.findByLabelText('Application name') as HTMLInputElement).value).toBe('Visitor Log')
    expect(screen.getByTestId('description-editor')).toBeTruthy()
  })

  it('falls back to a generic title on an application with no name', () => {
    open({ project: { ...PROJECT, name: '' } })
    expect(screen.getByText('Untitled application')).toBeTruthy()
  })

  it('opens straight onto a tab a door asked for', async () => {
    open({ initialTab: 'sharing' })
    expect(await screen.findByTestId('settings-tab-sharing')).toHaveProperty('dataset.state', 'active')
  })
})

describe('the name is saved through the same write the rest of the product uses', () => {
  it('writes the new name and hands the stored application back up', async () => {
    const { onProjectUpdate } = open()
    const field = await screen.findByLabelText('Application name')
    fireEvent.change(field, { target: { value: 'Visitor Register' } })
    fireEvent.blur(field)
    await waitFor(() => expect(h.patchProject).toHaveBeenCalledWith('p1', { name: 'Visitor Register' }))
    await waitFor(() => expect(onProjectUpdate).toHaveBeenCalled())
  })

  it('writes nothing for a name that has not changed', async () => {
    open()
    const field = await screen.findByLabelText('Application name')
    fireEvent.blur(field)
    expect(h.patchProject).not.toHaveBeenCalled()
  })

  it('refuses an empty name before it reaches the wire', async () => {
    open()
    const field = await screen.findByLabelText('Application name')
    fireEvent.change(field, { target: { value: '   ' } })
    fireEvent.blur(field)
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Name cannot be empty.')
    expect(h.patchProject).not.toHaveBeenCalled()
  })

  it('★ keeps the citizen\'s text when the save fails', async () => {
    // The moment a save fails is the moment their words are least replaceable. Resetting the
    // field to the stored value here would throw away exactly what they most want back.
    h.patchProject.mockRejectedValue(new Error('network'))
    open()
    const field = await screen.findByLabelText('Application name')
    fireEvent.change(field, { target: { value: 'Visitor Register' } })
    fireEvent.blur(field)
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect((screen.getByLabelText('Application name') as HTMLInputElement).value).toBe('Visitor Register')
  })

  it('does not open already refusing a stored name over the word cap', async () => {
    // The word rule is not retroactive. A nine-word name saved before it must not greet its
    // owner with an error about text they have not touched.
    const legacy = { ...PROJECT, name: 'One two three four five six seven eight nine ten' }
    open({ project: legacy })
    await screen.findByLabelText('Application name')
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('Delete exists in exactly one place, two steps from the list', () => {
  it('hands off to the confirmation rather than deleting anything itself', async () => {
    const { onDelete } = open()
    fireEvent.click(await screen.findByTestId('settings-delete'))
    expect(onDelete).toHaveBeenCalledTimes(1)
  })

  it('says what goes with it, because the fear it answers is losing work', async () => {
    open()
    const block = (await screen.findByTestId('settings-delete')).parentElement
    expect(block?.textContent).toMatch(/cannot be undone/i)
    expect(block?.textContent).toMatch(/chat/i)
  })
})

describe('Sharing is the same body the workspace opens', () => {
  it('mounts it, with its consent sentence', async () => {
    open()
    fireEvent.mouseDown(screen.getByTestId('settings-tab-sharing'))
    fireEvent.click(screen.getByTestId('settings-tab-sharing'))
    // Binding in substance, and it travels with the body rather than with either frame — so a
    // colleague is never granted access on a surface that forgot to say what access means.
    expect(await screen.findByText(/Anyone you add can open and use this app/)).toBeTruthy()
    await settle()
  })
})

describe('every tab the dialog offers has somewhere to go', () => {
  it('draws the four, in the order the board sets them', async () => {
    open()
    await screen.findByTestId('settings-tab-general')
    expect(screen.getByTestId('settings-tab-sharing')).toBeTruthy()
    expect(screen.getByTestId('settings-tab-integrations')).toBeTruthy()
    expect(screen.getByTestId('settings-tab-production')).toBeTruthy()
    expect(
      screen.getAllByRole('tab').map((tab) => tab.textContent),
    ).toEqual(['General', 'Sharing', 'Integrations', 'Production'])
  })
})

describe('Integrations is the data this one application may read', () => {
  it('★ asks the server nothing about connectors until the tab is chosen', async () => {
    open()
    await screen.findByTestId('settings-tab-general')
    // Liveness beside the absence: the dialog really opened and really drew its General tab, so
    // the unmade read is the panel being mounted BY the tab rather than hidden behind it.
    expect(screen.getByTestId('description-editor')).toBeTruthy()
    expect(h.listProjectConnectors).not.toHaveBeenCalled()
  })

  it('mounts the panel, which reads this application, when the tab is chosen', async () => {
    open()
    fireEvent.mouseDown(screen.getByTestId('settings-tab-integrations'))
    fireEvent.click(screen.getByTestId('settings-tab-integrations'))
    expect(await screen.findByTestId('integrations-tab')).toBeTruthy()
    await waitFor(() => expect(h.listProjectConnectors).toHaveBeenCalledWith('p1'))
    await settle()
  })
})

describe('Production is where the two live-app actions live', () => {
  it('★ reads nothing about production until the tab is actually opened', async () => {
    // A dialog that fired a deployment read on open would spend one per application a citizen
    // glances at the name of. The panel is mounted BY the tab, not merely hidden behind it.
    open()
    await screen.findByTestId('settings-tab-general')
    expect(h.getDeployment).not.toHaveBeenCalled()
  })

  it('mounts the panel, which reads this application, when the tab is chosen', async () => {
    open()
    fireEvent.mouseDown(screen.getByTestId('settings-tab-production'))
    fireEvent.click(screen.getByTestId('settings-tab-production'))
    expect(await screen.findByTestId('production-tab')).toBeTruthy()
    await waitFor(() => expect(h.getDeployment).toHaveBeenCalledWith('p1'))
    await settle()
  })
})
