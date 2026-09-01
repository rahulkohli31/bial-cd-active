/**
 * The workspace shell and the channel it holds (Plan A, U3).
 *
 * The shell's ROUTING claim — that the same element survives a move between the two addresses —
 * is asserted in `src/App.test.jsx` against the real `<App/>`, because that claim is about the
 * route table and a hand-built one here would prove the component instead of the wiring. What is
 * left for this file is everything the shell does once mounted: the single height model, the grid
 * it owns, the reclaim slot, and above all the channel's contract.
 *
 * THE CHANNEL'S CONTRACT IS THE PART WITH TEETH. Two rules make an upward channel between an
 * outlet child and its shell-mounted sibling safe rather than merely convenient, and both are
 * invisible until something breaks far away:
 *
 *  1. A publish must not wake a subscriber that did not care. The alternative — one context value
 *     republished on every change — re-renders the pane host on every character typed.
 *  2. Whether a payload survives its publisher's unmount is a PER-PAYLOAD decision. Uniform in
 *     either direction breaks something real: clear the address and leaving a build chat for the
 *     project screen destroys the running app (R8); keep the reclaim dialog and its buttons
 *     outlive the handlers they call.
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

// THE NAVBAR STUB CONSULTS THE EXIT HOOK, exactly as the real one does. That is the seam this
// file is answerable for: does the SHELL provide a guard to the chrome sitting ABOVE its Outlet?
// A `<div/>` stub could not see it, and the real navbar would drag a profile fetch, a usage poll
// and a feedback modal into every scenario here. That the real navbar routes its links through the
// hook is `Navbar.test.jsx`'s to prove; this proves there is something for it to route through.
vi.mock('../../layout/Navbar', () => ({
  // NAMED, because `default: () => …` is an anonymous arrow and the hooks lint rule reads a
  // component's identity off its name — a hook inside one it cannot recognise is an error, and it
  // is right to be: React itself keys a component's hook state on the same thing.
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
 * Mount `child` as the shell's outlet content, the way a route element is.
 *
 * Every probe below reads the channel from INSIDE the outlet. That is not the pane host's
 * position — the host is the Outlet's sibling — but it is the same channel through the same
 * context, and the tree-position property (that a route change cannot reach the host) is asserted
 * where it actually lives: `App.test.jsx` for the shell, `AppPaneHost.test.tsx` for the frame.
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
 * EVERY SURFACE THE SHELL FRAMES, by path. The shell's two routes (`App.tsx`) plus the components
 * they render inside its outlet column. A new in-shell surface belongs on this list.
 */
// The two chat PAGES are gone (Plan D U17) and one surface replaces them, so the list follows the
// tree rather than being left naming files that no longer exist — a guard whose inventory has
// rotted reads the same as a guard that passes.
const IN_SHELL_SURFACES = [
  'pages/ChatRoute.tsx',
  'components/chat/ConversationSurface.tsx',
  'pages/ProjectPage.tsx',
  'components/workspace/ConversationSlot.tsx',
  'components/workspace/AppPaneHost.tsx',
] as const

const grid = () => screen.getByTestId('workspace-grid')
const shellRoot = () => grid().parentElement as HTMLElement

