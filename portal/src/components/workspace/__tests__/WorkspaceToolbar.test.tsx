/**
 * THE TOOLBAR ROW (plan 002, U2) — one row, drawn once, above both columns.
 *
 * ═══ WHAT THIS SUITE IS FOR ═══
 *
 * The row replaces THREE headers: the rail's (back, project name, status chip, rename), the
 * conversation panel's (a bordered breadcrumb), and the framed preview's (device widths, Reload,
 * Save). Each of those lived inside something that could be collapsed, unmounted, or never mounted
 * at all — which is why the project name truncated at 400px and vanished on a collapse, and why a
 * project with nothing built had no device switcher, no Save and no way out to a tab.
 *
 * So the scenarios here are mostly about POSITION AND LIFETIME rather than about markup: what the
 * row shows on each address, what survives a collapse, and what a cold open renders before any
 * fetch has landed. They render through the REAL shell, because a row that is drawn above the
 * Outlet is invisible to a test that mounts only the Outlet's child.
 *
 * ═══ COVERAGE THAT MOVED HERE WITH ITS CONTROL ═══
 *
 * The device switcher's `aria-pressed` scenarios (from `LivePreview.test.jsx`), the Save control's
 * six states including the one that matters most — `null` is UNKNOWN and must never read as saved
 * — and the status chip's three (from `ProjectPage.test.tsx`). Named here so that "the test went
 * with the markup" cannot be how any of them stops being checked.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, Link, useLocation } from 'react-router-dom'
import WorkspaceShell from '../WorkspaceShell'
import {
  useAppPaneVisible,
  usePublishAddress,
  usePublishHeading,
  usePublishPaneView,
  usePublishSave,
  usePublishSaveState,
  useWorkspaceProject,
  type PaneView,
  type SaveSlot,
  type WorkspaceActions,
  type WorkspaceHeading,
} from '../workspaceChannel'

vi.mock('../../layout/Navbar', () => ({ default: () => <div data-testid="navbar" /> }))
/**
 * THE ROW'S RENDER COUNTER, and it has to be inside the row rather than around it.
 *
 * The chip is an ordinary child of the row's own JSX with no memo between them, so React renders
 * it exactly once per render of the row — which makes this stub a count of the row's renders that
 * a wrapper around `WorkspaceToolbar` could not produce (a wrapper only sees the renders its own
 * parent causes, and misses the ones the row's own cell subscriptions cause, which are precisely
 * the ones "the row does not wake with the composer" is about).
 */
const h = vi.hoisted(() => ({ rowRenders: 0 }))
vi.mock('../../PublishStatusChip', () => ({
  default: function PublishStatusChipStub({ projectId }: { projectId: string }) {
    h.rowRenders += 1
    return <span data-testid="publish-chip-stub" data-project={projectId} />
  },
}))
vi.mock('../../LivePreview', () => ({
  default: () => <div data-testid="live-preview" />,
}))

const APP_URL = 'https://app-a.example.azurecontainerapps.io/'

const EMPTY_PANE: PaneView = {
  iterating: false, reconnecting: false,
  restoredFromFailedBuild: false, completedLive: true, hasSavedBuild: null,
  previewState: null, occupyingProjectName: null, turnRunning: false,
  compileState: null, workspaceLost: false,
}

const PROJECT_HEADING: WorkspaceHeading = {
  projectId: 'pA',
  projectName: 'Visitor Log — Airport Office',
  chatTitle: null,
  chatKind: null,
}

const CHAT_HEADING: WorkspaceHeading = {
  projectId: 'pA',
  projectName: 'Visitor Log — Airport Office',
  chatTitle: 'Add an out-time column',
  chatKind: 'build',
}

/** The words a kind is presented with come from the bootstrap catalogue, never from a literal. */
vi.mock('../../../utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/auth')>()),
  getStoredUser: () => ({
    chat_kinds: [
      { value: 'plan', name: 'Plan', description: 'Shape a plan first.' },
      { value: 'build', name: 'Build', description: 'Change the live app.' },
    ],
  }),
}))

