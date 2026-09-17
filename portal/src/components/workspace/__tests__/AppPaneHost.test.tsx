/**
 * The app pane host — one iframe for the whole workspace.
 *
 * This suite proves ELEMENT IDENTITY: moving between the two addresses inside a project keeps the
 * same iframe node, a genuinely different address replaces it, and a hidden pane is still in the
 * document and still inert. It cannot prove a real cross-origin frame did not reload — jsdom does
 * not fetch the `src`, so "the same node, and its load handler did not fire again" is as close as
 * a unit suite gets.
 *
 * Pinning the iframe's own key is `LivePreview`'s to get wrong and is asserted in that suite, not
 * re-asserted here. Every continuity assertion below is paired with a discontinuity one.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { useState, useEffect, type ReactNode } from 'react'
import { render, screen, fireEvent, cleanup, act, waitFor } from '@testing-library/react'
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

const APP_URL = 'https://app-a.example.azurecontainerapps.io/'
const OTHER_APP_URL = 'https://app-b.example.azurecontainerapps.io/'

const EMPTY_PANE: PaneView = {
  iterating: false, reconnecting: false,
  previewState: null, turnRunning: false,
  compileState: null, workspaceLost: false,
}

/** A conversation surface: declares its project, publishes an address, asks to be seen. */
function ChatSurface({
  projectId = 'pA',
  url = APP_URL as string | null,
  visible = true,
  pane,
  status,
  serving = true,
}: {
  projectId?: string
  url?: string | null
  visible?: boolean
  pane?: Partial<PaneView>
  /** Overrides the default live `ready`. The TERMINAL statuses are what `serving` guards. */
  status?: BuildSessionStatus
  /** Is a container still answering at `url`? Rides on the ADDRESS, which survives an unmount. */
  serving?: boolean
}) {
  useWorkspaceProject(projectId)
  usePublishAddress({ url, status: url ? (status ?? 'ready') : null, serving: serving && url !== null }, projectId)
  usePublishPaneView({ ...EMPTY_PANE, ...pane })
  useAppPaneVisible(visible)
  return <div data-testid="chat-surface" />
}

/**
 * THE SAME SURFACE, MOUNTING COLD — and the distinction the suite above cannot make.
 *
 * `ChatSurface` takes its url as a constant prop, so a remount republishes the SAME address and the
 * return leg of a round trip is never actually exercised. The real surface has no constant:
 * every arm of `resolvePreviewAddress` reads hook state or a ref that is fresh per mount, and the
 * URL only arrives after a hydrate/reattach round trip, so its FIRST commit resolves nothing —
 * the commit that used to retire the held address and tear the frame down on the way back in.
 */
function ColdChatSurface({
  projectId = 'pA',
  pane,
  status = 'ready',
}: {
  projectId?: string
  pane?: Partial<PaneView>
  /** The status the resolved address settles at. TERMINAL is what liveness has to outrank. */
  status?: BuildSessionStatus
}) {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    setUrl(APP_URL)
  }, [])
  useWorkspaceProject(projectId)
  usePublishAddress({ url, status: url ? status : null, serving: url !== null }, projectId)
  usePublishPaneView({ ...EMPTY_PANE, ...pane })
  useAppPaneVisible(true)
  return <div data-testid="chat-surface" />
}

/** The project screen: it declares its project and asks for nothing. */
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

