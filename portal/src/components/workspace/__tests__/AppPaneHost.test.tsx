/**
 * The app pane host (Plan A, U4) — one iframe for the whole workspace.
 *
 * ═══ WHAT THIS FILE PROVES, AND WHAT IT CANNOT ═══
 *
 * It proves ELEMENT IDENTITY: that moving between the two addresses inside a project keeps the same
 * iframe DOM node, that a genuinely different address replaces it, and that a hidden pane is still
 * in the document and still inert. That is the mechanism R8 rests on, and it is exactly what jsdom
 * can answer.
 *
 * It CANNOT prove that a real cross-origin frame did not reload. jsdom does not fetch the `src`, so
 * "the same node, and its load handler did not fire again" is as close as the unit suite gets. The
 * browser suite is not in CI and is stale against `main`, so this plan does not claim what it
 * cannot show here: one scripted manual browser pass is named as a release condition on the PR
 * instead — open a project, open a build chat with a running preview, go back to the project,
 * return, and confirm the frame's content window is the same one and the framing handshake did not
 * re-run.
 *
 * ═══ THE FAILURE MODES THESE SCENARIOS ARE WRITTEN AGAINST ═══
 *
 * The general shape is "buy continuity by weakening what identifies the frame", and it has one
 * form per layer. The famous one — pinning the IFRAME's key to a constant — lives inside
 * `LivePreview` and is pinned in its own suite (`LivePreview.test.jsx:135`, a new URL remounts),
 * so it is deliberately not re-asserted here; mutating it at THIS layer changes nothing, because
 * the identity that matters is the iframe's, not this component's.
 *
 * What is this layer's to get wrong is the ADDRESS staying live and staying correct, and it fails
 * in three directions rather than one — each with a scenario below, and each mutation-checked:
 *
 *  - the identity picks up something that is not the address (the route, the chat, the rail mode),
 *    so an ordinary navigation reloads a running app;
 *  - the address is cleared when the surface that published it unmounts, so leaving a build chat
 *    for the project screen destroys the app — R8, broken in the transition it most obviously
 *    covers;
 *  - the address is kept too long, so a frame quietly holds one project's container alive while
 *    the citizen works in another, invisibly, for the life of the tab.
 *
 * Every continuity assertion here is therefore paired with a discontinuity one.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { useState, useEffect, type ReactNode } from 'react'
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route, Link, useParams } from 'react-router-dom'
import WorkspaceShell from '../WorkspaceShell'
import {
  useAppPaneVisible,
  usePublishAddress,
  usePublishPaneView,
  useWorkspaceChannel,
  useWorkspaceProject,
  type PaneView,
} from '../workspaceChannel'
import type { BuildSessionStatus } from '../../../utils/buildSessionTypes'

vi.mock('../../layout/Navbar', () => ({ default: () => <div data-testid="navbar" /> }))

const APP_URL = 'https://app-a.example.azurecontainerapps.io/'
const OTHER_APP_URL = 'https://app-b.example.azurecontainerapps.io/'

const EMPTY_PANE: PaneView = {
  iterating: false, reconnecting: false,
  relaunching: false, relaunchError: null, lastBuildFailed: false,
  restoredFromFailedBuild: false, completedLive: false, hasSavedBuild: null,
  previewState: null, occupyingProjectName: null, turnRunning: false,
  compileState: null, workspaceLost: false,
}

/** A conversation surface: declares its project, publishes an address, asks to be seen. */
function ChatSurface({
  projectId = 'pA',
  url = APP_URL as string | null,
  visible = true,
  pane,
  status,
}: {
  projectId?: string
  url?: string | null
  visible?: boolean
  pane?: Partial<PaneView>
  /** Overrides the default live `ready`. The TERMINAL statuses are what `completedLive` guards. */
  status?: BuildSessionStatus
}) {
  useWorkspaceProject(projectId)
  usePublishAddress({ url, status: url ? (status ?? 'ready') : null }, projectId)
  usePublishPaneView({ ...EMPTY_PANE, ...pane })
  useAppPaneVisible(visible)
  return <div data-testid="chat-surface" />
}

