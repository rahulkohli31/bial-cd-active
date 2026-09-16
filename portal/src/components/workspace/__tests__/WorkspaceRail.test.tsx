/**
 * THE RAIL'S CONTENTS — one section, and the four things that must NOT be here.
 *
 * This suite is deliberately narrow. Everything about the rail's WIDTH, its collapse and its
 * relationship to the pane is a claim about the shell and lives in `ProjectWorkspace.test.tsx`,
 * which renders through the real one.
 *
 * WHAT IT MOSTLY ASSERTS NOW IS ABSENCE, and every absence is PAIRED with the composer being
 * really on screen. A rail that failed to render at all passes an unpaired `toBeNull()` four
 * times over and reads as a clean removal.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import WorkspaceRail from '../WorkspaceRail'
import type { Project } from '../../../utils/projectApi'

vi.mock('../../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/auth')>()),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
}))

const PROJECT: Project = {
  id: 'p1',
  name: 'VIP Movement',
  description: 'A tracked movement.',
  appId: 'a1',
  appStatus: null,
  hasRelaunchableSnapshot: true,
  hasSavedSnapshot: null,
  isServing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
}

function renderRail() {
  return render(
    <MemoryRouter>
      <WorkspaceRail project={PROJECT} />
    </MemoryRouter>,
  )
}

/** The composer really rendered — the liveness half every absence below is paired with. */
function composer(): HTMLElement {
  return screen.getByPlaceholderText(/Describe what you have in mind/i)
}

afterEach(() => cleanup())

describe('what the rail carries', () => {
  it('carries the composer with its kind picker, and that is the whole of it', () => {
    renderRail()
    expect(composer()).toBeTruthy()
    expect(screen.getByRole('radio', { name: 'Build' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'START A CHAT' })).toBeTruthy()
  })

  it('★ centres the composer, because it is alone in a tall column', () => {
    // Mutation receipt: drop `justify-center` and this goes red. Parking the one block on this
    // side at the foot of an empty column reads as a page that failed to draw the rest of itself
    // — which is exactly what a citizen sees on an application with nothing built yet.
    renderRail()
    const column = composer().closest('main')
    expect(column?.className).toMatch(/justify-center/)
  })

  it('★ carries NO start control — exactly one exists, and it is the pane\'s', () => {
    // A second Start button on the same screen satisfies "exactly one control starts it" with
    // two, and both would race the same idempotent endpoint.
    renderRail()
    expect(composer()).toBeTruthy()
    expect(screen.queryByRole('button', { name: /launch application/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
  })
})

/**
 * ★ THE THREE SECTIONS THAT LEFT, and the one thing that makes their departure safe: each has a
 * home in that application's settings, reachable from the toolbar's menu and from the home list.
 * A section removed with nowhere to go is a capability deleted, not a capability moved.
 */
describe('what the rail gave up', () => {
  it.each([
    ['rail-data', 'the data section'],
    ['rail-app-status', 'the app status panel'],
    ['description-rail', 'the description block'],
    ['rail-save-state', 'the save sentence'],
  ])('no longer renders %s (%s)', (testid) => {
    renderRail()
    expect(composer()).toBeTruthy()
    expect(screen.queryByTestId(testid)).toBeNull()
  })

  it('names no integrations door, because the rail is not one any more', () => {
    // The copy is the fifth link of a removal: a page that still SAYS "manage integrations" is a
    // page still offering the capability, whatever happened to the component behind it.
    renderRail()
    expect(composer()).toBeTruthy()
    expect(screen.queryByText(/integrations/i)).toBeNull()
  })
})