interface SurfaceProps {
  heading: WorkspaceHeading
  /** Absent means "no app to point at" — the pane is asked for but nothing is framed. */
  appUrl?: string | null
  save?: Omit<SaveSlot, 'canSave'>
  actions?: WorkspaceActions
  paneVisible?: boolean
}

/** A mounted surface, publishing exactly what the row reads and nothing else. */
function Surface({
  heading,
  appUrl = APP_URL,
  save = { dirty: null, saving: false, error: null },
  actions = { save: null, rename: null },
  paneVisible = true,
}: SurfaceProps) {
  useWorkspaceProject(heading.projectId)
  usePublishHeading(heading)
  usePublishAddress({ url: appUrl, status: appUrl ? 'ready' : null }, heading.projectId)
  // A FRESH OBJECT PER RENDER, which is what the real conversation surface publishes — the pane
  // cell is identity-compared, so this is what makes a keystroke reach the channel at all.
  usePublishPaneView({ ...EMPTY_PANE })
  usePublishSave(save, actions)
  usePublishSaveState(save.dirty)
  useAppPaneVisible(paneVisible)
  return <div data-testid="surface" />
}

function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>
}

/** Both addresses under ONE shell, navigated by link exactly as the product navigates them. */
function Workspace({ entry = '/projects/pA', project, chat }: { entry?: string; project?: SurfaceProps; chat?: SurfaceProps }) {
  return (
    <MemoryRouter initialEntries={[entry]}>
      <Where />
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route
            path="/projects/:projectId"
            element={
              <>
                <Link to="/chat/c1">to chat</Link>
                <Surface {...(project ?? { heading: PROJECT_HEADING })} />
              </>
            }
          />
          <Route
            path="/chat/:chatId"
            element={
              <>
                <Link to="/projects/pA">to project</Link>
                <Surface {...(chat ?? { heading: CHAT_HEADING })} />
              </>
            }
          />
        </Route>
        <Route path="/projects" element={<div data-testid="projects-list" />} />
      </Routes>
    </MemoryRouter>
  )
}

const row = () => screen.getByTestId('workspace-toolbar')
const title = () => screen.getByTestId('toolbar-title')

beforeEach(() => vi.clearAllMocks())
afterEach(() => cleanup())