/**
 * THE SAME SURFACE, MOUNTING COLD — and the distinction the suite above cannot make.
 *
 * `ChatSurface` takes its url as a constant prop, so a remount republishes the SAME address and the
 * return leg of a round trip is never actually tested. The real `BuilderPage` has no constant: every
 * arm of `resolvePreviewAddress` reads hook state or a ref that is fresh per mount (a session hook
 * starts null, a turn-narrative ref starts unset, a transcript starts empty), and the URL only
 * arrives after a hydrate/reattach round trip. So its FIRST commit resolves nothing, and that is the
 * commit that used to retire the held address and tear the frame down on the way back in.
 */
function ColdChatSurface({ projectId = 'pA', pane }: { projectId?: string; pane?: Partial<PaneView> }) {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    setUrl(APP_URL)
  }, [])
  useWorkspaceProject(projectId)
  usePublishAddress({ url, status: url ? 'ready' : null }, projectId)
  usePublishPaneView({ ...EMPTY_PANE, ...pane })
  useAppPaneVisible(true)
  return <div data-testid="chat-surface" />
}

/** The project screen before Plan F: it declares its project and asks for nothing. */
function ProjectSurface({ projectId = 'pA' }: { projectId?: string }) {
  useWorkspaceProject(projectId)
  return (
    <div data-testid="project-surface">
      {/* The affordances the project page's own suite asserts the ABSENCE of. None of them may
          arrive with a hidden pane — see the inertness scenario below. */}
    </div>
  )
}

const frame = () => document.querySelector('iframe')
const paneWrapper = () => screen.queryByTestId('app-pane')

/** Two addresses under one shell, navigated by link exactly as the product navigates them. */
function Workspace({ chatSurface }: { chatSurface: ReactNode }) {
  return (
    <MemoryRouter initialEntries={['/chat/c1']}>
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route
            path="/chat/:chatId"
            element={
              <>
                <Link to="/projects/pA">to project</Link>
                <Link to="/projects/pB">to other project</Link>
                {chatSurface}
              </>
            }
          />
          <Route
            path="/projects/:projectId"
            element={<ProjectAddress />}
          />
        </Route>
      </Routes>
    </MemoryRouter>
  )
}

/** The project screen, declaring whichever project the URL names. */
function ProjectAddress() {
  const { projectId } = useParams()
  return (
    <>
      <Link to="/chat/c1">to chat</Link>
      <ProjectSurface projectId={projectId ?? 'pA'} />
    </>
  )
}

afterEach(() => cleanup())