/** Every pane prop at its quiet default. Individual tests set only what they are about. */
const EMPTY_PANE: PaneView = {
  toolbarLeading: null, toolbarTrailing: null,
  iterating: false, reconnecting: false,
  relaunching: false, relaunchError: null, lastBuildFailed: false,
  restoredFromFailedBuild: false, completedLive: false, hasSavedBuild: null,
  previewState: null, occupyingProjectName: null, turnRunning: false,
  compileState: null, workspaceLost: false,
  saveDirty: null, saving: false, saveError: null,
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
    // The codebase's only `calc(100vh - 56px)` was the navbar's `h-14` copied into a chat page,
    // one Tailwind edit away from a scrollbar nobody could explain. It must not come back as an
    // inline style on any surface the shell frames.
    const { container } = renderShell(<div data-testid="surface" />)
    expect(container.innerHTML).not.toMatch(/100vh/)
  })

  it('and no surface the shell frames asserts a viewport height in its source', () => {
    // READ AS SOURCE, not as a render, and that is the point. The scenario above can only see what
    // the stub surface it mounts happens to emit, so it was blind to `ChatRoute`'s loading arm —
    // a `min-h-screen` box inside a column that is 100vh MINUS the navbar and `overflow-hidden`,
    // which cannot shrink to fit, so it overflowed and pushed its spinner below the visible centre
    // on every cold chat open. One class on a branch no shell test renders.
    //
    // Every surface the shell frames is checked instead, by path, so a new one has to be added to
    // the list and a returning `h-screen` is caught wherever it lands. Surfaces OUTSIDE the shell
    // (`Dashboard`, `LoginPage`, `AdminPage`, `HelpPage`, the pre-shell `AuthLoading`) keep their
    // own viewport heights and are correctly not listed — they are their own document.
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
  /** Flip `stacked` from inside the outlet, the way Plan F's threshold will. */
  function StackToggle() {
    const channel = useWorkspaceChannel()
    const [stacked, setStacked] = useState(false)
    return (
      <button
        type="button"
        onClick={() => {
          setStacked(!stacked)
          channel?.rail.set({ mode: null, state: {}, stacked: !stacked, collapsed: false })
        }}
      >
        flip
      </button>
    )
  }

  it('follows the rail slot\'s direction, and the SAME grid element survives the flip', () => {
    // AE37's precondition, at the container this plan owns. Plan F supplies the threshold that
    // flips `stacked`; what must be true HERE is that flipping it changes a class on an element
    // that is not replaced — because the pane host hangs off that element as a sibling, and an
    // element swapped on a layout change takes the running app with it.
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
    projectId: 'p-other', projectName: 'Other Project', dirty: true, building: false,
  }

  function SurfaceWithRefusal({ onResolve }: { onResolve: (save: boolean) => Promise<void> }) {
    const [request, setRequest] = useState<ReclaimRequest | null>(null)
    usePublishReclaim(request)
    return (
      <button
        type="button"
        onClick={() => setRequest({ blocked, startingProjectName: 'Visitor Log', resolve: onResolve, cancel: () => setRequest(null) })}
      >
        refuse
      </button>
    )
  }

  it('opens from the channel and routes its answer back to the surface that was refused', async () => {
    // Only the OPEN STATE travels. Stopping the other project's build, saving it, releasing it and
    // retrying the refused call are all things the surface that made that call knows how to do,
    // and a shell that re-derived them would be a second authority on a refusal that has one.
    const onResolve = vi.fn().mockResolvedValue(undefined)
    renderShell(<SurfaceWithRefusal onResolve={onResolve} />)

    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'refuse' }))

    const dialog = await screen.findByRole('dialog')
    // The refusal names the project standing in the way — the whole reason the state travels
    // rather than the shell inventing its own copy.
    expect(dialog.textContent).toMatch(/Other Project/)
    // …and the STARTING project too, which travels for issue #161's framing half. The button copy
    // moved with it (`Switch without saving` → a sentence that names whose changes are lost), so
    // this assertion follows the copy rather than pinning the retired wording.
    expect(dialog.textContent).toMatch(/Visitor Log/)

    fireEvent.click(screen.getByRole('button', { name: /stop “Other Project” without saving/i }))
    expect(onResolve).toHaveBeenCalledWith(false)
  })
})

