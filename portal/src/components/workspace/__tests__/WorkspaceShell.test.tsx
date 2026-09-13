/**
 * The workspace shell and the channel it holds.
 *
 * The shell's ROUTING claim — that the same element survives a move between the two addresses —
 * is `src/App.test.jsx`'s, because it is a claim about the route table and a hand-built table
 * here would prove the component instead of the wiring. This file has everything the shell does
 * once mounted: the height model, the grid, the reclaim slot, and above all the channel's rules.
 *
 * Two rules give the channel teeth: a publish must not wake a subscriber that did not care, and
 * whether a payload survives its publisher's unmount is decided per payload — the table in
 * `workspaceChannel.ts` names each one.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { useState, type ReactNode } from 'react'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import WorkspaceShell from '../WorkspaceShell'
import { useWorkspaceExit } from '../UnsavedWorkGuard'
import {
  useAppPaneVisible,
  usePublishAddress,
  usePublishPaneView,
  usePublishReclaim,
  usePublishSaveState,
  useWorkspaceAddress,
  useWorkspaceChannel,
  useWorkspacePane,
  useWorkspacePaneVisible,
  useWorkspaceProject,
  useWorkspaceSaveState,
  type PaneView,
  type ReclaimRequest,
} from '../workspaceChannel'
import type { ReclaimBlocked } from '../../../utils/buildSessionApi'

// THE NAVBAR STUB CONSULTS THE EXIT HOOK, exactly as the real one does — the seam this file is
// answerable for is whether the SHELL provides a guard to the chrome sitting ABOVE its Outlet.
// A `<div/>` stub could not see it, and the real navbar would drag a profile fetch, a usage poll,
// and a feedback modal into every scenario here. That the real navbar routes its links through
// the hook is `Navbar.test.jsx`'s to prove; this proves there is something for it to route
// through.
vi.mock('../../layout/Navbar', () => ({
  // NAMED: `default: () => …` is an anonymous arrow, and the hooks lint rule reads a component's
  // identity off its name — a hook inside one it can't recognise is an error, rightly so.
  default: function StubNavbar() {
    const exit = useWorkspaceExit()
    return (
      <div data-testid="navbar">
        <button type="button" onClick={() => exit(() => {})}>leave to projects</button>
      </div>
    )
  },
}))

/**
 * Mount `child` as the shell's outlet content, the way a route element is. Every probe below
 * reads the channel from INSIDE the outlet — the same channel through the same context, but not
 * the pane host's tree position. That property is asserted where it lives: `App.test.jsx` for
 * the shell, `AppPaneHost.test.tsx` for the frame.
 */