describe('AppPaneHost — the frame outlives a move between the two addresses (AE4)', () => {
  it('keeps the SAME iframe node across chat → project → chat, and never re-issues its src', () => {
    // The transition the whole plan exists for. Before it, the pane existed because `BuilderPage`
    // was the page that matched, so going back to the project destroyed the running app.
    render(<Workspace chatSurface={<ChatSurface />} />)
    const original = frame()
    expect(original).toBeTruthy()
    expect(original?.getAttribute('src')).toBe(APP_URL)

    let loads = 0
    original?.addEventListener('load', () => { loads += 1 })

    fireEvent.click(screen.getByText('to project'))
    expect(screen.getByTestId('project-surface')).toBeTruthy()
    expect(frame()).toBe(original) // still there, and it is the SAME element

    fireEvent.click(screen.getByText('to chat'))
    expect(screen.getByTestId('chat-surface')).toBeTruthy()
    expect(frame()).toBe(original)
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
    expect(loads).toBe(0)
  })

  it('hides the pane on the project address rather than discarding it', () => {
    // "Hidden, not unmounted" IS the requirement: the pane is a cross-origin frame whose `src` is
    // re-issued on remount, and re-issuing it means a full reload plus a fresh framing handshake.
    render(<Workspace chatSurface={<ChatSurface />} />)
    expect(paneWrapper()?.className).toMatch(/flex-1/)

    fireEvent.click(screen.getByText('to project'))

    const wrapper = paneWrapper()
    expect(wrapper).toBeTruthy()
    expect(frame()).toBeTruthy() // the liveness half: a host that threw would read as a pass
    expect(wrapper?.className).toMatch(/invisible/)
    expect(wrapper?.className).toMatch(/w-0/)
    expect(wrapper?.getAttribute('aria-hidden')).toBe('true')
  })

  it('leaves the frame alone when the conversation unmounts MID-BUILD', () => {
    // THE REGRESSION THIS SUITE WAS BLIND TO, and it was blind for a reason worth writing down:
    // every other scenario here pins `iterating: false`, so none of them could see it.
    //
    // The pane view is cleared when its publisher unmounts. `iterating` rode along with it, fell
    // back to `LivePreview`'s prop default, and the pane read that true→false edge as "a turn just
    // ended over a live preview" — its signal to re-request the document. So leaving a build chat
    // for the project screen WHILE A BUILD WAS RUNNING reloaded the app: silently, and about an
    // event that had not happened. That is R8 broken in the one transition this host exists for,
    // and it is why `iterating` is held by the host rather than treated as chrome.
    render(<Workspace chatSurface={<ChatSurface pane={{ iterating: true }} />} />)
    const original = frame()
    expect(original).toBeTruthy()

    fireEvent.click(screen.getByText('to project'))

    expect(frame()).toBe(original)
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
  })

  it('survives the RETURN leg, when the remounted surface has not resolved an address yet', () => {
    // THE OTHER HALF OF R8, and the half every scenario above was structurally unable to see: they
    // all hand `ChatSurface` a constant url, so their remount republishes the same address and the
    // return leg is asserted without ever being exercised.
    //
    // A real surface mounts COLD. Its first commit resolves `{url: null, status: null}`, and the
    // publish ran on every render with no gate — so coming BACK into the build chat retired the
    // address the outbound leg had just gone to such lengths to keep, unmounted the iframe, and
    // then mounted a brand-new one once the reattach landed. The citizen watched their running app
    // reload on the way back in: R8 broken through the opposite door from the one it was fixed at.
    //
    // A publisher with nothing to say now abstains until it has an answer of its own.
    render(<Workspace chatSurface={<ColdChatSurface />} />)
    const original = frame()
    expect(original).toBeTruthy()
    expect(original?.getAttribute('src')).toBe(APP_URL)

    fireEvent.click(screen.getByText('to project'))
    expect(frame()).toBe(original) // outbound: the leg that already worked

    fireEvent.click(screen.getByText('to chat'))
    expect(frame()).toBe(original) // return: the leg that did not
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
  })

  it('leaves the frame alone when the conversation unmounts right after a build SUCCEEDS', () => {
    // The sibling of the mid-build scenario, on the more common exit. A finished build's container
    // is PARDONED — alive under an idle lease — and `completedLive` is the pane field carrying that
    // claim; it is what lets `keepFramed` outrank the address's terminal `ended` status.
    //
    // `completedLive` rode on the pane view, which is CLEARED on unmount, so it fell back to
    // `LivePreview`'s `false` default over an address whose `ended` status is deliberately KEPT.
    // `frameContext` collapsed and the iframe was unmounted — leaving a build chat at the moment a
    // citizen is most likely to leave one destroyed an app the server was still serving.
    // `ended` is the address status a completed build rests at, and the address KEEPS it. Without
    // it this scenario would false-green: a non-terminal status frames regardless of `completedLive`.
    render(
      <Workspace chatSurface={<ChatSurface status="ended" pane={{ completedLive: true }} />} />,
    )
    const original = frame()
    expect(original).toBeTruthy()

    fireEvent.click(screen.getByText('to project'))

    expect(frame()).toBe(original)
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
  })

  it('but leaving for ANOTHER project\'s screen takes the frame down', () => {
    // The other side of "the address outlives its publisher", and the reason it needed bounding at
    // all. Kept for the whole life of the tab, a held address means a frame quietly holding one
    // project's container alive while the citizen works in another — invisible, so nothing would
    // ever surface it. A different project is a different app.
    render(<Workspace chatSurface={<ChatSurface />} />)
    expect(frame()).toBeTruthy()

    fireEvent.click(screen.getByText('to other project'))

    expect(frame()).toBeNull()
    expect(paneWrapper()).toBeNull()
  })
})