describe('what the row names on each address', () => {
  it('the project screen: back and the project name, and NOT a second copy of the state', () => {
    render(<Workspace />)

    expect(screen.getByRole('button', { name: 'Back to projects' })).toBeTruthy()
    expect(title().textContent).toBe('Visitor Log — Airport Office')
    expect(title().tagName).toBe('H1')
    // `PreviewOff`, `Main`, `NewProject` and `NothingBuilt` all draw this cluster as chevron +
    // title and nothing else: the rail's APP STATUS section is right there carrying the pill, and
    // a chip beside the title stated the same word twice inside 300px.
    expect(screen.queryByTestId('publish-chip-stub')).toBeNull()
    // The chat half is absent, and that is what a heading with no kind means.
    expect(screen.queryByTestId('toolbar-chat-kind')).toBeNull()
  })

  it('a chat: the project, the kind pill, and the chat title', () => {
    render(<Workspace entry="/chat/c1" />)

    // The project drops to a muted breadcrumb and the CHAT becomes the heading — same row, same
    // slots, only which of the two names is the <h1>.
    expect(row().textContent).toContain('Visitor Log — Airport Office')
    expect(screen.getByTestId('toolbar-chat-kind').textContent).toContain('Build')
    expect(title().textContent).toBe('Add an out-time column')
    expect(screen.getByTestId('publish-chip-stub')).toBeTruthy()
  })

  it('★ draws the BUILD pill as the word alone, and the PLAN pill with the glyph its board has', () => {
    // The row shipped a Lucide wrench inside the BUILD pill that no BuildChat-family board draws.
    // The two kinds genuinely differ here — PLAN carries an 11px message-square, BUILD is the word
    // — so the assertion is a pair: the absence on one kind, the presence on the other, in the
    // same row. An absence alone would pass against a pill that stopped rendering at all.
    render(<Workspace entry="/chat/c1" />)
    const build = screen.getByTestId('toolbar-chat-kind')
    expect(build.textContent).toContain('Build')
    expect(build.querySelector('svg')).toBeNull()

    cleanup()
    render(<Workspace entry="/chat/c1" chat={{ heading: { ...CHAT_HEADING, chatKind: 'plan' } }} />)
    const plan = screen.getByTestId('toolbar-chat-kind')
    expect(plan.textContent).toContain('Plan')
    expect(plan.querySelector('svg')).toBeTruthy()
  })

  it('a freshly created chat, whose title is not yet known, names its kind rather than nothing', () => {
    // The ordinary case, not an error: the row is created by the first send and its title is
    // derived from that message. A blank <h1> or a spinner would both be worse than the kind.
    render(<Workspace entry="/chat/c1" chat={{ heading: { ...CHAT_HEADING, chatTitle: null } }} />)

    expect(title().textContent).toBe('New build')
    expect(screen.getByTestId('toolbar-chat-kind')).toBeTruthy()
  })

  it('★ a cold open, before the project name has resolved, keeps a stable name slot', () => {
    // THE FAILURE THIS IS WRITTEN AGAINST. On a chat address the project name arrives from a
    // second fetch, so the row spends its first frames with `projectName: null`. An empty slot
    // there means the row's contents shift under the citizen the moment the fetch lands.
    render(
      <Workspace
        entry="/chat/c1"
        chat={{ heading: { ...CHAT_HEADING, projectName: null, chatTitle: null } }}
      />,
    )

    expect(row()).toBeTruthy()
    expect(row().className).toMatch(/h-\[54px\]/)
    expect(row().textContent).toContain('Your project')
    expect(screen.getByRole('button', { name: 'Back to project' })).toBeTruthy()
  })

  it('★ a chat still inside its load window is drawn as a CHAT, not as the project screen', () => {
    // THE FAILURE THIS IS WRITTEN AGAINST, and it is a whole `GET /conversations/{id}` long: open
    // a bare `/chat/{id}` — a reload, a bookmark, the hand-over out of a plan chat — and neither
    // the kind nor the project has arrived. The row used to take that to mean "project screen": a
    // lone <h1> reading "Your project", no breadcrumb, and a back control labelled and aimed at
    // the projects list. A citizen who reloaded a build chat and pressed back was thrown out of
    // the project entirely, and the row re-shaped under them when the fetch landed.
    //
    // The scenario above this one keeps `chatKind: 'build'`, so it asserts a state the product
    // never actually passes through; this is the state it does.
    render(
      <Workspace
        entry="/chat/c1"
        chat={{ heading: { projectId: null, projectName: null, chatTitle: null, chatKind: null } }}
      />,
    )

    // The discriminator between the two shapes: on the project screen "Your project" IS the <h1>;
    // on a chat it is the breadcrumb beside it and the <h1> is the chat's own slot.
    expect(row().textContent).toContain('Your project')
    expect(title().textContent).toBe('')
    // Rename belongs to the project screen; nothing on a chat address may offer it.
    expect(screen.queryByRole('button', { name: /rename/i })).toBeNull()
    // And the way out says where it actually goes: with no project resolved there is none to
    // return to, so it is the list, and it says the list.
    expect(screen.getByRole('button', { name: 'Back to projects' })).toBeTruthy()
    // LIVENESS: the row is drawn at full height throughout, which is the property that stops the
    // layout shifting when the fetch lands.
    expect(row().className).toMatch(/h-\[54px\]/)
  })

  it('★ and its back control reaches the project the moment the URL names one, kind or no kind', () => {
    render(
      <Workspace
        entry="/chat/c1"
        chat={{ heading: { projectId: 'pA', projectName: null, chatTitle: null, chatKind: null } }}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Back to project' }))
    expect(screen.getByTestId('where').textContent).toBe('/projects/pA')
  })

  it('★ when the project fetch FAILED the row still names something and back still works', () => {
    // A project deleted out from under an open chat. `null` is the same value for "not yet" and
    // "never", and in both the row keeps its height, its word and its way out.
    render(<Workspace entry="/chat/c1" chat={{ heading: { ...CHAT_HEADING, projectName: null } }} />)

    fireEvent.click(screen.getByRole('button', { name: 'Back to project' }))
    expect(screen.getByTestId('where').textContent).toBe('/projects/pA')
  })

  it('no history control is rendered anywhere', () => {
    // Four boards draw a clock in this row and the drawer behind it is a later feature by the
    // owner's decision. Not built, and not stubbed either — a control that implies a drawer
    // nobody can open is worse than its absence.
    render(<Workspace />)
    expect(screen.queryByRole('button', { name: /history/i })).toBeNull()
    expect(screen.queryByRole('link', { name: /history/i })).toBeNull()
    expect(row().textContent).not.toMatch(/history/i)
  })
})

describe('the row is one element across a route change', () => {
  it('★ changes its contents and never its position, and does not remount', () => {
    // The whole reason the row is drawn by the shell. Three headers meant three elements
    // appearing and disappearing; one row means the same DOM node throughout.
    render(<Workspace />)
    const before = row()
    expect(title().textContent).toBe('Visitor Log — Airport Office')

    fireEvent.click(screen.getByText('to chat'))

    expect(row()).toBe(before)
    expect(title().textContent).toBe('Add an out-time column')
  })

  it('exactly one toolbar row renders, on both addresses', () => {
    render(<Workspace />)
    expect(screen.getAllByTestId('workspace-toolbar')).toHaveLength(1)
    fireEvent.click(screen.getByText('to chat'))
    expect(screen.getAllByTestId('workspace-toolbar')).toHaveLength(1)
  })

  it('★ the title is not truncated away — it is outside the rail, so the rail\'s width cannot clip it', () => {
    // The defect in one assertion: the name used to live INSIDE a 400px column. Now it is a child
    // of the row, which spans the window.
    render(<Workspace />)
    expect(screen.getByTestId('workspace-outlet').contains(title())).toBe(false)
    expect(row().contains(title())).toBe(true)
  })
})

describe('collapsing the rail', () => {
  it('★ leaves the row and everything in it visible', () => {
    render(<Workspace />)
    fireEvent.click(screen.getByRole('button', { name: 'Hide details' }))

    const rail = screen.getByTestId('workspace-outlet')
    expect(rail.className).toMatch(/(^|\s)w-0(\s|$)/)
    expect(rail.className).toMatch(/invisible/)
    // …and the row is untouched: title, chip and every control still there and still pressable.
    expect(title().textContent).toBe('Visitor Log — Airport Office')
    expect(screen.getByTestId('publish-chip-stub')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Show details' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Back to projects' })).toBeTruthy()
  })

  it('★ collapses in BOTH directions, because below the threshold the columns stack', () => {
    // THE BROKEN SCREEN THIS IS WRITTEN AGAINST, found in a browser at 1024px and invisible to
    // every suite because jsdom lays nothing out. The rail is a child of a flex ROW above the
    // stacking threshold and a flex COLUMN below it. `w-0 flex-shrink-0` collapses it in the row
    // and does nothing at all in the column: the rail kept its full 1,586px CONTENT height, so
    // pressing "Hide details" on a narrow window left an invisible band exactly where the rail had
    // been and pushed the app pane to y=1697 with a height of zero. The citizen presses the control
    // whose whole promise is "give the app the screen" and the entire workspace goes blank.
    //
    // A class assertion is all jsdom can carry — which is precisely why the class has to be pinned
    // here rather than trusted to a layout nobody in this suite computes.
    render(<Workspace />)
    fireEvent.click(screen.getByRole('button', { name: 'Hide details' }))

    const rail = screen.getByTestId('workspace-outlet')
    expect(rail.className).toMatch(/(^|\s)h-0(\s|$)/)
    expect(rail.className).toMatch(/(^|\s)w-0(\s|$)/)
    // Liveness: the same press still hides it, so this is not passing on a rail that never collapsed.
    expect(rail.className).toMatch(/invisible/)
  })

  it('the toggle points at the rail it hides', () => {
    render(<Workspace />)
    const toggle = screen.getByRole('button', { name: 'Hide details' })
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(toggle.getAttribute('aria-controls')).toBe(screen.getByTestId('workspace-outlet').id)
  })

  it('is absent when there is no pane to give the screen to', () => {
    render(<Workspace project={{ heading: PROJECT_HEADING, paneVisible: false }} />)
    expect(screen.queryByRole('button', { name: /hide details/i })).toBeNull()
    // Liveness: the row itself is still rendering.
    expect(title().textContent).toBe('Visitor Log — Airport Office')
  })
})

