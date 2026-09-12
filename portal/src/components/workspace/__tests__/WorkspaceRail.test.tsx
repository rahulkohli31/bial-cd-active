/**
 * THE RAIL'S CONTENTS — four things, and the one control that must NOT be here.
 *
 * This suite is deliberately narrow. Everything about the rail's WIDTH, its collapse and its
 * relationship to the pane is a claim about the shell and lives in `ProjectWorkspace.test.tsx`,
 * which renders through the real one. What is left is what the rail itself is answerable for: that
 * it carries all four, and that it does not carry a second way to start the app.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import WorkspaceRail from '../WorkspaceRail'
import type { Project } from '../../../utils/projectApi'
import type { SaveState } from '../../../utils/buildSessionApi'

vi.mock('../../PublishStatusChip', () => ({
  default: ({ projectId }: { projectId: string }) => (
    <span data-testid="publish-chip-stub" data-project={projectId} />
  ),
}))
vi.mock('../../projects/ProjectDescriptionEditor', () => ({
  default: () => <div data-testid="description-editor" />,
}))
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

const noop = () => {}

function renderRail(over: { save?: SaveState | null } = {}) {
  return render(
    <MemoryRouter>
      <WorkspaceRail
        project={PROJECT}
        save={over.save ?? null}
        onProjectUpdate={noop}
      />
    </MemoryRouter>,
  )
}

afterEach(() => cleanup())

describe("what the rail carries at rest", () => {
  it('carries the composer with its kind picker, the app status, and the description', () => {
    renderRail()

    expect(screen.getByPlaceholderText(/Describe what you have in mind/i)).toBeTruthy()
    expect(screen.getByRole('radio', { name: 'Build' })).toBeTruthy()
    expect(screen.getByTestId('rail-app-status')).toBeTruthy()
    expect(screen.getByTestId('description-editor')).toBeTruthy()
  })

  it('★ carries the PUBLISH status, and no longer the workspace sentence', () => {
    // TWO DIFFERENT STATUSES: publishing (live / approved / last saved) vs. whether the
    // CONTAINER is up — the boards give that to the pane, where a citizen is already looking
    // for their app. The rail carried it too, which meant two renderers for one sentence.
    const status = screen.queryByTestId('rail-app-status')
    expect(status).toBeNull() // nothing rendered yet — guards against a stale query below
    renderRail()

    const block = screen.getByTestId('rail-app-status')
    expect(within(block).getByTestId('app-status-panel')).toBeTruthy()
    expect(block.textContent).not.toMatch(/your app is saved/i)
  })

  it('★ carries NO start control — exactly one exists, and it is the pane\'s', () => {
    // The map OFFERS the start action here; the rail deliberately does not render it. A second
    // Start button on the same screen satisfies "exactly one control starts it" with two, and both
    // would race the same idempotent endpoint.
    //
    // Mutation receipt: render `workspace.action` as a button in the rail and this goes red.
    renderRail()

    expect(screen.queryByRole('button', { name: /launch application/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
  })

  it('shows no save block at all when the workspace is not alive', () => {
    // Not an omission: `fetchSaveState` costs two container `git` execs and may only be asked on a
    // live workspace, so its caller hands `null` here for every other state.
    renderRail({ save: null })

    expect(screen.queryByTestId('rail-save-state')).toBeNull()
  })
})

describe('the save half, which exists only while the app is running', () => {
  const save = (over: Partial<SaveState> = {}): SaveState => ({
    appId: 'a1',
    dirty: false,
    containerHead: 'aaaaaaabbbbbb',
    savedHead: 'ccccccc1111111',
    // The DEFAULT IS NULL and every existing scenario keeps it, so the `dirty: true` readings
    // below still land on the plain warning they were written against. A scenario that wants the
    // recoverable arm has to ask for it by name.
    recoveryAt: null,
    ...over,
  })

  it('says what is true for each arm of the TRI-STATE, and never reads null as clean', () => {
    const readings: [boolean | null, RegExp][] = [
      [true, /not saved yet/i],
      [false, /everything is saved/i],
      [null, /could not check/i],
    ]
    for (const [dirty, expected] of readings) {
      renderRail({ save: save({ dirty }) })
      expect(screen.getByTestId('rail-save-state').textContent, `dirty=${String(dirty)}`).toMatch(expected)
      cleanup()
    }
  })

  it('never reports "everything is saved" from an unknown state', () => {
    // where the platform cannot tell, it says so rather than reporting there is nothing to
    // lose. This is the half a loose assertion on the null arm would let through.
    renderRail({ save: save({ dirty: null }) })
    expect(screen.getByTestId('rail-save-state').textContent).not.toMatch(/everything is saved/i)
  })

  /**
   * ★ THE FOURTH SENTENCE, and the reading that produced the bug it exists for.
   *
   * A citizen described an app, the platform built it, they touched nothing — and the rail told
   * them they had changes that were not saved. `dirty` was right (there is no saved VERSION; Save
   * is the citizen's own act) and the sentence was wrong, because the platform was holding a
   * recovery copy of that tree the whole time.
   */
  const RECOVERY_INSTANT = '2026-09-10T10:38:43Z'

  it('★ says the work is safe when the platform is holding a copy it can put back', () => {
    // MUTATION RECEIPT: collapse `saveSentence`'s two `true` arms back into one warning and this
    // is the only case in the file that goes red — the three below stay green.
    renderRail({ save: save({ dirty: true, savedHead: null, recoveryAt: RECOVERY_INSTANT }) })

    const block = screen.getByTestId('rail-save-state')
    expect(block.textContent).toBe('Your work is safe. Save it to keep a version you can come back to.')
    // …and it is neither of the sentences it replaced: not the alarm, and not a claim of a save.
    expect(block.textContent).not.toMatch(/not saved yet/i)
    expect(block.textContent).not.toMatch(/everything is saved/i)
  })

  it('★ STILL says the old warning when there is no recovery copy', () => {
    // The half that keeps the fix honest. Real unsaved work the platform is holding nothing for
    // must not have become invisible — this is the reading the original sentence was written for.
    renderRail({ save: save({ dirty: true, recoveryAt: null }) })

    expect(screen.getByTestId('rail-save-state').textContent).toBe('You have changes that are not saved yet.')
  })

  it('★ answers the tri-state FIRST — a recovery copy never speaks for a check that did not run', () => {
    // ORDERING, and it is not cosmetic: reverse the two decisions in `saveSentence` and a `null`
    // beside a recovery instant starts reporting safety about a container nobody could read, and
    // a clean workspace starts being told to go and save something. Both sentences are untouched
    // by the new fact, deliberately.
    renderRail({ save: save({ dirty: null, recoveryAt: RECOVERY_INSTANT }) })
    expect(screen.getByTestId('rail-save-state').textContent).toBe('We could not check for unsaved changes.')
    cleanup()

    renderRail({ save: save({ dirty: false, recoveryAt: RECOVERY_INSTANT }) })
    expect(screen.getByTestId('rail-save-state').textContent).toBe('Everything is saved.')
  })

  it('★ says only whether the container has moved on — the VERSION is the panel\'s row now', () => {
    // The commit line duplicated a fact the panel states properly — which version was last
    // saved, with its date, from object-store metadata, no container involved. This block
    // answers what only a RUNNING container can: whether it holds work the saved bundle does not.
    renderRail({ save: save({ savedHead: 'ccccccc1111111' }) })

    const block = screen.getByTestId('rail-save-state')
    expect(block.textContent).toMatch(/everything is saved/i)
    expect(block.textContent).not.toContain('ccccccc')
    expect(block.textContent).not.toMatch(/last saved version/i)
  })
})

