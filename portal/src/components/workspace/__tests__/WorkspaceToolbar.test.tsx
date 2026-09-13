/**
 * THE TOOLBAR ROW — one row, drawn once, above both columns.
 *
 * It replaces three headers (the rail's, the conversation panel's, the framed preview's) that
 * could each be collapsed, unmounted, or never mounted, so scenarios here are about POSITION AND
 * LIFETIME rather than markup, and render through the REAL shell since the row sits above the
 * Outlet.
 *
 * Coverage moved here with its control: the device switcher's `aria-pressed` scenarios (from
 * `LivePreview.test.jsx`), the Save control's states including `null` = UNKNOWN, and the status
 * chip's states (from `ProjectPage.test.tsx`).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { useState } from 'react'
import { render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Routes, Route, Link, useLocation } from 'react-router-dom'
import WorkspaceShell from '../WorkspaceShell'
import { rememberProjectsSearch } from '../../../utils/projectsListMemory'
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
 * The stub lives INSIDE the row (an ordinary child, no memo between them) so its count is the
 * row's own renders — a wrapper around `WorkspaceToolbar` would only see renders its parent
 * causes, missing the ones the row's own cell subscriptions cause.
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
  previewState: null, turnRunning: false,
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
  actions = { save: null, rename: null, share: null },
  paneVisible = true,
}: SurfaceProps) {
  useWorkspaceProject(heading.projectId)
  usePublishHeading(heading)
  usePublishAddress({ url: appUrl, status: appUrl ? 'ready' : null, serving: appUrl !== null }, heading.projectId)
  // A FRESH OBJECT PER RENDER, which is what the real conversation surface publishes — the pane
  // cell is identity-compared, so this is what makes a keystroke reach the channel at all.
  usePublishPaneView({ ...EMPTY_PANE })
  usePublishSave(save, actions)
  // The row's own `save` slot carries no recovery instant — it is not the row's question — so the
  // reading published here names the flag it does have and no copy it cannot vouch for.
  usePublishSaveState({ dirty: save.dirty, recoveryAt: null })
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
    // The rail's APP STATUS section already carries this state; a chip beside the title would
    // repeat it.
    expect(screen.queryByTestId('publish-chip-stub')).toBeNull()
    expect(screen.queryByTestId('toolbar-chat-kind')).toBeNull()
  })

  it('a chat: the project, the kind pill, and the chat title', () => {
    render(<Workspace entry="/chat/c1" />)

    // On a chat, the project name drops to a breadcrumb inside the row and the chat title
    // becomes the <h1> — same row, same slots.
    expect(row().textContent).toContain('Visitor Log — Airport Office')
    expect(screen.getByTestId('toolbar-chat-kind').textContent).toContain('Build')
    expect(title().textContent).toBe('Add an out-time column')
    expect(screen.getByTestId('publish-chip-stub')).toBeTruthy()
  })

  it('★ draws the BUILD pill as the word alone, and the PLAN pill with the glyph its board has', () => {
    // Liveness: asserts the absence on BUILD AND the presence on PLAN in the same test, so a pill
    // that stopped rendering entirely could not pass this by accident.
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
    // Ordinary, not an error: the title is derived from the first message. A blank <h1> or a
    // spinner would both be worse than the kind.
    render(<Workspace entry="/chat/c1" chat={{ heading: { ...CHAT_HEADING, chatTitle: null } }} />)

    expect(title().textContent).toBe('New build')
    expect(screen.getByTestId('toolbar-chat-kind')).toBeTruthy()
  })

  it('★ a cold open, before the project name has resolved, keeps a stable name slot', () => {
    // On a chat, the project name arrives from a SECOND fetch — before it lands the slot must
    // stay stable rather than empty, or the row's contents shift the moment the fetch lands.
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
    // The scenario above keeps `chatKind: 'build'`, a state the product never actually passes
    // through; this is the state a reload or bookmark actually produces — neither project nor
    // kind resolved — and it must still read as a CHAT, not fall back to the project screen.
    render(
      <Workspace
        entry="/chat/c1"
        chat={{ heading: { projectId: null, projectName: null, chatTitle: null, chatKind: null } }}
      />,
    )

    // On the project screen "Your project" IS the <h1>; on a chat it is the breadcrumb, and the
    // <h1> is the chat's own (empty) slot.
    expect(row().textContent).toContain('Your project')
    expect(title().textContent).toBe('')
    expect(screen.queryByRole('button', { name: /rename/i })).toBeNull()
    // With no project resolved there is none to return to, so back goes to the list.
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
    // Four boards draw a clock in this row and the drawer behind it is a later feature. Not
    // built, and not stubbed either — a control that implies a drawer nobody can open is worse
    // than its absence.
    render(<Workspace />)
    expect(screen.queryByRole('button', { name: /history/i })).toBeNull()
    expect(screen.queryByRole('link', { name: /history/i })).toBeNull()
    expect(row().textContent).not.toMatch(/history/i)
  })
})

describe('the row is one element across a route change', () => {
  it('★ changes its contents and never its position, and does not remount', () => {
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
    expect(title().textContent).toBe('Visitor Log — Airport Office')
    expect(screen.getByTestId('publish-chip-stub')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Show details' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Back to projects' })).toBeTruthy()
  })

  it('★ collapses in BOTH directions, because below the threshold the columns stack', () => {
    // Below the stacking threshold the rail is a flex COLUMN, not a ROW: `w-0 flex-shrink-0`
    // collapses width but leaves height alone, so "Hide details" left a visible empty band and
    // pushed the app pane off screen. jsdom lays nothing out, so the class is what gets pinned.
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
    // Moved from `LivePreview.test.jsx` with the control; the WIDTH half (that Tablet reaches
    // 834px) stays there, since the card is the pane's.
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
    render(
      <Workspace
        project={{ heading: PROJECT_HEADING, save, actions: { save: onSave, rename: null, share: null } }}
      />,
    )

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
    // Today's project screen deliberately publishes no `onSave`. The state is worth showing and
    // a press would do nothing, so nothing invites one. Mutation receipt: render a `<button>`
    // unconditionally and this goes red.
    withSave({ dirty: false, saving: false, error: null }, null)
    expect(screen.queryByTestId('save-project')).toBeNull()
    expect(screen.getByTestId('save-state').textContent).toContain('Saved')
  })

  it('renders no control with a real disabled attribute', () => {
    withSave({ dirty: false, saving: false, error: null }, () => {})
    for (const el of screen.getAllByRole('button')) expect(el.hasAttribute('disabled')).toBe(false)
  })

  // THE WAIT ITSELF
  //
  // The control between a citizen and losing their work was the quietest wait in the product: the
  // word changed and nothing else did. Every scenario above stayed green through the removal of
  // the spinner, because a LABEL assertion cannot tell a moving control from a still one — so
  // these four are about the other two registers, and about the class name that carries the
  // reduced-motion guarantee.

  it('★ while it saves it SHOWS the wait: a spinner, `aria-busy`, and the sentence', () => {
    // Mutation receipt: delete the `Loader2` arm and this line goes red — the one no assertion in
    // this suite caught until now.
    withSave({ dirty: true, saving: true, error: null }, () => {})
    const save = screen.getByTestId('save-project')
    expect(screen.getByTestId('save-spinner')).toBeTruthy()
    expect(save.getAttribute('aria-busy')).toBe('true')
    expect(save.textContent).toContain('Saving…')
  })

  it('is still, and claims nothing, in both of the states it rests in', () => {
    // `aria-busy="false"` on a control that is not waiting is an answer where none was asked for,
    // so the attribute is absent rather than negative — hence `hasAttribute`, not a value compare.
    withSave({ dirty: true, saving: false, error: null }, () => {})
    const dirtyChip = screen.getByTestId('save-project')
    expect(screen.queryByTestId('save-spinner')).toBeNull()
    expect(dirtyChip.hasAttribute('aria-busy')).toBe(false)
    expect(dirtyChip.textContent).toContain('Save')

    cleanup()

    withSave({ dirty: false, saving: false, error: null }, () => {})
    const cleanChip = screen.getByTestId('save-project')
    expect(screen.queryByTestId('save-spinner')).toBeNull()
    expect(cleanChip.hasAttribute('aria-busy')).toBe(false)
    expect(cleanChip.textContent).toContain('Saved')
  })

  it('★ the spinner wears the class the reduced-motion block covers — the half jsdom cannot check', () => {
    // WHY THE CLASS NAME AND NOT MERE PRESENCE. jsdom cannot evaluate
    // `@media (prefers-reduced-motion: reduce)`, so "a spinner is on screen" is equally green for a
    // citizen who asked everything to stop moving and for one who did not — which is precisely the
    // gap that let the original removal ship in silence. `index.css` suppresses by UTILITY, so the
    // checkable half here is that this spinner is spelled with the utility it suppresses;
    // `src/__tests__/reducedMotion.test.ts` holds the other half — that the block still names it.
    withSave({ dirty: true, saving: true, error: null }, () => {})
    expect(screen.getByTestId('save-spinner').classList.contains('animate-spin')).toBe(true)
  })

  it('★ announces the wait by WRAPPING its one sentence, in a region that was already mounted', () => {
    // TWO WAYS TO ANNOUNCE A WAIT BADLY, and this pins against both. A second `sr-only` copy is
    // the on-screen sentence read twice — the shape `Announcer.tsx` records as having broken three
    // tests — and a live region inserted TOGETHER with its text is missed entirely by several
    // reader-and-browser combinations, which is why `TurnBanner.tsx` keeps a permanent region and
    // lets only the box inside it appear. So: the same region element before and after, empty
    // first, and exactly one copy of the sentence.
    const view = withSave({ dirty: true, saving: false, error: null }, () => {})
    const before = within(screen.getByTestId('save-project')).getByRole('status')
    expect(before.getAttribute('aria-live')).toBe('polite')
    expect(before.textContent).toBe('')

    view.rerender(
      <Workspace
        project={{
          heading: PROJECT_HEADING,
          save: { dirty: true, saving: true, error: null },
          actions: { save: () => {}, rename: null, share: null },
        }}
      />,
    )

    const after = within(screen.getByTestId('save-project')).getByRole('status')
    expect(after).toBe(before)
    expect(after.textContent).toBe('Saving…')
    // One element carries the sentence — `getNodeText` reads only direct text children, so a
    // duplicate anywhere in the control would make this two.
    expect(screen.getAllByText('Saving…')).toHaveLength(1)
  })
})

describe('the status chip — where the state is said, and where it would be said twice', () => {
  // The chip duplicates the rail's APP STATUS section, so it appears only where that section is
  // NOT: on a chat (no rail section) or over a collapsed rail. On the open project screen it
  // would be a second rendering of the same fact, which is what this row exists to prevent.

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
    render(<Workspace project={{ heading: PROJECT_HEADING, actions: { save: null, rename, share: null } }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Rename project' }))
    expect(rename).toHaveBeenCalledTimes(1)

    cleanup()
    // A chat's title is the agent's, not the citizen's.
    render(<Workspace entry="/chat/c1" />)
    expect(screen.queryByRole('button', { name: /rename/i })).toBeNull()
  })

  it('★ no pencil over a project that never loaded, and one the moment it does', () => {
    /* THE MANGLED ADDRESS, in the only shape this row can see it. `projectId` is the ROUTE PARAM,
       so it is still there on a page whose project 422'd at the boundary — which is exactly what
       the pencil used to be gated on, and why a citizen who followed a truncated link was offered
       a rename control whose press was a measured no-op (`NO_ACTIONS.rename` is `null`). The NAME
       is the field that comes from the project's own fetch, so it is the one that means loaded. */
    render(<Workspace project={{ heading: { ...PROJECT_HEADING, projectName: null } }} />)

    expect(screen.queryByRole('button', { name: /rename/i })).toBeNull()
    // LIVENESS: the row is fully drawn around that absence — full height, a word in the name
    // slot — so this is a control that is gone rather than a tree that failed to render.
    expect(row().className).toMatch(/h-\[54px\]/)
    expect(title().textContent).toBe('Your project')

    cleanup()
    const rename = vi.fn()
    render(<Workspace project={{ heading: PROJECT_HEADING, actions: { save: null, rename, share: null } }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Rename project' }))
    expect(rename).toHaveBeenCalledTimes(1)
  })

  it('★ the way out is NOT gated by the fact that silences the pencil', () => {
    /* THE MUTANT THIS EXISTS FOR: gate the back control on `heading.projectName !== null` too and
       the dead address becomes a dead end. The row's own docblock is explicit that the control
       survives the load-error branch, and a branch with no way off it is worse than the raw
       validator sentence this unit came to remove. */
    render(<Workspace project={{ heading: { ...PROJECT_HEADING, projectName: null } }} />)

    fireEvent.click(screen.getByRole('button', { name: 'Back to projects' }))
    expect(screen.getByTestId('where').textContent).toBe('/projects')
  })

  it('★ the deliberate "Your project" fallback on a chat is untouched', () => {
    /* NOT PART OF THE DEFECT, and deliberately left as it is. A project deleted out from under
       an open chat leaves the breadcrumb with no name, and the row deliberately says "Your project"
       rather than leaving a gap that shifts the layout when a fetch lands. The pencil gate is
       allowed to read the same `null`; it is not allowed to change what the slot says. */
    render(<Workspace entry="/chat/c1" chat={{ heading: { ...CHAT_HEADING, projectName: null } }} />)

    expect(row().textContent).toContain('Your project')
    expect(title().textContent).toBe('Add an out-time column')
    expect(screen.getByRole('button', { name: 'Back to project' })).toBeTruthy()
    // Rename is a project-screen control; a chat address never had it, name or no name.
    expect(screen.queryByRole('button', { name: /rename/i })).toBeNull()
  })
})