describe('AppPaneHost — which column grows, and which one is sized', () => {
  // THE REGRESSION THIS EXISTS TO CATCH, and jsdom cannot measure a pixel of it. The two columns
  // are the conversation and the app. Before the extraction the builder surface owned both, so its
  // 288px chat panel sat beside a `flex-1` preview. Split across the shell's grid with BOTH columns
  // at `flex-1`, the workspace halves: the panel keeps its 288px inside a column twice its width
  // and the app loses half the screen it had. Nothing in the unit suite would have said a word.
  //
  // REWRITTEN FOR PLAN F'S SETTLED WIDTHS, and the property is the same one. When Plan A shipped
  // this, the rail had no width of its own, so "not `flex-1`" WAS the whole signal — the column
  // fell back to its content. Plan F gives it two settled widths and a stacked crossing, both
  // expressed as classes on this same element, so the honest assertion is now: at the two-column
  // breakpoint the rail is the SIZED column (`lg:flex-none` plus a settled `lg:w-[…]`) and the
  // pane is the growing one. The `flex-1` that remains is the STACKED case, where the two share a
  // column and both must grow — asserting its absence would now be asserting that the layout below
  // the threshold is broken.
  const outlet = () => screen.getByTestId('workspace-outlet')

  /** The rail is the sized column: a settled width, and not the one that grows, at `lg`. */
  const expectRailIsSized = (className: string) => {
    // ONE CLASS, ONE CUSTOM PROPERTY (plan 002, U7). The width was a literal per rail mode; it is
    // the citizen's own now, carried on `--rail-w` and consumed only above the stacking threshold.
    // The class no longer says WHICH width — that is the element's style — so the assertion is
    // that the rail is SIZED rather than growing.
    expect(className).toMatch(/wide:flex-none/)
    expect(className).toMatch(/wide:w-\[var\(--rail-w\)\]/)
  }

  /** The rail is the whole surface: it grows, and carries no settled width to be pinned to. */
  const expectRailIsEverything = (className: string) => {
    expect(className).toMatch(/flex-1/)
    expect(className).not.toMatch(/wide:w-\[/)
    expect(className).not.toMatch(/wide:flex-none/)
  }

  it('with the pane visible, the conversation column is SIZED and the pane takes the rest', () => {
    render(<Workspace chatSurface={<ChatSurface />} />)

    expectRailIsSized(outlet().className)
    expect(paneWrapper()?.className).toMatch(/flex-1/)
  })

  it('with nothing asking for the pane, the conversation column IS the whole surface', () => {
    // Every planning conversation — which under Plan F is the one surface with no pane at all.
    render(<Workspace chatSurface={<ChatSurface visible={false} />} />)

    expectRailIsEverything(outlet().className)
    expect(paneWrapper()?.className).toMatch(/w-0/)
  })

  it('and the split follows the pane back and forth across a navigation', () => {
    render(<Workspace chatSurface={<ChatSurface />} />)
    expectRailIsSized(outlet().className)

    fireEvent.click(screen.getByText('to project'))
    expectRailIsEverything(outlet().className)

    fireEvent.click(screen.getByText('to chat'))
    expectRailIsSized(outlet().className)
  })

  it('gives the conversation the WIDER of the two OPENING widths', () => {
    // Two opening widths, and which is which is not arbitrary: a conversation holds a transcript
    // and a composer, the project's details do not. Taken from the canvas's 400px and 520px.
    // They are the OPENING widths now rather than settled ones — once the citizen has dragged,
    // their own width replaces both, which is the board's "drag it once and every project opens
    // there". The number lives on the element's style, because the class is shared.
    render(<Workspace chatSurface={<ChatSurface />} />)
    expect(outlet().style.getPropertyValue('--rail-w')).toBe('520px')
  })
})

describe('AppPaneHost — a hidden pane is genuinely inert, at shell level', () => {
  // ASSERTED HERE AND NOT IN `ProjectPage.test.tsx`, ON PURPOSE. That suite renders the project
  // page with no shell and stubs `LivePreview` to null, so its `queryByTestId('live-preview')`
  // assertions cannot observe anything the shell mounts and would stay green against a pane
  // leaking onto the project screen. Its assertions still pass and stay Plan F's to invert
  // deliberately; they are not evidence for this unit.
  it('is in the document, out of the tab order, out of the accessibility tree, and offers nothing', () => {
    render(<Workspace chatSurface={<ChatSurface />} />)
    fireEvent.click(screen.getByText('to project'))

    const wrapper = paneWrapper() as HTMLElement
    // THE LIVENESS HALF FIRST. Without it every assertion below passes against a host that
    // rendered nothing at all, which is the assert-absence false-green in its purest form.
    expect(wrapper.querySelector('iframe')).toBeTruthy()

    // `visibility:hidden` rather than `aria-hidden` alone: zero width and overflow:hidden clip a
    // subtree visually but leave its descendants in the tab order.
    expect(wrapper.className).toMatch(/invisible/)
    expect(wrapper.getAttribute('aria-hidden')).toBe('true')

    // None of the affordances the project screen's tests pin the absence of arrive with it.
    for (const name of [/view app/i, /continue building/i, /open app/i]) {
      expect(screen.queryByRole('button', { name })).toBeNull()
      expect(screen.queryByRole('link', { name })).toBeNull()
    }
    expect(screen.queryByText(/^draft$/i)).toBeNull()
  })
})

describe('AppPaneHost — identity is the address, and a different app is a real remount', () => {
  function Switchable() {
    const [url, setUrl] = useState<string | null>(APP_URL)
    const [project, setProject] = useState('pA')
    return (
      <>
        <button type="button" onClick={() => setUrl(null)}>drop</button>
        <button type="button" onClick={() => setUrl(APP_URL)}>restore</button>
        <button type="button" onClick={() => { setUrl(OTHER_APP_URL); setProject('pB') }}>other project</button>
        <ChatSurface projectId={project} url={url} />
      </>
    )
  }

  const renderSwitchable = () =>
    render(
      <MemoryRouter initialEntries={['/chat/c1']}>
        <Routes>
          <Route element={<WorkspaceShell />}>
            <Route path="/chat/:chatId" element={<Switchable />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )

  it('the address becoming null removes the frame; re-acquiring one mounts a NEW frame', () => {
    // A real remount, and it is correct: nothing was being framed in between, so there is no
    // document to preserve. This is the half that a constant key would silently break.
    renderSwitchable()
    const original = frame()
    expect(original).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'drop' }))
    expect(frame()).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'restore' }))
    expect(frame()).toBeTruthy()
    expect(frame()).not.toBe(original)
  })

  it('a DIFFERENT project is a different app, so a different address, so a deliberate remount', () => {
    renderSwitchable()
    const original = frame()

    fireEvent.click(screen.getByRole('button', { name: 'other project' }))

    expect(frame()?.getAttribute('src')).toBe(OTHER_APP_URL)
    expect(frame()).not.toBe(original)
  })

  it('re-rendering the surface without changing the address keeps the same node', () => {
    // The rule the channel exists to hold: a publish must not change the host's identity inputs
    // unless the address genuinely changed. The surface republishes its pane view on every render
    // — a composer keystroke is one — and none of that may reach the frame.
    function Typist() {
      const [text, setText] = useState('')
      return (
        <>
          <input aria-label="composer" value={text} onChange={(e) => setText(e.target.value)} />
          <ChatSurface />
        </>
      )
    }
    render(
      <MemoryRouter initialEntries={['/chat/c1']}>
        <Routes>
          <Route element={<WorkspaceShell />}>
            <Route path="/chat/:chatId" element={<Typist />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )
    const original = frame()

    fireEvent.change(screen.getByLabelText('composer'), { target: { value: 'add a chart' } })
    fireEvent.change(screen.getByLabelText('composer'), { target: { value: 'add a chart please' } })

    expect(frame()).toBe(original)
  })
})