function renderShell(child: ReactNode) {
  return render(
    <MemoryRouter initialEntries={['/projects/p1']}>
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route path="/projects/:projectId" element={<>{child}</>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

/**
 * EVERY SURFACE THE SHELL FRAMES, by path — the shell's two routes plus the components they
 * render inside its outlet column. A new in-shell surface belongs on this list, kept current
 * rather than left naming files that no longer exist (a rotted inventory reads as a passing one).
 * It is a SOURCE scan, not a render assertion, so it reaches branches no test mounts — a surface
 * left off it is not covered by "the tests pass".
 */
const IN_SHELL_SURFACES = [
  'pages/ChatRoute.tsx',
  'components/chat/ConversationSurface.tsx',
  'pages/ProjectPage.tsx',
  'components/workspace/ConversationSlot.tsx',
  'components/workspace/AppPaneHost.tsx',
  'components/workspace/AppPane.tsx',
  'components/workspace/WorkspaceToolbar.tsx',
  'components/workspace/WorkspaceRail.tsx',
  'components/workspace/RailComposer.tsx',
  'components/workspace/RailResizeHandle.tsx',
  'components/workspace/PlanChatWorkspaceLine.tsx',
  'components/workspace/StartAppControl.tsx',
] as const

const grid = () => screen.getByTestId('workspace-grid')
const shellRoot = () => grid().parentElement as HTMLElement

/** Every pane prop at its quiet default. Individual tests set only what they are about. */
const EMPTY_PANE: PaneView = {
  iterating: false, reconnecting: false,
  previewState: null, turnRunning: false,
  compileState: null, workspaceLost: false,
}

afterEach(() => cleanup())

describe('WorkspaceShell — one height model, one frame, one grid', () => {
  it('is a full-height frame that does not scroll, so no surface below it has a viewport opinion', () => {
    renderShell(<div data-testid="surface" />)

    // The chat model, taken as the shell's own: full height, navbar, no document scroll. What the
    // project surface loses by this — its document scroll — it declares for itself instead.
    expect(shellRoot().className).toMatch(/h-screen/)
    expect(shellRoot().className).toMatch(/overflow-hidden/)
    expect(screen.getAllByTestId('navbar')).toHaveLength(1)
  })

  it('renders the surface inside a column that may not overflow the frame', () => {
    renderShell(<div data-testid="surface" />)
    const outletColumn = screen.getByTestId('surface').parentElement as HTMLElement

    expect(outletColumn.className).toMatch(/overflow-hidden/)
    expect(outletColumn.className).toMatch(/min-h-0/)
  })

  it('nothing in the rendered workspace transcribes the viewport height', () => {
    // No surface may hardcode the viewport height in a `calc()` — the shell already owns it.
    const { container } = renderShell(<div data-testid="surface" />)
    expect(container.innerHTML).not.toMatch(/100vh/)
  })

  it('and no surface the shell frames asserts a viewport height in its source', () => {
    // READ AS SOURCE, not as a render: the scenario above only sees what the stub surface it
    // mounts happens to emit, so it was blind to `ChatRoute`'s loading arm — a `min-h-screen` box
    // in a column already 100vh minus the navbar, which overflowed and pushed its spinner below
    // centre on every cold chat open. Surfaces OUTSIDE the shell (`Dashboard`, `LoginPage`,
    // `AdminPage`, `HelpPage`) are correctly not on the list — they are their own document.
    const offending = IN_SHELL_SURFACES.flatMap((file) =>
      readFileSync(`src/${file}`, 'utf8')
        .split('\n')
        .flatMap((line, i) => {
          // Prose about the height model is the whole point of several of these files, so only
          // code lines count — a comment saying "not `min-h-screen`" must not fail its own guard.
          const code = line.trimStart()
          if (code.startsWith('//') || code.startsWith('*') || code.startsWith('/*')) return []
          return /\b(?:min-h-screen|h-screen)\b/.test(line) ? [`${file}:${i + 1}`] : []
        }),
    )
    expect(offending).toEqual([])
  })
})

describe('WorkspaceShell — the grid is the shell\'s own', () => {
  /** Flip `stacked` from inside the outlet, the way a responsive threshold does. */
  function StackToggle() {
    const channel = useWorkspaceChannel()
    const [stacked, setStacked] = useState(false)
    return (
      <button
        type="button"
        onClick={() => {
          setStacked(!stacked)
          channel?.rail.set({ mode: null, stacked: !stacked, collapsed: false })
        }}
      >
        flip
      </button>
    )
  }

  it('follows the rail slot\'s direction, and the SAME grid element survives the flip', () => {
    // What must be true HERE is that flipping `stacked` changes a class on an element that is NOT
    // replaced — the pane host hangs off it.
    renderShell(<StackToggle />)
    const before = grid()
    expect(before.className).toMatch(/flex-row/)

    fireEvent.click(screen.getByRole('button', { name: 'flip' }))

    expect(grid()).toBe(before)
    expect(grid().className).toMatch(/flex-col/)
    expect(grid().className).not.toMatch(/flex-row/)
  })
})

describe('WorkspaceShell — the reclaim dialog is mounted here, its handlers stay with the publisher', () => {
  const blocked: ReclaimBlocked = {
    projectId: 'p-other', projectName: 'Other Project', dirty: true, building: false, agentWorking: false,
    isSharedView: false,
  }

  function SurfaceWithRefusal({ onResolve }: { onResolve: (save: boolean) => Promise<void> }) {
    const [request, setRequest] = useState<ReclaimRequest | null>(null)
    usePublishReclaim(request)
    return (
      <button
        type="button"
        onClick={() => setRequest({ blocked, startingProjectName: 'Visitor Log', step: null, resolve: onResolve, cancel: () => setRequest(null) })}
      >
        refuse
      </button>
    )
  }

  it('opens from the channel and routes its answer back to the surface that was refused', async () => {
    // Only the OPEN STATE travels — stopping, saving, and retrying stay with the surface that
    // made the call, so the shell is never a second authority on a refusal that already has one.
    const onResolve = vi.fn().mockResolvedValue(undefined)
    renderShell(<SurfaceWithRefusal onResolve={onResolve} />)

    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'refuse' }))

    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toMatch(/Other Project/)
    // …and the STARTING project too, so the dialog can name whose changes are lost.
    expect(dialog.textContent).toMatch(/Visitor Log/)

    fireEvent.click(screen.getByRole('button', { name: /stop “Other Project” without saving/i }))
    expect(onResolve).toHaveBeenCalledWith(false)
  })
})

