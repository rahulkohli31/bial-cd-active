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
import NavReveal from '../../layout/NavReveal'
import { rememberProjectsSearch } from '../../../utils/projectsListMemory'
import { formatStamp } from '../../../utils/publishPresentation'
import {
  useAppPaneVisible,
  usePublishAddress,
  usePublishHeading,
  usePublishPaneView,
  usePublishSave,
  useWorkspaceProject,
  type PaneView,
  type SaveSlot,
  type WorkspaceActions,
  type WorkspaceHeading,
} from '../workspaceChannel'

/**
 * The stub lives INSIDE the row (an ordinary child, no memo between them) so its count is the
 * row's own renders — a wrapper around `WorkspaceToolbar` would only see renders its parent
 * causes, missing the ones the row's own cell subscriptions cause.
 */
const h = vi.hoisted(() => ({ rowRenders: 0, usage: vi.fn() }))
// The token counter's read. It is the NAVIGATION's read too — one hook, so the two surfaces can
// never disagree about the same day's figures — which is why it is stubbed at the hook rather
// than at the network.
vi.mock('../../../hooks/useUsageToday', () => ({ useUsageToday: h.usage }))
vi.mock('../../PublishStatusChip', () => ({
  default: function PublishStatusChipStub({ projectId }: { projectId: string }) {
    h.rowRenders += 1
    return <span data-testid="publish-chip-stub" data-project={projectId} />
  },
}))
vi.mock('../../LivePreview', () => ({
  default: () => <div data-testid="live-preview" />,
}))
vi.mock('../../../hooks/usePublishState', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../hooks/usePublishState')>()),
  usePublishState: () => ({ deployment: { savedAt: '2026-09-13T14:32:00Z' } }),
}))

const USAGE = { used: 42_000, limit: 100_000, remaining: 58_000 }

const APP_URL = 'https://app-a.example.azurecontainerapps.io/'
const SAVED_AT = '2026-09-13T14:32:00Z'

const EMPTY_PANE: PaneView = {
  reconnecting: false,
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
  /** Unset fields take `QUIET_SAVE`'s value. */
  save?: Partial<Omit<SaveSlot, 'canSave' | 'canDiscard'>>
  /** Unset handlers are `null`. */
  actions?: Partial<WorkspaceActions>
  paneVisible?: boolean
}

const QUIET_SAVE: Omit<SaveSlot, 'canSave' | 'canDiscard'> = {
  dirty: null,
  saving: false,
  error: null,
  discarding: false,
  replying: false,
  hasSavedVersion: false,
}

/** A mounted surface, publishing exactly what the row reads and nothing else. */
function Surface({
  heading,
  appUrl = APP_URL,
  save = {},
  actions = {},
  paneVisible = true,
}: SurfaceProps) {
  const slot = { ...QUIET_SAVE, ...save }
  useWorkspaceProject(heading.projectId)
  usePublishHeading(heading)
  usePublishAddress({ url: appUrl, status: appUrl ? 'ready' : null, serving: appUrl !== null }, heading.projectId)
  // A FRESH OBJECT PER RENDER, which is what the real conversation surface publishes — the pane
  // cell is identity-compared, so this is what makes a keystroke reach the channel at all.
  usePublishPaneView({ ...EMPTY_PANE })
  usePublishSave(slot, { save: null, discard: null, settings: null, share: null, ...actions })
  useAppPaneVisible(paneVisible)
  return <div data-testid="surface" />
}

function Where() {
  return <span data-testid="where">{useLocation().pathname}</span>
}

/**
 * Both addresses under ONE shell, navigated by link exactly as the product navigates them.
 *
 * `nav` puts the real `NavReveal` around it, which is what the shell does in the product. Most
 * tests here are about the row alone and do not need it; the ones about what the row shows WHILE
 * THE PANEL IS ON SCREEN cannot be written without it — the menu button renders nothing when
 * there is no reveal to open.
 */