describe('AppPaneHost — a layout change does not remount the frame (AE37)', () => {
  it('flipping the shell\'s grid between side-by-side and stacked keeps the same iframe node', () => {
    // The class comes from a fixture here; Plan F owns the threshold that produces it in the
    // product. What is assertable NOW — and what makes the claim about this plan's own element
    // rather than an arbitrary wrapper — is that the container whose class changes is the shell's
    // grid, and the pane host is its sibling.
    function Flipper() {
      const channel = useWorkspaceChannel()
      const [stacked, setStacked] = useState(false)
      return (
        <>
          <button
            type="button"
            onClick={() => { setStacked(!stacked); channel?.rail.set({ mode: null, stacked: !stacked, collapsed: false }) }}
          >
            flip
          </button>
          <ChatSurface />
        </>
      )
    }
    render(
      <MemoryRouter initialEntries={['/chat/c1']}>
        <Routes>
          <Route element={<WorkspaceShell />}>
            <Route path="/chat/:chatId" element={<Flipper />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )
    const original = frame()
    expect(screen.getByTestId('workspace-grid').className).toMatch(/flex-row/)

    fireEvent.click(screen.getByRole('button', { name: 'flip' }))

    expect(screen.getByTestId('workspace-grid').className).toMatch(/flex-col/)
    expect(frame()).toBe(original)
  })
})

describe('AppPaneHost — the framed app\'s message gate survived the move', () => {
  it('still refuses a message whose source is not the current frame\'s window', () => {
    // The gate validates on BOTH `e.origin` and `e.source === frameRef.current?.contentWindow`, and
    // both halves stay: origin proves the bytes came from the apps host, source proves they came
    // from THIS pane's app rather than any other one on it. Moving the mount site must not have
    // loosened either — the ref is now created inside a component the shell renders, not the page.
    const onFrameMessage = vi.fn()
    render(
      <MemoryRouter initialEntries={['/chat/c1']}>
        <Routes>
          <Route element={<WorkspaceShell />}>
            <Route path="/chat/:chatId" element={<ChatSurface pane={{ onFrameMessage }} />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )
    const iframe = frame() as HTMLIFrameElement
    expect(iframe).toBeTruthy()

    const origin = new URL(APP_URL).origin
    // Right origin, WRONG sender — a nested or sibling frame on the same apps host.
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { origin, source: window, data: { hi: 1 } }))
    })
    expect(onFrameMessage).not.toHaveBeenCalled()

    // Right origin, right sender.
    act(() => {
      window.dispatchEvent(
        new MessageEvent('message', { origin, source: iframe.contentWindow, data: { hi: 2 } }),
      )
    })
    expect(onFrameMessage).toHaveBeenCalledWith({ hi: 2 })
  })
})

describe('AppPaneHost — nothing to host is not the same as hidden', () => {
  it('renders no pane at all at a project address that has never framed anything', () => {
    // R3 stays true before Plan F owns the start control: the host frames what already exists and
    // never asks for an address, so a project screen with nothing built costs nothing at all.
    render(
      <MemoryRouter initialEntries={['/projects/pA']}>
        <Routes>
          <Route element={<WorkspaceShell />}>
            <Route path="/projects/:projectId" element={<ProjectSurface />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )

    expect(paneWrapper()).toBeNull()
    expect(frame()).toBeNull()
  })
})