describe('the back control carries the projects list state back', () => {
  /* `page`, `pageSize` and `q` live in `/projects`'s own address, which is a one-way fix:
     reading it in is `ProjectsPage.test.tsx`'s job. This control is mounted on a DIFFERENT
     address — a project, a chat — and has to name a destination without ever having read that
     query string itself. Before this it hardcoded a bare `/projects`, so leaving a filtered,
     paged list and pressing Back landed on page one with the search cleared:
     `projectsListMemory.ts`'s own docblock calls this "addressable in one direction and silent
     in the other". `Navbar` is the one place that remembers what the address bar carried, since
     `ProjectsPage` mounts its own instance of it — this suite only has to seed that memory. */

  function WhereFull() {
    return <span data-testid="where-full">{useLocation().pathname + useLocation().search}</span>
  }

  function renderAt(entry: string) {
    return render(
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route element={<WorkspaceShell />}>
            <Route path="/projects/:projectId" element={<Surface heading={PROJECT_HEADING} />} />
          </Route>
          <Route path="/projects" element={<WhereFull />} />
        </Routes>
      </MemoryRouter>,
    )
  }

  afterEach(() => {
    // Leaves the module in the same "nothing remembered" state it starts in — every other test
    // in this file presses this same button and expects a bare `/projects`.
    rememberProjectsSearch('')
  })

  it('★ returns to the remembered page, search and page size — not to page one', () => {
    rememberProjectsSearch('?page=2&pageSize=20&q=ramp')
    renderAt('/projects/pA')

    // LIVENESS FIRST: the project screen actually rendered — not a crash a bare presence check
    // on the destination below would miss.
    expect(screen.getByTestId('surface')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Back to projects' }))

    expect(screen.getByTestId('where-full').textContent).toBe('/projects?page=2&pageSize=20&q=ramp')
  })

  it('falls back to the bare list when nothing has been remembered this session', () => {
    renderAt('/projects/pA')

    fireEvent.click(screen.getByRole('button', { name: 'Back to projects' }))

    expect(screen.getByTestId('where-full').textContent).toBe('/projects')
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

/**
 * THE NARROW-WIDTH CONTRACT
 *
 * EVERY SCENARIO BELOW IS STRUCTURAL, AND THE NAMES SAY SO. jsdom has no layout engine:
 * `getBoundingClientRect()` returns zeroes for every element on this page, `matchMedia` evaluates
 * nothing, and no Tailwind utility exists in the unit environment at all. A test here that called
 * itself "measures 44×44" would be asserting a class name and lying about it — which is precisely
 * the false pass this campaign has already produced once, and the reason `reducedMotion.test.ts`
 * exists as source-text rules rather than as a DOM test.
 *
 * So the division of labour is explicit. THESE scenarios pin the structure: which class is on
 * which control, that the floors are variant-gated so nothing changes above the threshold, that
 * the glyph attributes are untouched, and that the `narrow` screen those variants depend on is
 * really declared — without it every `narrow:` class compiles to nothing and each assertion below
 * would still be green. THE MEASUREMENTS — 44×44 in CSS pixels at a 360px viewport, a non-zero
 * title width, a row that scrolls instead of clipping — belong to the browser suite, where a
 * layout engine actually runs.
 *
 * WHAT IS DELIBERATELY NOT HERE: an overflow menu. Of the two remedies available — a collapsing
 * menu, or a scroller on the row — the lighter one shipped: the row scrolls, and all nine
 * occupants stay on it. `every occupant is still on the row` below is what makes a future
 * re-introduction of the menu go red rather than quietly ship.
 */
describe('the narrow-width contract — STRUCTURAL assertions, never measurements', () => {
  const cls = (el: Element) => el.getAttribute('class') ?? ''
  /** The class list a viewport ABOVE the stacking threshold actually sees — every `narrow:` token
   *  dropped, which is the only honest way jsdom can ask "did anything change up there?". */
  const aboveThreshold = (el: Element) =>
    cls(el)
      .split(/\s+/)
      .filter((token) => !token.startsWith('narrow:'))
      .join(' ')
  /** Lucide writes `size` straight onto the svg's width/height attributes, so the DRAWN size is
   *  readable in jsdom even though the laid-out one is not. */
  const glyph = (el: Element) => {
    const svg = el.querySelector('svg')
    return svg === null ? null : `${svg.getAttribute('width')}×${svg.getAttribute('height')}`
  }

  /** The project screen with an app framed, unsaved work, and the rail collapsed — the one state
   *  in which all nine of the row's occupants are on screen at once. */
  const everything = () => {
    render(
      <Workspace
        project={{
          heading: PROJECT_HEADING,
          save: { dirty: true, saving: false, error: null },
          actions: { save: () => {}, rename: () => {}, share: null },
        }}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Hide details' }))
  }

  const back = () => screen.getByRole('button', { name: 'Back to projects' })
  const pencil = () => screen.getByRole('button', { name: 'Rename project' })
  const devices = () => ['Desktop', 'Tablet', 'Mobile'].map((name) => screen.getByRole('button', { name }))
  const reload = () => screen.getByRole('button', { name: 'Reload your app' })
  const newTab = () => screen.getByRole('link', { name: 'Open your app in a new tab' })
  const save = () => screen.getByTestId('save-project')
  const railToggle = () => screen.getByRole('button', { name: 'Show details' })

  it('★ the row owns a horizontal scroller, so overflow is reachable instead of clipped', () => {
    // THE DEFECT IN ONE LINE. The shell's root is `overflow-hidden` for the rail and the pane, so
    // what did not fit in this row was not merely off to the right — it was clipped, with nothing
    // anywhere to bring it back, and Save was the control it took away. The scroller is on the ROW
    // rather than on the root because the row's own box never exceeds the root's width; only its
    // contents do.
    everything()
    expect(cls(row())).toMatch(/\boverflow-x-auto\b/)
    // Not decoration: `overflow-x: auto` alone computes `overflow-y` to `auto` too, which would
    // put a second scrollbar inside a 54px row the moment the first one took height from it.
    expect(cls(row())).toMatch(/\boverflow-y-hidden\b/)
    expect(cls(row())).toMatch(/h-\[54px\]/)
  })

  it('★ every occupant is still on the row — nothing was moved into a menu', () => {
    // The guard on the remedy that was NOT taken. Either a collapsing menu or a scrolling ancestor
    // would have fixed the clipping; the scroller shipped, so all nine stay put. If an overflow
    // menu is ever added, this goes red before anyone has to notice the row lost a control.
    everything()
    expect(back()).toBeTruthy()
    expect(title().textContent).toBe('Visitor Log — Airport Office')
    expect(screen.getByTestId('publish-chip-stub')).toBeTruthy()
    expect(pencil()).toBeTruthy()
    expect(devices()).toHaveLength(3)
    expect(reload()).toBeTruthy()
    expect(newTab()).toBeTruthy()
    expect(save()).toBeTruthy()
    expect(railToggle()).toBeTruthy()
  })

  it('★ every pressable control declares the 44px floor below the stacking threshold', () => {
    // STRUCTURAL: this asserts the class, not the rectangle. The rectangle is the browser suite's.
    // The floor is `min-h`/`min-w` rather than a bigger glyph — that class declaration is the half
    // jsdom CAN see; `hit areas grow by padding` below is its other half.
    everything()
    for (const control of [back(), pencil(), ...devices(), reload(), newTab(), railToggle()]) {
      expect(cls(control)).toContain('narrow:min-h-[44px]')
      expect(cls(control)).toContain('narrow:min-w-[44px]')
      // A 44px box with a 15px glyph in its top-left corner is not a 44px target anyone can aim
      // at. Both axes have to centre.
      expect(cls(control)).toMatch(/\bitems-center\b/)
      expect(cls(control)).toMatch(/\bjustify-center\b/)
    }
    // Save is past 44px wide on its own words in every state, so only its HEIGHT needs a floor.
    expect(cls(save())).toContain('narrow:min-h-[44px]')
    expect(cls(save())).not.toContain('narrow:min-w-[44px]')
  })

  it('★ the hit areas grow by padding — every glyph keeps the size the canvas drew it at', () => {
    // The requirement is a bigger TARGET, not a bigger picture. Growing the icons would be the
    // easy way to 44×44 and would rebuild the row's visual weight at every width.
    everything()
    expect(glyph(back())).toBe('16×16')
    expect(glyph(pencil())).toBe('13×13')
    for (const device of devices()) expect(glyph(device)).toBe('14×14')
    expect(glyph(reload())).toBe('15×15')
    expect(glyph(newTab())).toBe('15×15')
    expect(glyph(railToggle())).toBe('15×15')
    expect(glyph(save())).toBe('14×14')
  })

  it('★ above the stacking threshold every control keeps exactly the geometry it shipped with', () => {
    // The whole point of gating the floors behind a variant. Strip the `narrow:` tokens — which is
    // what a 1200px viewport does — and what is left must be the pre-existing class list, with no
    // 44px floor leaking up into the desktop row.
    everything()
    const desktop = [back(), pencil(), ...devices(), reload(), newTab(), railToggle(), save()].map(aboveThreshold)
    for (const list of desktop) expect(list).not.toMatch(/min-[hw]-\[44px\]/)

    // …and the sizes those controls are actually drawn at up there, named so a silent change to
    // any of them has to be deliberate: 20×20, 21×21, 28×32, 28×30.
    expect(aboveThreshold(back())).toContain('p-0.5')
    expect(aboveThreshold(pencil())).toContain('p-1')
    for (const device of devices()) expect(aboveThreshold(device)).toMatch(/\bh-7\b.*\bw-8\b/)
    expect(aboveThreshold(railToggle())).toMatch(/\bh-7\b.*\bw-\[30px\]/)
  })

  it('★ the title carries a floor of its own, and still truncates', () => {
    // WHY IT COLLAPSED TO ZERO. Every sibling in this row is `flex-shrink-0` and the title carried
    // `min-w-0` with no floor — so it was the only flexible participant, and 100% of any width
    // deficit landed on it, all the way down. 144px is about ten characters and the ellipsis.
    everything()
    expect(cls(title())).toContain('narrow:min-w-[9rem]')
    // The floor must not cost the truncation: a long name still has to end in an ellipsis rather
    // than push Save off the row.
    expect(cls(title())).toMatch(/\btruncate\b/)
    expect(cls(title())).toMatch(/\bmin-w-0\b/)

    // The chat shape's heading is a different <h1> in a different branch, and it needs the floor
    // for the same reason.
    cleanup()
    render(<Workspace entry="/chat/c1" />)
    expect(title().textContent).toBe('Add an out-time column')
    expect(cls(title())).toContain('narrow:min-w-[9rem]')
    expect(cls(title())).toMatch(/\btruncate\b/)
  })

  it("★ the title's floor is variant-gated, so a SHORT name is untouched above the threshold", () => {
    // NOT A STYLE PREFERENCE — a real defect an ungated `min-width` would have shipped. In flex,
    // `min-width` does not only stop an item shrinking, it GROWS one whose content is narrower
    // than the floor. Ungated, "Ops" would be padded out to 144px at every width and shove the
    // status chip and the rename pencil away from the name they belong to.
    render(<Workspace project={{ heading: { ...PROJECT_HEADING, projectName: 'Ops' } }} />)
    expect(title().textContent).toBe('Ops')
    expect(aboveThreshold(title())).not.toMatch(/min-w-\[9rem\]/)
    // Liveness: the floor is genuinely on this element — the assertion above is a gate, not an
    // absence that would pass just as well if the floor had never been added.
    expect(cls(title())).toContain('narrow:min-w-[9rem]')
  })

  it('a very long title truncates rather than shrinking the row or pushing anything off it', () => {
    // STRUCTURAL HALF of the edge case: jsdom cannot show that the text is clipped, but it can
    // show that the row did not respond by dropping occupants, and that the full string is still
    // in the accessibility tree for a reader that does not care about pixels.
    const long = 'Add an out-time column and a visitor photo and an escort field and a badge number'
    render(
      <Workspace
        entry="/chat/c1"
        chat={{
          heading: { ...CHAT_HEADING, chatTitle: long },
          save: { dirty: true, saving: false, error: null },
          actions: { save: () => {}, rename: null, share: null },
        }}
      />,
    )

    expect(title().textContent).toBe(long)
    expect(cls(title())).toMatch(/\btruncate\b/)
    expect(cls(row())).toMatch(/h-\[54px\]/)
    expect(screen.getByRole('button', { name: 'Back to project' })).toBeTruthy()
    expect(save()).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Reload your app' })).toBeTruthy()
  })
})

/**
 * THE SCREEN THOSE VARIANTS DEPEND ON, ASSERTED AGAINST THE CONFIG SOURCE.
 *
 * Without `narrow` in `tailwind.config.js`, every `narrow:`-prefixed class above compiles to
 * absolutely nothing and every scenario in the block above stays green while the product ships
 * 15×15 targets. That is the one failure mode a class-name suite cannot see from inside the DOM,
 * so it is checked here instead — the same reason `reducedMotion.test.ts` reads this file as text.
 *
 * (What jsdom still cannot do is prove the class RESOLVES. Compiling the real config through
 * PostCSS emits `@media (max-width: 1099.98px){ .narrow\:min-h-\[44px\]{min-height:44px} }`, which
 * was checked out of band; the browser suite is where it is checked continuously.)
 */
describe('the narrow screen is declared, and does not overlap the wide one', () => {
  const CONFIG = readFileSync(path.resolve(process.cwd(), 'tailwind.config.js'), 'utf8')
  // Closed on `\n<indent>},` rather than on the first `},`, which `narrow: { max: … },` supplies
  // one line early — a reader that stopped there would drop the very entry it exists to find.
  const SCREENS_BLOCK = /screens:\s*\{([\s\S]*?)\n\s*\},/
  const capture = (pattern: RegExp, source: string) => source.match(pattern)?.[1] ?? null
  const screens = (source: string) => {
    const block = capture(SCREENS_BLOCK, source) ?? ''
    return {
      wide: capture(/\bwide:\s*'(\d+(?:\.\d+)?)px'/, block),
      narrowMax: capture(/\bnarrow:\s*\{\s*max:\s*'(\d+(?:\.\d+)?)px'\s*\}/, block),
    }
  }

  it('★ declares `narrow` as a max-width screen below the 1100px stacking threshold', () => {
    const { wide, narrowMax } = screens(CONFIG)
    expect(wide).toBe('1100')
    expect(narrowMax).not.toBeNull()
    // Disjoint by construction: no element may ever be inside both variants at once.
    expect(Number(narrowMax)).toBeLessThan(Number(wide))
  })

  it('★ has no `min` on that screen — the floors must survive below 360px, not switch off', () => {
    // A `min: '360px'` would take the finger-sized targets away from exactly the narrowest devices
    // that need them. 360px is the DECLARED minimum supported width, which means every control is
    // reachable there — not that the CSS stops caring underneath it.
    const block = capture(SCREENS_BLOCK, CONFIG)
    expect(block).not.toBeNull()
    expect(block ?? '').not.toMatch(/\bmin:\s*'/)
    // …and the reader is told what "declared minimum" does and does not promise, because the
    // shared navbar still overflows 360px by ~18px on three routes.
    expect(CONFIG).toMatch(/360px/)
  })

  it('the reader goes red rather than silently matching nothing', () => {
    // Every pattern above answers `null` on a config that does not declare these screens, so the
    // assertions are pure functions over a string that a broken parser cannot fake green.
    const gutted = CONFIG.replace(/\bnarrow:\s*\{\s*max:\s*'[\d.]+px'\s*\},?/, '')
    expect(screens(gutted).narrowMax).toBeNull()
    expect(screens(gutted).wide).toBe('1100')
  })
})