describe('WorkspaceShell — the unsaved-work warning, hoisted here (U7, AE33\'s leaving-the-page half)', () => {
  /** Ask the browser to leave, and report whether anything objected. */
  const tryToLeave = (): boolean => {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event.defaultPrevented
  }

  function Surface({ dirty }: { dirty: boolean | null }) {
    useWorkspaceProject('p1')
    usePublishSaveState(dirty)
    return <div data-testid="surface" />
  }

  /** A conversation that publishes, then a project screen that does not — the hoist's whole point. */
  function Workspace({ conversationMounted, dirty }: { conversationMounted: boolean; dirty: boolean | null }) {
    return (
      <MemoryRouter initialEntries={['/projects/p1']}>
        <Routes>
          <Route element={<WorkspaceShell />}>
            <Route
              path="/projects/:projectId"
              element={conversationMounted ? <Surface dirty={dirty} /> : <div data-testid="project-only" />}
            />
          </Route>
        </Routes>
      </MemoryRouter>
    )
  }

  it('warns on a definite `true`, and GOES ON warning after the conversation unmounts', () => {
    // THE COVERAGE THAT DID NOT EXIST BEFORE THE HOIST, and the only user-visible consequence of
    // this unit. The effect used to live on the builder page, so it disarmed the moment the
    // citizen navigated from the chat to the project screen — which is precisely when they have
    // stopped looking at the conversation that knows about the unsaved work, and exactly the
    // moment they are most likely to close the tab.
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
    // `null` is "could not check", never "clean", and the silence is deliberate rather than an
    // oversight: the browser renders fixed text the page cannot supply a "we could not check"
    // sentence to, so a prompt armed on an unknown is a prompt with nothing answerable behind it —
    // which is how people learn to dismiss them. Plan F's in-app dialog is where that sentence
    // lands. What must NOT happen is the other failure: claiming there is nothing unsaved.
    const { container } = render(<Workspace conversationMounted dirty={null} />)

    expect(tryToLeave()).toBe(false)
    expect(container.textContent).not.toMatch(/no unsaved|nothing unsaved|all saved|up to date/i)
  })

  it('says nothing when NOBODY has published, and asks nothing to find out', () => {
    // A project address with no conversation mounted. "Nobody has reported" is the same `null` as
    // "the check failed", and this plan adds no caller of the save-state endpoint anywhere — the
    // check costs two `git` executions inside the container and compares container-HEAD against
    // saved-bundle-HEAD, which a screen with no conversation has nothing to compare.
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    try {
      render(<Workspace conversationMounted={false} dirty={null} />)
      expect(tryToLeave()).toBe(false)
      expect(fetchSpy).not.toHaveBeenCalled()
    } finally {
      fetchSpy.mockRestore()
    }
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
    return <div data-testid="save-sub">{String(useWorkspaceSaveState())}</div>
  }

  /** Publishes from its OWN state, so the change reaches the subscribers only through the cells. */
  function SavePublisher() {
    const [dirty, setDirty] = useState<boolean | null>(null)
    usePublishSaveState(dirty)
    return <button type="button" onClick={() => setDirty(true)}>mark dirty</button>
  }

  it('a save-state publish does not re-render the pane subscriber', () => {
    // The rule that keeps the pane host still. One context value republished on every change would
    // re-render every consumer of the channel — which is the pane host, on every keystroke.
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
    usePublishAddress({ url: 'https://app.example/', status: 'ready' }, 'p1')
    usePublishPaneView(EMPTY_PANE)
    useAppPaneVisible(true)
    usePublishSaveState(true)
    return <div data-testid="surface" />
  }

  function Probe() {
    const address = useWorkspaceAddress()
    const pane = useWorkspacePane()
    const visible = useWorkspacePaneVisible()
    const dirty = useWorkspaceSaveState()
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
    // Every one of these four has its own reason, and making them uniform breaks something:
    //  - the address is kept because R8 IS "leaving this conversation does not destroy the running
    //    app"; it is bounded by the project instead of by its publisher's lifetime;
    //  - the save state is kept because the unsaved work is in the CONTAINER, not the component —
    //    which is the coverage hoisting the unload warning to the shell exists to add;
    //  - the pane view is dropped because it is a departed conversation's chrome;
    //  - visibility is dropped because a surface that is gone is not asking for anything.
    const view = render(<Workspace conversationMounted />)
    expect(probe()).toBe('https://app.example/|view|shown|true')

    view.rerender(<Workspace conversationMounted={false} />)

    expect(probe()).toBe('https://app.example/|no-view|hidden|true')
  })

  it('a DIFFERENT project invalidates a held address; an UNRESOLVED one does not', () => {
    // The one thing that can bound an address once its publisher is gone. The asymmetry matters:
    // every cold open of a chat address learns its project from a fetch, so a `null` claim means
    // "I do not know yet" — reading that as "some other project" would tear the app down while
    // the route resolved, which is R8 broken in the round trip it is most obviously about.
    function Declarer({ project }: { project: string | null }) {
      usePublishAddress({ url: 'https://app.example/', status: 'ready' }, 'p1')
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
 * THE IN-PLACE GUARD, MOUNTED AT SHELL LEVEL (Plan F, U8).
 *
 * Two guards, one requirement pair, and the property that has to hold between them: `beforeunload`
 * covers leaving the TAB and stays armed only on a definite `true`; this one covers leaving the
 * WORKSPACE without an unload and warns on `null` too, because an in-app dialog can carry a reason
 * the browser's fixed prompt cannot.
 *
 * Mounted HERE and not in the Outlet child, and the reason is structural: the exits it exists for —
 * the navbar's links — sit above the Outlet, so a guard mounted below it would lose coverage of
 * exactly the controls it was written for.
 */
describe('WorkspaceShell — the in-place unsaved-work guard (U8)', () => {
  function SurfaceWithSaveState({ dirty, running }: { dirty: boolean | null; running: boolean }) {
    usePublishSaveState(dirty)
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

  it('★ there is exactly ONE guard, not two', async () => {
    // "Never two guards" is held by construction here rather than by remembering to delete one:
    // Plan A hoisted the unload handler and this plan EXTENDS what A ships, adding no second
    // `beforeunload` listener and no second hook.
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