describe('the publishing chip and the recents survive the rewrite', () => {
  it('draws no chip and no project name of its own — both are the toolbar row\'s', () => {
    // THE RAIL SURRENDERED ITS HEADER. Back, the project name, the status chip and
    // the rename control lived here, inside a 400px column, which is why the name truncated at the
    // rail's width and vanished entirely on a collapse — the opposite of what the collapse board
    // draws. They are drawn once by the shell now, above both columns.
    //
    // ASSERTED AS AN ABSENCE PAIRED WITH A LIVENESS CHECK, because a `toBeNull()` on its own passes
    // just as happily when the component threw and rendered nothing at all.
    renderRail()

    expect(screen.queryByTestId('publish-chip-stub')).toBeNull()
    expect(screen.queryByRole('heading', { name: 'VIP Movement' })).toBeNull()
    expect(screen.queryByRole('button', { name: /rename project/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /back to projects/i })).toBeNull()
    // …and the rail itself is alive and rendering its own sections.
    expect(screen.getByTestId('rail-app-status')).toBeTruthy()
    expect(screen.getByTestId('description-rail')).toBeTruthy()
  })

  it('★ renders exactly three sections, in the board\'s order, with no card borders between them', () => {
    // THE SHAPE, AND THE FOURTH SECTION THAT IS NOT THERE. A grey rail with four floating white
    // cards became a white rail with three sections and 1px rules — which is not decoration: it
    // gives #E2E8F0 back its role as the divider and #F0F4F8 back its role as the ground behind
    // the app. The fourth card was the recents list, deleted by the owner's ruling.
    const { container } = renderRail()

    expect(screen.queryByTestId('conversations')).toBeNull()
    expect(screen.queryByText(/no conversations yet/i)).toBeNull()

    const labels = Array.from(container.querySelectorAll('h2')).map((h) => h.textContent)
    // The third section's heading is the description editor's own, which this suite stubs — so
    // the two the RAIL draws are asserted here, in order, and the stub's presence stands for the
    // third. `ProjectPage.test.tsx` renders the real editor and sees its heading.
    expect(labels).toEqual(['START A CHAT', 'APP STATUS'])
    expect(screen.getByTestId('description-editor')).toBeTruthy()

    // No section carries a border, a radius or a fill of its own.
    for (const testid of ['rail-app-status', 'description-rail']) {
      const section = screen.getByTestId(testid)
      expect(section.className).not.toMatch(/border-bial-border/)
      expect(section.className).not.toMatch(/rounded-2xl/)
      expect(section.className).not.toMatch(/bg-white/)
    }
    // …and the rail itself is the white surface, with hairlines between the sections.
    expect(container.querySelector('main')?.className).toMatch(/bg-white/)
    expect(container.querySelectorAll('div.h-px').length).toBe(2)
  })
})