describe('WorkspaceShell — the unsaved-work warning, hoisted here (the leaving-the-page half)', () => {
  /** Ask the browser to leave, and report whether anything objected. */
  const tryToLeave = (): boolean => {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event.defaultPrevented
  }

  /** THE RECOVERY INSTANT DEFAULTS TO `null` — "the platform holds no copy of this" — so every
   *  scenario written before it existed still describes work that leaving would genuinely cost
   *  somebody, and a scenario that wants the recoverable case has to ask for it by name. */
  function Surface({ dirty, recoveryAt = null }: { dirty: boolean | null; recoveryAt?: string | null }) {
    useWorkspaceProject('p1')
    usePublishSaveState({ dirty, recoveryAt })
    return <div data-testid="surface" />
  }

  /** A conversation that publishes, then a project screen that does not — the hoist's whole point. */
  function Workspace({
    conversationMounted,
    dirty,
    recoveryAt = null,
  }: {
    conversationMounted: boolean
    dirty: boolean | null
    recoveryAt?: string | null
  }) {
    return (
      <MemoryRouter initialEntries={['/projects/p1']}>
        <Routes>
          <Route element={<WorkspaceShell />}>
            <Route
              path="/projects/:projectId"
              element={
                conversationMounted ? (
                  <Surface dirty={dirty} recoveryAt={recoveryAt} />
                ) : (
                  <div data-testid="project-only" />
                )
              }
            />
          </Route>
        </Routes>
      </MemoryRouter>
    )
  }

  it('warns on a definite `true`, and GOES ON warning after the conversation unmounts', () => {
    // THE ONLY USER-VISIBLE CONSEQUENCE OF THE HOIST: bound to the builder page instead, the
    // effect would disarm the moment the citizen navigates away — exactly when they are most
    // likely to close the tab.
    const view = render(<Workspace conversationMounted dirty />)
    expect(tryToLeave()).toBe(true)

    view.rerender(<Workspace conversationMounted={false} dirty />)

    expect(screen.getByTestId('project-only')).toBeTruthy()
    expect(tryToLeave()).toBe(true)
  })

  it('says nothing when the state is definitely clean', () => {
    render(<Workspace conversationMounted dirty={false} />)
    expect(tryToLeave()).toBe(false)
  })

  it('says nothing when the state is UNKNOWN, and claims nothing either way', () => {
    // `null` is "could not check", never "clean" — the silence is deliberate, not an oversight:
    // the browser's fixed prompt can't carry a "we could not check" sentence (the in-app dialog
    // does), and arming it anyway is how people learn to dismiss it. What must NOT happen is the
    // other failure: claiming there is nothing unsaved.
    const { container } = render(<Workspace conversationMounted dirty={null} />)

    expect(tryToLeave()).toBe(false)
    expect(container.textContent).not.toMatch(/no unsaved|nothing unsaved|all saved|up to date/i)
  })

  it('says nothing when NOBODY has published, and asks nothing to find out', () => {
    // "Nobody has reported" is the same `null` as "the check failed", and nothing here calls the
    // save-state endpoint — that check costs two `git` executions inside the container, which a
    // screen with no conversation has nothing to compare.
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    try {
      render(<Workspace conversationMounted={false} dirty={null} />)
      expect(tryToLeave()).toBe(false)
      expect(fetchSpy).not.toHaveBeenCalled()
    } finally {
      fetchSpy.mockRestore()
    }
  })

  it('★ says nothing when the platform is holding a copy it can put back', () => {
    // THE REPORTED BUG, at the tab-closing exit. A freshly built app that nobody has saved is
    // `dirty: true` — there is no VERSION, because Save is the citizen's own act — and the
    // browser's fixed-text prompt fired on somebody closing a tab over work the platform can put
    // back at any time. `newest_restore_source` hands that copy to every automatic restore.
    //
    // MUTATION RECEIPT: drop `|| canBePutBack(recoveryAt)` from the effect's guard and this goes
    // red while every other case in this describe stays green.
    const { container } = render(<Workspace conversationMounted dirty recoveryAt="2026-09-10T10:38:43Z" />)

    // Alive and mounted — so the silence below is the rule's, not a component that failed to render.
    expect(screen.getByTestId('surface')).toBeTruthy()
    expect(tryToLeave()).toBe(false)
    // …and nothing on screen claims the work was SAVED, which it was not.
    expect(container.textContent).not.toMatch(/all saved|up to date/i)
  })

  it('★ goes on warning when the same `true` has NO recovery copy', () => {
    // The half that keeps the carve-out narrow: work the platform is holding nothing for is still
    // work closing the tab would cost somebody.
    render(<Workspace conversationMounted dirty recoveryAt={null} />)

    expect(tryToLeave()).toBe(true)
  })

  it('★ FOLLOWS the instant when a later reading takes it away', () => {
    // A poll answers again, and only the recovery instant is different — which is a reading the
    // channel's cell has to let through. Its value comparator is what decides that, and one blind
    // to this field would hold the first answer forever: the guard would go on letting somebody
    // out over work the platform has since stopped holding a copy of, for the life of the tab.
    //
    // MUTATION RECEIPT: drop the `recoveryAt` conjunct from `sameReading` in `workspaceChannel.ts`
    // and this goes red — the second reading is discarded and the prompt never re-arms.
    const view = render(<Workspace conversationMounted dirty recoveryAt="2026-09-10T10:38:43Z" />)
    expect(tryToLeave()).toBe(false)

    view.rerender(<Workspace conversationMounted dirty recoveryAt={null} />)

    expect(tryToLeave()).toBe(true)
  })

  it('disarms when the state goes from dirty back to clean', () => {
    // The listener has to come off, not merely stop mattering: a stale one left bound would warn
    // about a container that has since been saved, for the life of the tab.
    const view = render(<Workspace conversationMounted dirty />)
    expect(tryToLeave()).toBe(true)

    view.rerender(<Workspace conversationMounted dirty={false} />)

    expect(tryToLeave()).toBe(false)
  })
})