function Workspace({
  entry = '/projects/pA',
  project,
  chat,
  nav = false,
}: {
  entry?: string
  project?: SurfaceProps
  chat?: SurfaceProps
  nav?: boolean
}) {
  const inside = (
    <>
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
    </>
  )
  return (
    <MemoryRouter initialEntries={[entry]}>
      {nav ? <NavReveal hideable>{inside}</NavReveal> : inside}
    </MemoryRouter>
  )
}

const row = () => screen.getByTestId('workspace-toolbar')
const title = () => screen.getByTestId('toolbar-title')

beforeEach(() => vi.clearAllMocks())
afterEach(() => cleanup())

describe('what the row names on each address', () => {
  it('the project screen: back, the project name, and the state it is in', () => {
    render(<Workspace />)

    expect(screen.getByRole('button', { name: 'Back to My Applications' })).toBeTruthy()
    expect(title().textContent).toBe('Visitor Log — Airport Office')
    expect(title().tagName).toBe('H1')
    expect(screen.getByTestId('publish-chip-stub').getAttribute('data-project')).toBe('pA')
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
    expect(row().textContent).toContain('Your application')
    expect(screen.getByRole('button', { name: 'Back to the application' })).toBeTruthy()
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
    expect(row().textContent).toContain('Your application')
    expect(title().textContent).toBe('')
    expect(screen.queryByRole('button', { name: /rename/i })).toBeNull()
    // With no project resolved there is none to return to, so back goes to the list.
    expect(screen.getByRole('button', { name: 'Back to My Applications' })).toBeTruthy()
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

    fireEvent.click(screen.getByRole('button', { name: 'Back to the application' }))
    expect(screen.getByTestId('where').textContent).toBe('/projects/pA')
  })

  it('★ when the project fetch FAILED the row still names something and back still works', () => {
    // A project deleted out from under an open chat. `null` is the same value for "not yet" and
    // "never", and in both the row keeps its height, its word and its way out.
    render(<Workspace entry="/chat/c1" chat={{ heading: { ...CHAT_HEADING, projectName: null } }} />)

    fireEvent.click(screen.getByRole('button', { name: 'Back to the application' }))
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
    fireEvent.click(screen.getByRole('button', { name: 'Hide the chat' }))

    const rail = screen.getByTestId('workspace-outlet')
    expect(rail.className).toMatch(/(^|\s)w-0(\s|$)/)
    expect(rail.className).toMatch(/invisible/)
    expect(title().textContent).toBe('Visitor Log — Airport Office')
    expect(screen.getByTestId('publish-chip-stub')).toBeTruthy()
    // ★ TWO WAYS BACK, not one. The toolbar control was the only route out of a collapse, which
    // made a single control the single point of failure for a state that hides a whole column.
    // The stub the chat leaves behind carries the second — and it is a SIBLING of the hidden
    // column rather than inside it, for the same reason the toolbar control is not in the rail.
    expect(screen.getAllByRole('button', { name: 'Show the chat' })).toHaveLength(2)
    expect(screen.getByTestId('chat-stub')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Back to My Applications' })).toBeTruthy()
  })

  it('★ collapses in BOTH directions, because below the threshold the columns stack', () => {
    // Below the stacking threshold the rail is a flex COLUMN, not a ROW: `w-0 flex-shrink-0`
    // collapses width but leaves height alone, so "Hide the chat" left a visible empty band and
    // pushed the app pane off screen. jsdom lays nothing out, so the class is what gets pinned.
    render(<Workspace />)
    fireEvent.click(screen.getByRole('button', { name: 'Hide the chat' }))

    const rail = screen.getByTestId('workspace-outlet')
    expect(rail.className).toMatch(/(^|\s)h-0(\s|$)/)
    expect(rail.className).toMatch(/(^|\s)w-0(\s|$)/)
    // Liveness: the same press still hides it, so this is not passing on a rail that never collapsed.
    expect(rail.className).toMatch(/invisible/)
  })

  it('the toggle points at the rail it hides', () => {
    render(<Workspace />)
    const toggle = screen.getByRole('button', { name: 'Hide the chat' })
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(toggle.getAttribute('aria-controls')).toBe(screen.getByTestId('workspace-outlet').id)

    // ★ AND SO DOES THE STUB, which is what makes it a real second route rather than a decoration:
    // it names the same column and reports the same state.
    fireEvent.click(toggle)
    const stub = within(screen.getByTestId('chat-stub')).getByRole('button')
    expect(stub.getAttribute('aria-expanded')).toBe('false')
    expect(stub.getAttribute('aria-controls')).toBe(screen.getByTestId('workspace-outlet').id)
    fireEvent.click(stub)
    expect(screen.getByTestId('workspace-outlet').className).not.toMatch(/invisible/)
  })

  it('is absent when there is no pane to give the screen to', () => {
    render(<Workspace project={{ heading: PROJECT_HEADING, paneVisible: false }} />)
    expect(screen.queryByRole('button', { name: /hide the chat/i })).toBeNull()
    // The divider goes with it — a separator with one side is just a line.
    expect(screen.queryByTestId('chat-stub')).toBeNull()
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
    // Moved from `LivePreview.test.jsx` with the control; the WIDTH half (that Mobile reaches
    // 834px) stays there, since the card is the pane's.
    render(<Workspace />)
    const at = (name: string) => screen.getByRole('button', { name }).getAttribute('aria-pressed')

    expect(at('Desktop')).toBe('true')
    expect(at('Mobile')).toBe('false')

    fireEvent.click(screen.getByRole('button', { name: 'Mobile' }))
    expect(at('Mobile')).toBe('true')
    expect(at('Desktop')).toBe('false')
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
  const withSave = (save: Partial<Omit<SaveSlot, 'canSave' | 'canDiscard'>>, onSave: (() => void) | null = null) =>
    render(
      <Workspace
        project={{ heading: PROJECT_HEADING, save, actions: { save: onSave, settings: null, share: null } }}
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
          actions: { save: () => {}, settings: null, share: null },
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

describe('the Discard control', () => {
  const OPEN_WORK = { dirty: true, saving: false, error: null, discarding: false, replying: false, hasSavedVersion: true }
  const withDiscard = (
    save: Partial<Omit<SaveSlot, 'canSave' | 'canDiscard'>>,
    handlers: Partial<WorkspaceActions> = {},
  ) =>
    render(
      <Workspace
        project={{
          heading: PROJECT_HEADING,
          save: { ...OPEN_WORK, ...save },
          actions: { save: () => {}, discard: async () => {}, ...handlers },
        }}
      />,
    )
  const discard = () => screen.getByTestId('discard-changes')

  it.each([
    ['unsaved work over a saved version', {}, null],
    ['work that was never saved', { hasSavedVersion: false }, 'Nothing saved yet to go back to'],
    ['everything saved', { dirty: false }, 'No unsaved changes'],
    ['a save running', { saving: true }, 'Wait for the save to finish'],
    ['a reply running', { replying: true }, 'Wait for the reply to finish'],
    ['a discard running', { discarding: true }, 'Discarding your changes'],
  ] as const)('★ with %s it sits left of Save and says whether it can be pressed', (_, save, refusal) => {
    // Mutation check: switch to a real `disabled`, or drop the saved-version gate, and a row goes red.
    withDiscard(save)
    const control = discard()
    expect(control.nextElementSibling?.contains(screen.getByTestId('save-project'))).toBe(true)
    expect(control.hasAttribute('disabled')).toBe(false)
    expect(control.getAttribute('aria-disabled')).toBe(String(refusal !== null))
    expect(control.getAttribute('title')).toBe(refusal ?? 'Go back to the version you last saved')
    if (refusal === null) {
      expect(control.className).toMatch(/hover:text-danger/)
      expect(control.className).not.toMatch(/cursor-not-allowed/)
    } else {
      expect(control.className).toMatch(/cursor-not-allowed/)
      expect(control.className).not.toMatch(/hover:/)
      fireEvent.click(control)
      expect(screen.queryByTestId('discard-dialog-confirm')).toBeNull()
    }
  })

  it.each([
    ['a discard starting', { discarding: true }, {}, 'refused'],
    ['a reply starting', { replying: true }, {}, 'refused'],
    ['the saved version going away', { hasSavedVersion: false }, {}, 'refused'],
    ['the discard action going away', {}, { discard: null }, 'hidden'],
  ] as const)('★ %s alone reaches the row', (_, save, handlers, expected) => {
    // Mutation check: leave the field out of `sameSave` and the row keeps its earlier answer.
    const view = withDiscard({})
    expect(discard().getAttribute('aria-disabled')).toBe('false')

    view.rerender(
      <Workspace
        project={{
          heading: PROJECT_HEADING,
          save: { ...OPEN_WORK, ...save },
          actions: { save: () => {}, discard: async () => {}, ...handlers },
        }}
      />,
    )

    if (expected === 'hidden') expect(screen.queryByTestId('discard-changes')).toBeNull()
    else expect(discard().getAttribute('aria-disabled')).toBe('true')
  })

  it('shows the wait while a discard runs, and Save cannot be pressed under it', () => {
    const onSave = vi.fn()
    withDiscard({ discarding: true }, { save: onSave })
    expect(discard().textContent).toContain('Discarding…')
    expect(screen.getByTestId('discard-spinner')).toBeTruthy()
    const save = screen.getByTestId('save-project')
    expect(save.getAttribute('aria-disabled')).toBe('true')
    fireEvent.click(save)
    expect(onSave).not.toHaveBeenCalled()
  })

  it('★ is hidden whenever Save is, and when nothing can discard', () => {
    withDiscard({ dirty: null })
    expect(screen.queryByTestId('discard-changes')).toBeNull()
    expect(title().textContent).toBe('Visitor Log — Airport Office')

    cleanup()
    withDiscard({}, { discard: null })
    expect(screen.queryByTestId('discard-changes')).toBeNull()
    expect(screen.getByTestId('save-project')).toBeTruthy()
  })

  it('★ asks first, dated from the last save, and only a confirm discards', async () => {
    const onDiscard = vi.fn(async () => {})
    withDiscard({}, { discard: onDiscard })

    fireEvent.click(discard())
    expect(screen.getByText(new RegExp(formatStamp(SAVED_AT)))).toBeTruthy()
    fireEvent.click(screen.getByTestId('discard-dialog-cancel'))
    await waitFor(() => expect(screen.queryByTestId('discard-dialog-confirm')).toBeNull())
    expect(onDiscard).not.toHaveBeenCalled()

    fireEvent.click(discard())
    fireEvent.click(screen.getByTestId('discard-dialog-confirm'))
    await waitFor(() => expect(screen.queryByTestId('discard-dialog-confirm')).toBeNull())
    expect(onDiscard).toHaveBeenCalledTimes(1)
  })
})

describe('the status chip — the one place that says what state the application is in', () => {
  // NOTHING ELSE ON THIS SCREEN SAYS IT. The rail beside the toolbar carries a composer and
  // nothing else, so a chip gated on anything leaves a citizen unable to tell a draft from
  // something live without changing the layout first.

  it('★ names the project on a chat', () => {
    render(<Workspace entry="/chat/c1" />)
    expect(screen.getByTestId('publish-chip-stub').getAttribute('data-project')).toBe('pA')
  })

  it('★ stays put across the one layout change a citizen can make here', () => {
    // Collapsing the chat is the gesture that used to be REQUIRED to see the state at all. It is
    // now the gesture that must not change the answer — which is the half a "renders the chip"
    // test on a single layout cannot see.
    render(<Workspace />)
    expect(screen.getByTestId('publish-chip-stub').getAttribute('data-project')).toBe('pA')

    fireEvent.click(screen.getByRole('button', { name: 'Hide the chat' }))
    expect(screen.getByTestId('publish-chip-stub').getAttribute('data-project')).toBe('pA')
  })

  it('is drawn ONCE — one mount, not two that could word a state differently', () => {
    render(<Workspace entry="/chat/c1" />)
    expect(screen.getAllByTestId('publish-chip-stub')).toHaveLength(1)
  })

  it('renders nothing where there is no project to name', () => {
    render(<Workspace entry="/chat/c1" chat={{ heading: { ...CHAT_HEADING, projectId: null } }} />)
    expect(screen.queryByTestId('publish-chip-stub')).toBeNull()
  })
})

describe('the back control, and the menu that replaced two controls', () => {
  /** The menu is Radix: it opens on POINTERDOWN, never on click. */
  async function openMenu(): Promise<void> {
    fireEvent.pointerDown(screen.getByTestId('workspace-menu'))
    await screen.findByRole('menuitem', { name: 'Settings…' })
  }

  it('goes to the projects list from a project, and to the project from a chat', () => {
    render(<Workspace />)
    fireEvent.click(screen.getByRole('button', { name: 'Back to My Applications' }))
    expect(screen.getByTestId('where').textContent).toBe('/projects')

    cleanup()
    render(<Workspace entry="/chat/c1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Back to the application' }))
    expect(screen.getByTestId('where').textContent).toBe('/projects/pA')
  })

  it('★ completes at once with unsaved work in play, asking nothing first', async () => {
    // Shutdown now writes unsaved work back automatically under an ancestry guard before a
    // container is destroyed, so this exit has nothing left to ask about.
    render(<Workspace project={{ heading: PROJECT_HEADING, save: { dirty: true, saving: false, error: null } }} />)

    fireEvent.click(screen.getByRole('button', { name: 'Back to My Applications' }))

    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/projects'))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it.each([
    ['Settings…', 'settings'],
    ['Share…', 'share'],
  ] as const)('presses the published %s action', async (label, key) => {
    const press = vi.fn()
    render(
      <Workspace
        project={{ heading: PROJECT_HEADING, actions: { save: null, settings: null, share: null, [key]: press } }}
      />,
    )
    await openMenu()
    fireEvent.click(screen.getByRole('menuitem', { name: label }))
    expect(press).toHaveBeenCalledTimes(1)
  })

  it('★ holds Settings and Share and NOTHING ELSE', async () => {
    // Delete is deliberately not here — it is inside Settings, two steps from any surface. Send
    // for review is not here either: Settings > Production is its one owner, and a second submit
    // control is a second place to disagree about whether there is anything to submit.
    render(<Workspace project={{ heading: PROJECT_HEADING }} />)
    await openMenu()
    expect(screen.getAllByRole('menuitem').map((item) => item.textContent)).toEqual([
      'Settings…',
      'Share…',
    ])
  })

  it('★ the two controls it replaced are gone from the row itself', () => {
    // Rename retired into Settings > General; Share folded into the menu. Both left bare glyphs
    // in a row whose own note says nine occupants never fit 360px.
    render(<Workspace project={{ heading: PROJECT_HEADING }} />)
    // LIVENESS beside the absence: the row is fully drawn and names the project.
    expect(row().className).toMatch(/h-\[54px\]/)
    expect(title().textContent).toBe('Visitor Log — Airport Office')
    expect(screen.queryByRole('button', { name: /rename/i })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Share project' })).toBeNull()
  })

  it('★ no menu over a project that never loaded, and one the moment it does', async () => {
    /* THE MANGLED ADDRESS, in the only shape this row can see it. `projectId` is the ROUTE PARAM,
       so it is still there on a page whose project 422'd at the boundary — which is exactly what
       the retired rename pencil used to be gated on, and why a citizen who followed a truncated
       link was offered a control whose press was a measured no-op. The NAME is the field that
       comes from the project's own fetch, so it is the one that means loaded. */
    render(<Workspace project={{ heading: { ...PROJECT_HEADING, projectName: null } }} />)

    expect(screen.queryByTestId('workspace-menu')).toBeNull()
    // LIVENESS: the row is fully drawn around that absence — full height, a word in the name
    // slot — so this is a control that is gone rather than a tree that failed to render.
    expect(row().className).toMatch(/h-\[54px\]/)
    expect(title().textContent).toBe('Your application')

    cleanup()
    const settings = vi.fn()
    render(<Workspace project={{ heading: PROJECT_HEADING, actions: { save: null, settings, share: null } }} />)
    await openMenu()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Settings…' }))
    expect(settings).toHaveBeenCalledTimes(1)
  })

  it('★ the way out is NOT gated by the fact that silences the menu', () => {
    /* THE MUTANT THIS EXISTS FOR: gate the back control on `heading.projectName !== null` too and
       the dead address becomes a dead end. The row's own docblock is explicit that the control
       survives the load-error branch, and a branch with no way off it is worse than the raw
       validator sentence this unit came to remove. */
    render(<Workspace project={{ heading: { ...PROJECT_HEADING, projectName: null } }} />)

    fireEvent.click(screen.getByRole('button', { name: 'Back to My Applications' }))
    expect(screen.getByTestId('where').textContent).toBe('/projects')
  })

  it('★ the deliberate "Your project" fallback on a chat is untouched', () => {
    /* NOT PART OF THE DEFECT, and deliberately left as it is. A project deleted out from under
       an open chat leaves the breadcrumb with no name, and the row deliberately says "Your project"
       rather than leaving a gap that shifts the layout when a fetch lands. The menu gate is
       allowed to read the same `null`; it is not allowed to change what the slot says. */
    render(<Workspace entry="/chat/c1" chat={{ heading: { ...CHAT_HEADING, projectName: null } }} />)

    expect(row().textContent).toContain('Your application')
    expect(title().textContent).toBe('Add an out-time column')
    expect(screen.getByRole('button', { name: 'Back to the application' })).toBeTruthy()
    // The menu is about the APPLICATION; a chat address never had it, name or no name.
    expect(screen.queryByTestId('workspace-menu')).toBeNull()
  })
})

describe('the back control carries the projects list state back', () => {
  /* `page`, `pageSize` and `q` live in `/projects`'s own address, which is a one-way fix:
     reading it in is `ProjectsPage.test.tsx`'s job. This control is mounted on a DIFFERENT
     address — a project, a chat — and has to name a destination without ever having read that
     query string itself. Before this it hardcoded a bare `/projects`, so leaving a filtered,
     paged list and pressing Back landed on page one with the search cleared:
     `projectsListMemory.ts`'s own docblock calls this "addressable in one direction and silent
     in the other". This suite seeds that memory directly rather than mounting whatever writes
     it on `/projects`. */

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

    fireEvent.click(screen.getByRole('button', { name: 'Back to My Applications' }))

    expect(screen.getByTestId('where-full').textContent).toBe('/projects?page=2&pageSize=20&q=ramp')
  })

  it('falls back to the bare list when nothing has been remembered this session', () => {
    renderAt('/projects/pA')

    fireEvent.click(screen.getByRole('button', { name: 'Back to My Applications' }))

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
 * menu, or a scroller on the row — the lighter one shipped: the row scrolls, and all ten
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

  /** The project screen with an app framed, unsaved work over a saved version, and the chat
   *  hidden — the one state in which every one of the row's occupants is on screen at once. */
  const everything = () => {
    render(
      <Workspace
        project={{
          heading: PROJECT_HEADING,
          save: { dirty: true, hasSavedVersion: true },
          actions: { save: () => {}, discard: async () => {}, settings: () => {} },
        }}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Hide the chat' }))
  }

  const back = () => screen.getByRole('button', { name: 'Back to My Applications' })
  const menu = () => screen.getByTestId('workspace-menu')
  const devices = () => ['Desktop', 'Mobile'].map((name) => screen.getByRole('button', { name }))
  const reload = () => screen.getByRole('button', { name: 'Reload your app' })
  const newTab = () => screen.getByRole('link', { name: 'Open your app in a new tab' })
  const save = () => screen.getByTestId('save-project')
  const discard = () => screen.getByTestId('discard-changes')
  const railToggle = () => screen.getByTestId('toolbar-collapse')

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

  it('★ every occupant the row still owns is on it, and the two that left are named', () => {
    // THE ROW GOT SHORTER ON PURPOSE. Its own note says nine occupants never fit 360px, and this
    // is the first change to reduce the count: a third device width, a rename pencil and a share
    // glyph left; a token ring and one menu arrived. What must not happen is a control quietly
    // becoming unreachable, so both halves are asserted together — what is here, and what is not.
    everything()
    expect(back()).toBeTruthy()
    expect(title().textContent).toBe('Visitor Log — Airport Office')
    expect(screen.getByTestId('publish-chip-stub')).toBeTruthy()
    expect(devices()).toHaveLength(2)
    expect(reload()).toBeTruthy()
    expect(newTab()).toBeTruthy()
    expect(discard()).toBeTruthy()
    expect(save()).toBeTruthy()
    expect(railToggle()).toBeTruthy()
    expect(menu()).toBeTruthy()

    expect(screen.queryByRole('button', { name: 'Tablet' })).toBeNull()
    expect(screen.queryByRole('button', { name: /rename/i })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Share project' })).toBeNull()
  })

  it('★ every pressable control declares the 44px floor below the stacking threshold', () => {
    // STRUCTURAL: this asserts the class, not the rectangle. The rectangle is the browser suite's.
    // The floor is `min-h`/`min-w` rather than a bigger glyph — that class declaration is the half
    // jsdom CAN see; `hit areas grow by padding` below is its other half.
    everything()
    for (const control of [back(), menu(), ...devices(), reload(), newTab(), railToggle()]) {
      expect(cls(control)).toContain('narrow:min-h-[44px]')
      expect(cls(control)).toContain('narrow:min-w-[44px]')
      // A 44px box with a 15px glyph in its top-left corner is not a 44px target anyone can aim
      // at. Both axes have to centre.
      expect(cls(control)).toMatch(/\bitems-center\b/)
      expect(cls(control)).toMatch(/\bjustify-center\b/)
    }
    // Save and Discard are past 44px wide on their own words in every state, so only their HEIGHT
    // needs a floor.
    for (const worded of [save(), discard()]) {
      expect(cls(worded)).toContain('narrow:min-h-[44px]')
      expect(cls(worded)).not.toContain('narrow:min-w-[44px]')
    }
  })

  it('★ the hit areas grow by padding — every glyph keeps the size the canvas drew it at', () => {
    // The requirement is a bigger TARGET, not a bigger picture. Growing the icons would be the
    // easy way to 44×44 and would rebuild the row's visual weight at every width.
    everything()
    expect(glyph(back())).toBe('16×16')
    expect(glyph(menu())).toBe('16×16')
    for (const device of devices()) expect(glyph(device)).toBe('14×14')
    expect(glyph(reload())).toBe('15×15')
    expect(glyph(newTab())).toBe('15×15')
    expect(glyph(railToggle())).toBe('15×15')
    expect(glyph(save())).toBe('14×14')
    expect(glyph(discard())).toBe('14×14')
  })

  it('★ above the stacking threshold every control keeps exactly the geometry it shipped with', () => {
    // The whole point of gating the floors behind a variant. Strip the `narrow:` tokens — which is
    // what a 1200px viewport does — and what is left must be the pre-existing class list, with no
    // 44px floor leaking up into the desktop row.
    everything()
    const desktop = [back(), menu(), ...devices(), reload(), newTab(), railToggle(), discard(), save()].map(
      aboveThreshold,
    )
    for (const list of desktop) expect(list).not.toMatch(/min-[hw]-\[44px\]/)

    // …and the sizes those controls are actually drawn at up there, named so a silent change to
    // any of them has to be deliberate: 20×20, 28×32, 28×30.
    expect(aboveThreshold(back())).toContain('p-0.5')
    for (const device of devices()) expect(aboveThreshold(device)).toMatch(/\bh-7\b.*\bw-8\b/)
    for (const boxed of [railToggle(), menu()]) expect(aboveThreshold(boxed)).toMatch(/\bh-7\b.*\bw-\[30px\]/)
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
          actions: { save: () => {}, settings: null, share: null },
        }}
      />,
    )

    expect(title().textContent).toBe(long)
    expect(cls(title())).toMatch(/\btruncate\b/)
    expect(cls(row())).toMatch(/h-\[54px\]/)
    expect(screen.getByRole('button', { name: 'Back to the application' })).toBeTruthy()
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

/**
 * ★ THE TOKEN COUNTER FOLLOWS THE CITIZEN INTO THE WORKSPACE.
 *
 * The client requires a counter that never stops being visible, and it lives in the navigation
 * panel — which is HIDDEN inside an application. So the one screen where tokens are actually
 * spent was the one screen with no reading on it at all. A compact ring in the right-hand
 * cluster answers that, off the same hook the panel reads.
 */
describe('the token ring in the toolbar', () => {
  it('★ shows the figures, not just an arc', () => {
    // The ring is how it reads at a glance; the numbers are the requirement. A ring alone would
    // satisfy the shape of the ask and none of it.
    h.usage.mockReturnValue(USAGE)
    render(<Workspace />)
    const meter = screen.getByTestId('usage-meter')
    expect(meter.textContent).toMatch(/42/)
    expect(meter.textContent).toMatch(/100/)
  })

  it('★ draws the COMPACT form — the row is already full', () => {
    // Mutation receipt: drop `compact` and this goes red. The panel's ring carries a border, a
    // card background and 38px of arc; dropped into a 54px row it is a box inside a box.
    h.usage.mockReturnValue(USAGE)
    render(<Workspace />)
    const meter = screen.getByTestId('usage-meter')
    expect(meter.className).not.toMatch(/\bborder\b/)
    expect(meter.querySelector('svg')?.getAttribute('width')).toBe('30')
  })

  it('★ draws NOTHING when there is no reading, rather than a confident zero', () => {
    // `null` is signed out, or a read that failed. A 0-of-0 ring would say the budget is spent.
    h.usage.mockReturnValue(null)
    render(<Workspace />)
    // LIVENESS beside the absence: the row is fully drawn around the gap.
    expect(title().textContent).toBe('Visitor Log — Airport Office')
    expect(screen.queryByTestId('usage-meter')).toBeNull()
  })

  it('is on the row itself, where a collapse cannot take it away', () => {
    h.usage.mockReturnValue(USAGE)
    render(<Workspace />)
    fireEvent.click(screen.getByTestId('toolbar-collapse'))
    expect(row().contains(screen.getByTestId('usage-meter'))).toBe(true)
  })

  it('★ stands IN for the panel\'s counter rather than joining it', () => {
    // The requirement is a reading that never stops being visible — ONE reading. Summoning the
    // navigation puts the panel's own counter on screen, and without this both drew the same two
    // numbers in one view: not a second opinion, a second chance to disagree.
    h.usage.mockReturnValue(USAGE)
    render(<Workspace nav />)
    expect(row().querySelector('[data-testid="usage-meter"]')).not.toBeNull()

    fireEvent.click(screen.getByTestId('nav-menu-button'))
    // The reading did not disappear, it MOVED — the panel is what carries it now.
    expect(screen.getByTestId('nav-panel')).toBeTruthy()
    expect(row().querySelector('[data-testid="usage-meter"]')).toBeNull()
    expect(screen.getAllByTestId('usage-meter')).toHaveLength(1)
  })
})
