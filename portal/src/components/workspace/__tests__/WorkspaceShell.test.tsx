/**
 * The workspace shell and the channel it holds.
 *
 * The shell's ROUTING claim — that the same element survives a move between the two addresses —
 * is `src/App.test.jsx`'s, because it is a claim about the route table and a hand-built table
 * here would prove the component instead of the wiring. This file has everything the shell does
 * once mounted: the height model, the grid, and above all the channel's rules.
 *
 * Two rules give the channel teeth: a publish must not wake a subscriber that did not care, and
 * whether a payload survives its publisher's unmount is decided per payload — the table in
 * `workspaceChannel.ts` names each one.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { useState, type ReactNode } from 'react'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import WorkspaceShell from '../WorkspaceShell'
import {
  createWorkspaceChannel,
  useAppPaneVisible,
  usePublishAddress,
  usePublishPaneView,
  usePublishSaveState,
  useWorkspaceAddress,
  useWorkspaceChannel,
  useWorkspacePane,
  useWorkspacePaneVisible,
  useWorkspaceProject,
  useWorkspaceSaveState,
  type PaneView,
} from '../workspaceChannel'

/**
 * Mount `child` as the shell's outlet content, the way a route element is. Every probe below
 * reads the channel from INSIDE the outlet — the same channel through the same context, but not
 * the pane host's tree position. That property is asserted where it lives: `App.test.jsx` for
 * the shell, `AppPaneHost.test.tsx` for the frame.
 *
 * `chrome` is what sits OUTSIDE the shell, where the navigation really is.
 */