describe('the workspace channel — a publish wakes only what it concerns', () => {
  const renders = { pane: 0, save: 0 }

  function PaneSubscriber() {
    renders.pane += 1
    const address = useWorkspaceAddress()
    const pane = useWorkspacePane()
    return <div data-testid="pane-sub">{`${address.url ?? 'none'}|${pane ? 'view' : 'no-view'}`}</div>
  }

  function SaveSubscriber() {
    renders.save += 1
    return <div data-testid="save-sub">{String(useWorkspaceSaveState().dirty)}</div>
  }

  /** Publishes from its OWN state, so the change reaches the subscribers only through the cells. */
  function SavePublisher() {
    const [dirty, setDirty] = useState<boolean | null>(null)
    usePublishSaveState({ dirty, recoveryAt: null })
    return <button type="button" onClick={() => setDirty(true)}>mark dirty</button>
  }

  it('a save-state publish does not re-render the pane subscriber', () => {
    // The rule that keeps the pane host still: one republished context value would re-render it
    // on every keystroke.
    renders.pane = 0
    renders.save = 0
    renderShell(<><PaneSubscriber /><SaveSubscriber /><SavePublisher /></>)
    const paneRendersBefore = renders.pane
    const saveRendersBefore = renders.save

    fireEvent.click(screen.getByRole('button', { name: 'mark dirty' }))

    expect(screen.getByTestId('save-sub').textContent).toBe('true')
    expect(renders.save).toBeGreaterThan(saveRendersBefore)
    expect(renders.pane).toBe(paneRendersBefore)
  })
})

