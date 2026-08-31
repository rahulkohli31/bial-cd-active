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
import { useState, type ReactNode } from 'react'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import WorkspaceShell from '../WorkspaceShell'
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

vi.mock('../../layout/Navbar', () => ({ default: () => <div data-testid="navbar" /> }))

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
          channel?.rail.set({ mode: null, state: {}, stacked: !stacked })
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
        onClick={() => setRequest({ blocked, resolve: onResolve, cancel: () => setRequest(null) })}
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

    fireEvent.click(screen.getByRole('button', { name: /switch without saving/i }))
    expect(onResolve).toHaveBeenCalledWith(false)
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