describe('AppPaneHost — the frame outlives a move between the two addresses', () => {
  it('keeps the SAME iframe node across chat → project → chat, and never re-issues its src', () => {
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

  it('hides the pane on the project address rather than discarding it', async () => {
    // "Hidden, not unmounted" is the requirement — see `AppPaneHost`.
    render(<Workspace chatSurface={<ChatSurface />} />)
    expect(paneWrapper()?.className).toMatch(/flex-1/)

    fireEvent.click(screen.getByText('to project'))

    const wrapper = paneWrapper()
    expect(wrapper).toBeTruthy()
    expect(frame()).toBeTruthy() // the liveness half: a host that threw would read as a pass
    // Awaited because the departure animates — `aria-hidden` lands immediately, `invisible`/`w-0` follow.
    expect(wrapper?.getAttribute('aria-hidden')).toBe('true')
    await waitFor(() => expect(paneWrapper()?.className).toMatch(/invisible/))
    expect(paneWrapper()?.className).toMatch(/w-0/)
  })

  it('leaves the frame alone when the conversation unmounts MID-BUILD', () => {
    // THE REGRESSION THIS SUITE WAS BLIND TO, and it was blind for a reason worth writing down:
    // every other scenario here pins `iterating: false`, so none of them could see it. Why the
    // host holds `iterating` rather than treating it as chrome is recorded in `AppPaneHost`.
    render(<Workspace chatSurface={<ChatSurface pane={{ iterating: true }} />} />)
    const original = frame()
    expect(original).toBeTruthy()

    fireEvent.click(screen.getByText('to project'))

    expect(frame()).toBe(original)
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
  })

  it('survives the RETURN leg, when the remounted surface has not resolved an address yet', () => {
    // The one leg the constant-url scenarios above cannot exercise — see `ColdChatSurface`.
    // A publisher with nothing to say abstains until it has an answer of its own, rather than
    // publishing an empty address that retires the one the outbound leg kept.
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

  it('★ a COLD REMOUNT whose address resolves AFTER mount keeps the frame, terminal status and all', () => {
    // ★ THE SCENARIO A CONSTANT-INPUT FIXTURE CANNOT WRITE, and the reason `ColdChatSurface` exists:
    // its URL arrives in an effect, so its FIRST commit resolves nothing — which is what every real
    // surface does (a session hook starts null, a turn narrative ref starts unset, a transcript
    // starts empty, and the URL only lands after a reattach round trip).
    //
    // WHAT THIS ADDS OVER THE COLD SCENARIO ABOVE: a TERMINAL status. With `ready` the frame
    // survives whatever liveness says, so the cold round trip is asserted without the field being
    // exercised at all. At `ended`, the frame exists only while something is serving — so this is
    // the cell where "liveness rides on the address" is actually load-bearing.
    //
    // Mutation check: put liveness back on the pane view (which is CLEARED on unmount) and the
    // return leg goes red — the cold surface's first commit hands the pane a `false` default over
    // an address whose `ended` status is deliberately KEPT, and the iframe comes down.
    render(<Workspace chatSurface={<ColdChatSurface status="ended" />} />)
    const original = frame()
    expect(original).toBeTruthy()

    fireEvent.click(screen.getByText('to project'))
    expect(frame()).toBe(original)

    fireEvent.click(screen.getByText('to chat'))
    expect(frame()).toBe(original)
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
    // LIVENESS, PAIRED: the pane is framing rather than showing its terminal card, so the identity
    // assertions above are about a live pane and not about a render that produced nothing.
    expect(screen.queryByTestId('preview-ended-card')).toBeNull()
  })

  it('leaves the frame alone when the conversation unmounts right after a build SUCCEEDS', () => {
    // The sibling of the mid-build scenario, on the more common exit. A finished turn's container
    // is PARDONED — alive under an idle lease — and liveness is what lets `keepFramed` outrank the
    // address's terminal `ended` status.
    //
    // IT USED TO RIDE ON THE PANE VIEW, as `completedLive`, and the pane view is CLEARED on unmount
    // — so it fell back to `LivePreview`'s `false` default over an address whose `ended` status is
    // deliberately KEPT. `frameContext` collapsed and the iframe was unmounted: leaving a build chat
    // at the moment a citizen is most likely to leave one destroyed an app the server was still
    // serving. The host first answered that with a held ref, then was reworked to answer it
    // structurally, by moving liveness onto the address, which survives the unmount for the same
    // reason the URL does.
    //
    // `ended` is the address status a completed build rests at, and the address KEEPS it. Without
    // it this scenario would false-green: a non-terminal status frames regardless of liveness.
    render(<Workspace chatSurface={<ChatSurface status="ended" />} />)
    const original = frame()
    expect(original).toBeTruthy()

    fireEvent.click(screen.getByText('to project'))

    expect(frame()).toBe(original)
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
  })

  it('★ a terminal address with NOTHING serving still collapses — the hold is not a blanket keep-alive', () => {
    // THE OTHER HALF OF THE SCENARIO ABOVE, and the reason it is not vacuous. Moving liveness onto
    // a cell that survives an unmount is only safe if the cell can still say "no": a version that
    // simply never let go of the frame would pass every continuity assertion in this file and leave
    // a frame pointing at a container that is gone, with nothing able to detect it — which is the
    // failure `AppPaneHost`'s own docblock is written against.
    //
    // Paired with a liveness assertion, because "no iframe" is also what a crashed render looks
    // like.
    //
    // ★ THE LIVENESS HANDLE MOVED, AND THE MOVE IS THE CHANGE. It was `preview-ended-card` — the
    // pane's own "The preview is no longer running" card — which is DELETED. That card was a
    // verdict about the citizen's WORKSPACE, drawn by the one component that can only see a frame,
    // and `workspace/workspaceState.ts` computes that sentence once for the whole product. So what
    // proves this render happened is the pane's permanent live region, which is mounted in every
    // state of `LivePreview` and is the one element that exists whether or not it has anything to
    // say. See `components/__tests__/LivePreview.test.tsx` for the deletion itself.
    render(<Workspace chatSurface={<ChatSurface status="ended" serving={false} />} />)

    expect(frame()).toBeNull()
    const spoken = document.querySelector('[role="status"][aria-live="polite"]')
    expect(spoken).toBeTruthy()
    expect(screen.queryByTestId('preview-ended-card')).toBeNull()
  })

  it('but leaving for ANOTHER project\'s screen takes the frame down', () => {
    // The bound on a held address, and the reason it needs one: kept for the whole life of the
    // tab, a held address means a frame quietly holding one project's container alive while the
    // citizen works in another — invisible, so nothing would ever surface it. A different
    // project is a different app, and a legitimate remount.
    render(<Workspace chatSurface={<ChatSurface />} />)
    expect(frame()).toBeTruthy()

    fireEvent.click(screen.getByText('to other project'))

    expect(frame()).toBeNull()
    expect(paneWrapper()).toBeNull()
  })
})

describe('AppPaneHost — which column grows, and which one is sized', () => {
  // jsdom cannot measure a pixel, so a column that silently lost its share of the screen would
  // pass every assertion here unless something checks WHICH column is sized. At the two-column
  // breakpoint the rail is the sized column (`lg:flex-none` + a settled `lg:w-[…]`) and the pane
  // grows; `flex-1` on the rail is only correct in the stacked case, so asserting its absence
  // here is the actual regression check.
  const outlet = () => screen.getByTestId('workspace-outlet')

  /** The rail is the sized column: a settled width, and not the one that grows, at `lg`. */
  const expectRailIsSized = (className: string) => {
    // The class no longer names WHICH width — that's the element's own `--rail-w` style — so
    // this only asserts the rail is SIZED (not growing), never the number.
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
    // Every planning conversation — which is the one surface with no pane at all.
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
    // The two opening widths are 400px and 520px, and which is which is not arbitrary: a
    // conversation holds a transcript and a composer, the project's details do not. It only holds pre-drag — after
    // that, the citizen's own width replaces it everywhere ("drag it once, every project opens there").
    render(<Workspace chatSurface={<ChatSurface />} />)
    expect(outlet().style.getPropertyValue('--rail-w')).toBe('520px')
  })
})

describe('AppPaneHost — a hidden pane is genuinely inert, at shell level', () => {
  // ASSERTED HERE AND NOT IN `ProjectPage.test.tsx`, ON PURPOSE. That suite renders the project
  // page with no shell and stubs `LivePreview` to null, so its `queryByTestId('live-preview')`
  // assertions cannot observe anything the shell mounts and would stay green against a pane
  // leaking onto the project screen. Its assertions still pass; they are not evidence for this
  // unit.
  it('is in the document, out of the tab order, out of the accessibility tree, and offers nothing', async () => {
    render(<Workspace chatSurface={<ChatSurface />} />)
    fireEvent.click(screen.getByText('to project'))

    const wrapper = paneWrapper() as HTMLElement
    // THE LIVENESS HALF FIRST. Without it every assertion below passes against a host that
    // rendered nothing at all, which is the assert-absence false-green in its purest form.
    expect(wrapper.querySelector('iframe')).toBeTruthy()

    // `visibility:hidden` rather than `aria-hidden` alone: zero width and overflow:hidden clip a
    // subtree visually but leave its descendants in the tab order. Awaited because the pane draws
    // its departure first — `aria-hidden` is the half that lands immediately.
    expect(wrapper.getAttribute('aria-hidden')).toBe('true')
    await waitFor(() => expect(paneWrapper()?.className).toMatch(/invisible/))

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

describe('AppPaneHost — a layout change does not remount the frame', () => {
  it('flipping the shell\'s grid between side-by-side and stacked keeps the same iframe node', () => {
    // The class comes from a fixture here; the real threshold that produces it in the
    // product lives elsewhere. What is assertable NOW — and what makes the claim about this
    // suite's own element rather than an arbitrary wrapper — is that the container whose class
    // changes is the shell's grid, and the pane host is its sibling.
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
    // The host frames what already exists and never asks for an address, so a project screen with
    // nothing built costs nothing at all.
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