describe('the app-scoped controls appear only when there is an app to point at', () => {
  it('shows the device switcher and the new-tab link over a running app', () => {
    render(<Workspace />)
    expect(screen.getByRole('group', { name: 'Preview device width' })).toBeTruthy()
    const tab = screen.getByRole('link', { name: 'Open your app in a new tab' })
    expect(tab.getAttribute('href')).toBe(APP_URL)
    expect(tab.getAttribute('target')).toBe('_blank')
    // Without `noopener` the opened page gets a handle on this window through `window.opener`.
    expect(tab.getAttribute('rel')).toContain('noopener')
  })

  it('★ hides both when nothing is framed, rather than offering controls that cannot act', () => {
    render(<Workspace project={{ heading: PROJECT_HEADING, appUrl: null }} />)
    expect(screen.queryByRole('group', { name: 'Preview device width' })).toBeNull()
    expect(screen.queryByRole('link', { name: /new tab/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /reload your app/i })).toBeNull()
    // Liveness: the row is there, it simply has nothing app-shaped to offer.
    expect(title().textContent).toBe('Visitor Log — Airport Office')
  })

  it('marks exactly one device pressed, and switches', () => {
    // Moved from `LivePreview.test.jsx` with the control. The WIDTH half — that Tablet reaches the
    // card's inline style as 834px — stays there, since the card is the pane's.
    render(<Workspace />)
    const at = (name: string) => screen.getByRole('button', { name }).getAttribute('aria-pressed')

    expect(at('Desktop')).toBe('true')
    expect(at('Tablet')).toBe('false')
    expect(at('Mobile')).toBe('false')

    fireEvent.click(screen.getByRole('button', { name: 'Tablet' }))
    expect(at('Tablet')).toBe('true')
    expect(at('Desktop')).toBe('false')
    expect(at('Mobile')).toBe('false')
  })

  it('★ the chosen width survives a route change from the project screen to a chat', () => {
    // It could not, while the state lived inside `LivePreview`: the pane host re-renders around
    // that component and its private `useState` had no reason to outlive a navigation. The shell
    // holds it now, which is what makes this assertable at all.
    render(<Workspace />)
    fireEvent.click(screen.getByRole('button', { name: 'Mobile' }))

    fireEvent.click(screen.getByText('to chat'))

    expect(screen.getByRole('button', { name: 'Mobile' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('offers Reload — a shipped recourse no board draws, kept deliberately', () => {
    render(<Workspace />)
    expect(screen.getByRole('button', { name: 'Reload your app' })).toBeTruthy()
  })
})

describe('the Save control', () => {
  const withSave = (save: Omit<SaveSlot, 'canSave'>, onSave: (() => void) | null = null) =>
    render(<Workspace project={{ heading: PROJECT_HEADING, save, actions: { save: onSave, rename: null } }} />)

  it('★ UNKNOWN hides the control rather than claiming the work is saved', () => {
    // THE POINT, carried over verbatim from the control's old home. `null` is not `false`. A chip
    // reading "Saved" here would be a claim nobody verified, and the citizen would act on it.
    withSave({ dirty: null, saving: false, error: null }, () => {})
    expect(screen.queryByTestId('save-project')).toBeNull()
    expect(screen.queryByTestId('save-state')).toBeNull()
    // Liveness: the row rendered.
    expect(title().textContent).toBe('Visitor Log — Airport Office')
  })

  it('draws the board\'s unsaved treatment: a teal outline with an amber dot, not a filled button', () => {
    withSave({ dirty: true, saving: false, error: null }, () => {})
    const save = screen.getByTestId('save-project')
    expect(save.textContent).toContain('Save')
    expect(save.className).toMatch(/border-primary/)
    expect(save.className).not.toMatch(/bg-primary/)
    expect(save.querySelector('.bg-accent')).not.toBeNull()
  })

  it('goes quiet once everything is saved, and refuses the press', () => {
    const onSave = vi.fn()
    withSave({ dirty: false, saving: false, error: null }, onSave)
    const save = screen.getByTestId('save-project')
    expect(save.textContent).toContain('Saved')
    expect(save.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(save)
    expect(onSave).not.toHaveBeenCalled()
  })

  it('calls the published action once per click', () => {
    const onSave = vi.fn()
    withSave({ dirty: true, saving: false, error: null }, onSave)
    fireEvent.click(screen.getByTestId('save-project'))
    expect(onSave).toHaveBeenCalledTimes(1)
  })

  it('reports progress while saving, and refuses a second click', () => {
    const onSave = vi.fn()
    withSave({ dirty: true, saving: true, error: null }, onSave)
    const save = screen.getByTestId('save-project')
    expect(save.textContent).toContain('Saving')
    expect(save.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(save)
    expect(onSave).not.toHaveBeenCalled()
  })

  it('shows a save failure as an alert instead of letting it look successful', () => {
    withSave(
      { dirty: true, saving: false, error: 'Your workspace is no longer running, so there is nothing to save.' },
      () => {},
    )
    expect(screen.getByRole('alert').textContent).toMatch(/no longer running/i)
  })

  it('★ with NO action published it is a status, not a button', () => {
    // Today's project screen: its surface deliberately publishes no `onSave`, and U11 gives it
    // one. Until then the state is worth showing and a press would do nothing, so nothing invites
    // one. Mutation receipt: render a `<button>` unconditionally and this goes red.
    withSave({ dirty: false, saving: false, error: null }, null)
    expect(screen.queryByTestId('save-project')).toBeNull()
    expect(screen.getByTestId('save-state').textContent).toContain('Saved')
  })

  it('renders no control with a real disabled attribute', () => {
    withSave({ dirty: false, saving: false, error: null }, () => {})
    for (const el of screen.getAllByRole('button')) expect(el.hasAttribute('disabled')).toBe(false)
  })
})

describe('the status chip — where the state is said, and where it would be said twice', () => {
  // THE RULE THE BOARDS DRAW, and it is about duplication rather than about the chip. Every state
  // the chip names is also named by the rail's APP STATUS section. So the chip appears exactly
  // where that section is NOT: on a chat, which has no rail section at all, and over a collapsed
  // rail, where the section has just gone off screen. On the open project screen it is a second
  // rendering of one fact standing 300px from the first — which is how two renderings begin to
  // disagree, and it is the one thing this row is supposed to prevent.

  it('★ names the project on a chat, where nothing else says the state', () => {
    render(<Workspace entry="/chat/c1" />)
    expect(screen.getByTestId('publish-chip-stub').getAttribute('data-project')).toBe('pA')
  })

  it('★ comes back when the rail that was carrying it is hidden', () => {
    render(<Workspace />)
    expect(screen.queryByTestId('publish-chip-stub')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Hide details' }))
    expect(screen.getByTestId('publish-chip-stub').getAttribute('data-project')).toBe('pA')
  })

  it('is drawn ONCE where it is drawn at all — one mount, not two that could word a state differently', () => {
    render(<Workspace entry="/chat/c1" />)
    expect(screen.getAllByTestId('publish-chip-stub')).toHaveLength(1)
  })

  it('renders nothing where there is no project to name', () => {
    render(<Workspace entry="/chat/c1" chat={{ heading: { ...CHAT_HEADING, projectId: null } }} />)
    expect(screen.queryByTestId('publish-chip-stub')).toBeNull()
  })
})

describe('the back control and the rename', () => {
  it('goes to the projects list from a project, and to the project from a chat', () => {
    render(<Workspace />)
    fireEvent.click(screen.getByRole('button', { name: 'Back to projects' }))
    expect(screen.getByTestId('where').textContent).toBe('/projects')

    cleanup()
    render(<Workspace entry="/chat/c1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Back to project' }))
    expect(screen.getByTestId('where').textContent).toBe('/projects/pA')
  })

  it('★ asks first when there is unsaved work, rather than discarding it in silence', async () => {
    // One of the two most-used exits out of a workspace, and it used to leave unsaved work behind
    // without a word. It routes through the same guard the navbar's links do.
    render(<Workspace project={{ heading: PROJECT_HEADING, save: { dirty: true, saving: false, error: null } }} />)

    fireEvent.click(screen.getByRole('button', { name: 'Back to projects' }))

    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(screen.getByTestId('where').textContent).toBe('/projects/pA')
  })

  it('offers rename on the project screen only, and presses the published action', () => {
    const rename = vi.fn()
    render(<Workspace project={{ heading: PROJECT_HEADING, actions: { save: null, rename } }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Rename project' }))
    expect(rename).toHaveBeenCalledTimes(1)

    cleanup()
    // A chat's title is the agent's, not the citizen's.
    render(<Workspace entry="/chat/c1" />)
    expect(screen.queryByRole('button', { name: /rename/i })).toBeNull()
  })
})

describe('the row does not wake with the composer', () => {
  /**
   * A chat surface with a composer in it, republishing its pane view on every keystroke exactly as
   * the real one does — plus one control that changes something the row DOES read, which is what
   * makes the counter's silence meaningful.
   */
  function Typist() {
    const [text, setText] = useState('')
    const [chatTitle, setChatTitle] = useState('Add an out-time column')
    return (
      <>
        <input aria-label="composer" value={text} onChange={(e) => setText(e.target.value)} />
        <button type="button" onClick={() => setChatTitle('Add an out-time and an in-time')}>
          rename the chat
        </button>
        <Surface heading={{ ...CHAT_HEADING, chatTitle }} />
      </>
    )
  }

  const typing = () =>
    render(
      <MemoryRouter initialEntries={['/chat/c1']}>
        <Routes>
          <Route element={<WorkspaceShell />}>
            <Route path="/chat/:chatId" element={<Typist />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )

  it('★ a publish that changes nothing the row shows does not re-render it', async () => {
    // The reason the row reads its own value-compared cells rather than the pane cell, which is
    // republished per character typed and holds handlers that cannot be compared.
    //
    // COUNTED, NOT COMPARED BY NODE. The version of this scenario that shipped asserted `row()` was
    // the same ELEMENT after five re-renders — which React guarantees whether the row re-rendered
    // five times or none, so it stayed green for the very property it was written for. Point the
    // row at the pane cell and this one goes red; that one did not.
    typing()
    await waitFor(() => expect(screen.getByTestId('publish-chip-stub')).toBeTruthy())
    const node = row()
    const before = h.rowRenders

    for (const value of ['a', 'ad', 'add', 'add ', 'add a']) {
      fireEvent.change(screen.getByLabelText('composer'), { target: { value } })
    }

    expect(h.rowRenders).toBe(before)
    // …and the row is still the same element with the same contents, which is the half the
    // earlier version of this test could see.
    expect(row()).toBe(node)
    expect(title().textContent).toBe('Add an out-time column')

    // LIVENESS: the counter is genuinely wired to the row. Something the row DOES read — the chat's
    // own title — wakes it, so the five silent keystrokes above are a subscription that ignores the
    // composer rather than a probe that never counts anything.
    fireEvent.click(screen.getByRole('button', { name: 'rename the chat' }))
    expect(h.rowRenders).toBeGreaterThan(before)
    expect(title().textContent).toBe('Add an out-time and an in-time')
  })
})