function renderShell(child: ReactNode, chrome?: ReactNode) {
  return render(
    <MemoryRouter initialEntries={['/projects/p1']}>
      {chrome}
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

    // The chat model, taken as the shell's own: full height, no document scroll. What the
    // project surface loses by this — its document scroll — it declares for itself instead.
    expect(shellRoot().className).toMatch(/h-screen/)
    expect(shellRoot().className).toMatch(/overflow-hidden/)
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
    // `AdminPage`) are correctly not on the list — they are their own document.
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

/**
 * ★ WHAT THIS DESCRIBE PINS, INVERTED RATHER THAN DELETED: the shell used to mount a dialog that
 * asked the citizen which of their own two projects should keep the one workspace. The server now
 * gives the workspace to whichever project was asked for, so there is no question to put and
 * nothing here to mount it with.
 */
describe('WorkspaceShell — no dialog is mounted here at all', () => {
  it('★ the shell has no modal slot, and none appears however a surface publishes', () => {
    renderShell(<div data-testid="surface" />)

    expect(screen.queryByRole('dialog')).toBeNull()
    // LIVENESS: the shell really rendered, so an empty tree cannot green this.
    expect(screen.getByTestId('surface')).toBeTruthy()
  })

  it('★ and the channel carries no slot one could be published into', () => {
    // Asserted on the channel itself rather than on a render: a cell that still existed would let
    // any surface put a modal back over the pane without a single component changing.
    expect(Object.keys(createWorkspaceChannel())).not.toContain('reclaim')
  })
})

/**
 * WHAT THIS DESCRIBE USED TO PIN, INVERTED RATHER THAN DELETED: a `beforeunload` prompt that
 * warned before a tab close discarded unsaved work, narrowly, on a tri-state and a recovery
 * instant. Shutdown now writes that same work back automatically under an ancestry guard before a
 * container is destroyed — `WorkspaceLifecycleNotes` states the one case that write-back can fail
 * — so a tab close has nothing left to ask about. The save states the old warning branched on
 * still reach the channel (`WorkspaceLifecycleNotes` reads some of them), so this keeps exercising
 * all of them; only the expectation flips, from "warns" to "arms nothing at all".
 */
describe('WorkspaceShell — no tab-close warning is ever armed (the leaving-the-page half, inverted)', () => {
  /** THE RECOVERY INSTANT DEFAULTS TO `null`, matching the states the old warning distinguished. */
  function Surface({ dirty, recoveryAt = null }: { dirty: boolean | null; recoveryAt?: string | null }) {
    useWorkspaceProject('p1')
    usePublishSaveState({ dirty, recoveryAt })
    return <div data-testid="surface" />
  }

  /** A conversation that publishes, then a project screen that does not. */
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

  /** Every `addEventListener` call the tree makes, so an unload-family listener has nowhere to hide. */
  function watchListeners() {
    const added: string[] = []
    const original = window.addEventListener.bind(window)
    const spy = vi.spyOn(window, 'addEventListener').mockImplementation((type, ...rest) => {
      added.push(String(type))
      return original(type, ...(rest as [EventListenerOrEventListenerObject]))
    })
    return { added, restore: () => spy.mockRestore() }
  }

  it('arms no listener on a definite `true`, mounted or after the conversation unmounts', () => {
    const { added, restore } = watchListeners()
    const view = render(<Workspace conversationMounted dirty />)
    view.rerender(<Workspace conversationMounted={false} dirty />)

    expect(screen.getByTestId('project-only')).toBeTruthy()
    expect(added).not.toContain('beforeunload')
    restore()
  })

  it('arms no listener when the state is definitely clean', () => {
    const { added, restore } = watchListeners()
    render(<Workspace conversationMounted dirty={false} />)
    expect(added).not.toContain('beforeunload')
    restore()
  })

  it('arms no listener when the state is UNKNOWN, and claims nothing on screen either', () => {
    const { added, restore } = watchListeners()
    const { container } = render(<Workspace conversationMounted dirty={null} />)

    expect(added).not.toContain('beforeunload')
    expect(container.textContent).not.toMatch(/no unsaved|nothing unsaved|all saved|up to date/i)
    restore()
  })

  it('asks nothing when NOBODY has published a save state', () => {
    // Nothing here calls the save-state endpoint — that check costs two `git` executions inside
    // the container, which a screen with no conversation has nothing to compare.
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    const { added, restore } = watchListeners()
    try {
      render(<Workspace conversationMounted={false} dirty={null} />)
      expect(fetchSpy).not.toHaveBeenCalled()
      expect(added).not.toContain('beforeunload')
    } finally {
      fetchSpy.mockRestore()
      restore()
    }
  })

  it('arms no listener whether or not the platform holds a recoverable copy', () => {
    // The distinction this describe used to warn on both sides of — recoverable and not — collapses
    // to the same outcome now: neither arms anything.
    const { added, restore } = watchListeners()
    const view = render(<Workspace conversationMounted dirty recoveryAt="2026-09-10T10:38:43Z" />)
    expect(screen.getByTestId('surface')).toBeTruthy()

    view.rerender(<Workspace conversationMounted dirty recoveryAt={null} />)

    expect(added).not.toContain('beforeunload')
    restore()
  })
})

/**
 * THE SOURCE-LEVEL HALF OF THE SAME CLAIM. The render-based tests above only see what THEIR OWN
 * tree mounts; a listener added by a module none of them renders would pass every one of them and
 * still ship. Read as source, over the whole client, so there is nowhere left for one to hide.
 */
describe('no `beforeunload` listener remains anywhere in the client', () => {
  function sourceFiles(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      if (entry.name === '__tests__' || entry.name === 'node_modules') return []
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) return sourceFiles(full)
      return /\.(ts|tsx|js|jsx)$/.test(entry.name) ? [full] : []
    })
  }

  it('is absent from every shipped file', () => {
    const offending = sourceFiles('src').flatMap((file) =>
      readFileSync(file, 'utf8')
        .split('\n')
        .flatMap((line, i) => {
          const code = line.trimStart()
          if (code.startsWith('//') || code.startsWith('*') || code.startsWith('/*')) return []
          return /beforeunload/i.test(line) ? [`${file}:${i + 1}`] : []
        }),
    )
    expect(offending).toEqual([])
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