describe('the workspace channel — what survives its publisher\'s unmount, and what must not', () => {
  function Surface() {
    useWorkspaceProject('p1')
    usePublishAddress({ url: 'https://app.example/', status: 'ready', serving: true }, 'p1')
    usePublishPaneView(EMPTY_PANE)
    useAppPaneVisible(true)
    usePublishSaveState({ dirty: true, recoveryAt: null })
    return <div data-testid="surface" />
  }

  function Probe() {
    const address = useWorkspaceAddress()
    const pane = useWorkspacePane()
    const visible = useWorkspacePaneVisible()
    const { dirty } = useWorkspaceSaveState()
    return (
      <div data-testid="probe">
        {`${address.url ?? 'none'}|${pane ? 'view' : 'no-view'}|${visible ? 'shown' : 'hidden'}|${String(dirty)}`}
      </div>
    )
  }

  const probe = () => screen.getByTestId('probe').textContent

  /** The probe outlives the surface, the way the shell outlives a conversation. */
  function Workspace({ conversationMounted }: { conversationMounted: boolean }) {
    return (
      <MemoryRouter initialEntries={['/projects/p1']}>
        <Routes>
          <Route element={<WorkspaceShell />}>
            <Route
              path="/projects/:projectId"
              element={
                <>
                  <Probe />
                  {conversationMounted ? <Surface /> : <div data-testid="other-surface" />}
                </>
              }
            />
          </Route>
        </Routes>
      </MemoryRouter>
    )
  }

  it('keeps the ADDRESS and the SAVE STATE, and drops the pane view and the visibility declaration', () => {
    // Each of the four has its own reason and uniformity breaks one of them — the table in
    // `workspaceChannel.ts`. The save state is kept because the unsaved work is in the CONTAINER,
    // not the component.
    const view = render(<Workspace conversationMounted />)
    expect(probe()).toBe('https://app.example/|view|shown|true')

    view.rerender(<Workspace conversationMounted={false} />)

    expect(probe()).toBe('https://app.example/|no-view|hidden|true')
  })

  it('a DIFFERENT project invalidates a held address; an UNRESOLVED one does not', () => {
    // The asymmetry is the whole point: a `null` project claims nothing, and only a DIFFERENT one
    // invalidates a held address. `AppPaneHost` owns the rule.
    function Declarer({ project }: { project: string | null }) {
      usePublishAddress({ url: 'https://app.example/', status: 'ready', serving: true }, 'p1')
      useWorkspaceProject(project)
      return null
    }
    function At({ project }: { project: string | null }) {
      return (
        <MemoryRouter initialEntries={['/projects/p1']}>
          <Routes>
            <Route element={<WorkspaceShell />}>
              <Route path="/projects/:projectId" element={<><Probe /><Declarer project={project} /></>} />
            </Route>
          </Routes>
        </MemoryRouter>
      )
    }

    const view = render(<At project="p1" />)
    expect(probe()).toContain('https://app.example/')

    view.rerender(<At project={null} />) // still resolving — claims nothing
    expect(probe()).toContain('https://app.example/')

    view.rerender(<At project="p2" />) // a different project is a different app
    expect(probe()).toContain('none')
  })
})

/**
 * THE IN-PLACE GUARD, MOUNTED AT SHELL LEVEL. Two guards divide the work: `beforeunload` covers
 * leaving the TAB and arms only on a definite `true`; this one covers leaving the WORKSPACE
 * without an unload and warns on `null` too, since an in-app dialog can carry a reason the
 * browser's fixed prompt cannot. Mounted here rather than in the Outlet child because the exits
 * it guards — the navbar's links — sit above the Outlet. What they now AGREE on is the
 * recoverable case: neither stops anybody over a `true` the platform holds a recovery copy of.
 */
describe('WorkspaceShell — the in-place unsaved-work guard', () => {
  function SurfaceWithSaveState({
    dirty,
    running,
    recoveryAt = null,
  }: {
    dirty: boolean | null
    running: boolean
    recoveryAt?: string | null
  }) {
    usePublishSaveState({ dirty, recoveryAt })
    useWorkspaceChannel()?.workspace.set({
      state: running
        ? { name: 'running', headline: 'Your app is running.', detail: null, action: null }
        : { name: 'not-running', headline: 'Your app is saved.', detail: null, action: null },
      projectId: 'p1',
      onStarted: () => {},
      onStartPending: () => {},
      onStartOutcome: () => {},
      onRefresh: () => {},
      onReclaimRefusal: () => {},
    })
    return <div data-testid="surface" />
  }

  it('★ intercepts a navbar link when the workspace holds unsaved work', async () => {
    renderShell(<SurfaceWithSaveState dirty running />)

    fireEvent.click(await screen.findByRole('button', { name: /leave to projects/i }))

    expect(screen.getByRole('dialog').textContent).toMatch(/changes that are not saved yet/i)
  })

  it('lets the same link through when the workspace is clean', async () => {
    renderShell(<SurfaceWithSaveState dirty={false} running />)

    fireEvent.click(await screen.findByRole('button', { name: /leave to projects/i }))

    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('★ warns about nothing on a STOPPED project, where the check was never asked', async () => {
    // The fourth case. A stopped project's save state is `null` because `fetchSaveState` may only
    // be called on a live workspace — not because a check failed.
    renderShell(<SurfaceWithSaveState dirty={null} running={false} />)

    fireEvent.click(await screen.findByRole('button', { name: /leave to projects/i }))

    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('★ lets a navbar link through when the work is recoverable — the SEAM, end to end', async () => {
    // THE WIRING THIS FIX IS: the instant is published on the channel beside the tri-state, read
    // by the shell, and handed to the guard as one reading. Nothing here reaches into the guard —
    // it is the same publish a conversation surface makes, and the same navbar link a citizen
    // presses — so a break in ANY hop of that chain shows up right here.
    //
    // MUTATION RECEIPT: delete the `recoveryAt` line from the shell's `useUnsavedWorkGuard` call
    // and this goes red on its own (the guard then defaults to `null` and stops them again).
    renderShell(<SurfaceWithSaveState dirty running recoveryAt="2026-09-10T10:38:43Z" />)

    fireEvent.click(await screen.findByRole('button', { name: /leave to projects/i }))

    expect(screen.queryByRole('dialog')).toBeNull()
    // …and the shell is alive and the link is still there, so the absent dialog is the rule's
    // doing rather than a render that fell over.
    expect(screen.getByTestId('surface')).toBeTruthy()
    expect(screen.getByRole('button', { name: /leave to projects/i })).toBeTruthy()
  })

  it('★ still intercepts the same link when the work is NOT recoverable', async () => {
    // Same publish, same link, one field different — the assertion that stops the fix from
    // becoming "never warn about anything".
    renderShell(<SurfaceWithSaveState dirty running recoveryAt={null} />)

    fireEvent.click(await screen.findByRole('button', { name: /leave to projects/i }))

    expect(screen.getByRole('dialog').textContent).toMatch(/changes that are not saved yet/i)
  })

  it('★ there is exactly ONE guard, not two', async () => {
    // "Never two guards" is held by construction here rather than by remembering to delete one:
    // the hoisted unload handler is extended, never duplicated — no second `beforeunload`
    // listener and no second hook.
    const added: string[] = []
    const original = window.addEventListener.bind(window)
    const spy = vi.spyOn(window, 'addEventListener').mockImplementation((type, ...rest) => {
      added.push(String(type))
      return original(type, ...(rest as [EventListenerOrEventListenerObject]))
    })

    renderShell(<SurfaceWithSaveState dirty running />)
    await screen.findByTestId('surface')

    expect(added.filter((t) => t === 'beforeunload')).toHaveLength(1)
    spy.mockRestore()
  })
})
