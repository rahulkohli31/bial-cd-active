/**
 * WHY THIS EXISTS: the pane host is the shell's sibling, not the Outlet's child, so a suite that
 * mounts the project page alone has no pane in its tree and stays green against a screen that
 * frames nothing (`AppPaneHost`'s mechanism).
 *
 * The project surface is the SECOND publisher on the workspace channel — two surfaces publishing
 * to one channel can retire each other's work on the hop between them. Every continuity assertion
 * here is paired with the round trip that would break it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route, Link } from 'react-router-dom'
import WorkspaceShell from '../WorkspaceShell'
import ProjectWorkspace from '../ProjectWorkspace'
import {
  useAppPaneVisible,
  usePublishAddress,
  usePublishHeading,
  usePublishPaneView,
  useWorkspaceProject,
  type PaneView,
} from '../workspaceChannel'
import type { ReactNode } from 'react'
import type { Project } from '../../../utils/projectApi'
import type { DeploymentView, PublishState } from '../../../utils/deployApi'
import { formatStamp } from '../../../utils/publishPresentation'

const api = vi.hoisted(() => ({
  fetchPreviewState: vi.fn(),
  fetchSaveState: vi.fn(),
  // THE COMPILE READ IS MOCKED AT THE WIRE, not at a hook, and the mock is the REQUEST LOG this
  // suite asserts on. "The screen must not cause a container call" is a claim about what was
  // ASKED — a scenario that only inspected rendered text would pass just as happily against a
  // screen that made the call and ignored the answer.
  fetchCompileState: vi.fn(),
  relaunchPreview: vi.fn(),
  saveProject: vi.fn(),
  listProjectConversations: vi.fn(),
  getDeployment: vi.fn(),
}))

vi.mock('../../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/buildSessionApi')>()),
  fetchPreviewState: api.fetchPreviewState,
  fetchSaveState: api.fetchSaveState,
  fetchCompileState: api.fetchCompileState,
  relaunchPreview: api.relaunchPreview,
  saveProject: api.saveProject,
}))
// THE PUBLISH READ IS PART OF THIS SCREEN, not a stub. The rail's APP STATUS panel holds
// one and the toolbar's chip holds another, and the LAST SAVED row this suite asserts about is a
// FIELD OF THIS RESPONSE — so it is mocked at the wire, where a count of the reads is meaningful,
// rather than at the hook, which is the seam the defect lived in.
vi.mock('../../../utils/deployApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/deployApi')>()),
  getDeployment: api.getDeployment,
}))
vi.mock('../../layout/Navbar', () => ({ default: () => <div data-testid="navbar" /> }))
vi.mock('../../projects/ProjectDescriptionEditor', () => ({
  default: () => <div data-testid="description-editor" />,
}))

const APP_URL = 'https://app-a.example.azurecontainerapps.io/'

const PROJECT: Project = {
  id: 'pA',
  name: 'VIP Movement',
  description: 'A tracked movement.',
  appId: 'app-1',
  appStatus: null,
  hasRelaunchableSnapshot: true,
  hasSavedSnapshot: null,
  isServing: false,
  createdAt: '2026-07-10T00:00:00Z',
  updatedAt: '2026-07-10T00:00:00Z',
  access: 'owner',
}

const preview = (over: Record<string, unknown> = {}) => ({
  state: 'never_built',
  alive: false,
  previewUrl: null,
  occupyingProjectName: null,
  occupyingProjectId: null,
  restorable: null,
  ...over,
})

/** The publish read's empty envelope, in whichever state the scenario is about. */
const deployment = (publishState: PublishState = 'draft', over: Partial<DeploymentView> = {}): DeploymentView => ({
  deploymentId: null,
  appId: 'app-1',
  status: null,
  step: null,
  url: null,
  headSha: null,
  failureCode: null,
  failureDetail: null,
  startedAt: null,
  finishedAt: null,
  unpublishedAt: null,
  approval: null,
  publishState,
  savedHead: null,
  savedAt: null,
  // `null` is "the server did not say", which keeps the saved row — the neutral default
  // for suites that are not about the never-saved omission.
  savedState: null,
  ...over,
})

const EMPTY_PANE: PaneView = {
  iterating: false, reconnecting: false,
  previewState: null, turnRunning: false,
  compileState: null, workspaceLost: false,
}

/**
 * A chat, publishing its own address — the OTHER publisher on this channel. `pane` is the one
 * thing the two kinds differ on: a build chat asks for the app to be seen, a plan chat does not.
 */
function ChatSurface({ projectId = 'pA', pane = true }: { projectId?: string; pane?: boolean }) {
  useWorkspaceProject(projectId)
  usePublishAddress({ url: APP_URL, status: 'ready', serving: true }, projectId)
  usePublishPaneView(EMPTY_PANE)
  useAppPaneVisible(pane)
  return <div data-testid="chat-surface" />
}

const noop = () => {}

/** The project surface, with every prop its owner would have loaded. */
function Surface({ project = PROJECT }: { project?: Project }) {
  return (
    <ProjectWorkspace
      project={project}
      onProjectUpdate={noop}
    />
  )
}

/** Both addresses under ONE shell, navigated by link exactly as the product navigates them. */
function Workspace({ entry = '/projects/pA', project = PROJECT }: { entry?: string; project?: Project }) {
  return (
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route
            path="/projects/:projectId"
            element={
              <>
                <Link to="/chat/c1">to chat</Link>
                <Link to="/plan/c2">to plan chat</Link>
                <Surface project={project} />
              </>
            }
          />
          <Route
            path="/chat/:chatId"
            element={
              <>
                <Link to="/projects/pA">to project</Link>
                <ChatSurface />
              </>
            }
          />
          <Route
            path="/plan/:chatId"
            element={
              <>
                <Link to="/projects/pA">to project</Link>
                <ChatSurface pane={false} />
              </>
            }
          />
        </Route>
      </Routes>
    </MemoryRouter>
  )
}

const frame = () => document.querySelector('iframe')

// THE ORIGIN THE PANE'S PROVENANCE GATE COMPARES AGAINST, derived exactly as `LivePreview` derives
// it (`new URL(previewUrl).origin`) rather than typed out by hand a second time — a copy that
// drifted from `APP_URL` would silently pass every vouch below through the wrong-origin arm the
// gate exists to close.
const SANDBOX_ORIGIN = new URL(APP_URL).origin

/**
 * The frame's own beacon, fired deliberately — a load alone no longer reveals. `LivePreview` only
 * ever sets `vouchedKey` off a `bial:app-mounted` message whose `origin` matches the preview
 * origin, whose `source` is this pane's own iframe window, AND whose `path` names the document
 * framed at `APP_URL` — the beacon is an identity claim now, not just a shape. `path` is derived
 * the same way the component derives its own framed path (`new URL(...).pathname`) rather than
 * hand-typed a second time, so a copy that drifted from `APP_URL` could not silently start
 * matching or failing to. A beacon missing `path` is not a beacon at all under
 * `isMountedBeaconFor` — it is forwarded to `onFrameMessage` and reveals nothing — which is the
 * mutant this guards: drop the `path` field here and every reveal assertion below goes red.
 * Dispatched inside `act()` because it drives a state update outside any `fireEvent` call.
 */
function vouch() {
  const iframe = frame()
  if (!iframe) throw new Error('vouch() called before the frame mounted')
  act(() => {
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: 'bial:app-mounted', path: new URL(APP_URL).pathname },
        origin: SANDBOX_ORIGIN,
        source: iframe.contentWindow,
      }),
    )
  })
}

/**
 * The pane's opaque full-bleed cover, found by what makes it one rather than by a test id — so a
 * refactor that stops covering fails here. Matched on either idle sentence, because WHICH one it
 * is telling is a separate question from WHETHER the frame is covered.
 *
 * ★ BOTH IDLE SENTENCES WERE REWRITTEN, AND THE REASON IS THIS CHANGE. They described the
 * WORKSPACE, which is not this pane's subject: "Getting your app ready…" is word for word the
 * sentence the workspace map says while nothing is serving, and "Your app isn't running right now"
 * is the claim the apps router's own error page makes — told from inside a pane that is mounted
 * only because the app IS up. On 2026-09-10 the two of them contradicted each other on screen. A
 * cover may describe the DOCUMENT in front of it and nothing else, so both now do.
 */
const COVER_SAYS_BROKEN = /The last change didn’t come together, so this page can’t open/i
const cover = () =>
  [...document.querySelectorAll('div')].find(
    (el) =>
      el.className.includes('absolute inset-0') &&
      (COVER_SAYS_BROKEN.test(el.textContent ?? '') ||
        /Putting this page together|Putting the latest change together/i.test(el.textContent ?? '')),
  )
// TWO DIFFERENT ELEMENTS, and the distinction is load-bearing. `app-pane-region` is `AppPane`'s
// own named region — always rendered, whether or not there is anything to frame, and where the
// skip control and the rail's collapse toggle live. `app-pane` is `AppPaneHost`'s frame wrapper,
// which exists only once an address resolved. A test that queries the second when it means the
// first reads "the pane is missing" for a project that simply has nothing built yet.
const paneRegion = () => screen.queryByTestId('app-pane-region')
const frameWrapper = () => screen.queryByTestId('app-pane')
const grid = () => screen.getByTestId('workspace-grid')
const rail = () => screen.getByTestId('workspace-outlet')

beforeEach(() => {
  vi.clearAllMocks()
  api.fetchPreviewState.mockResolvedValue(preview())
  api.fetchSaveState.mockResolvedValue({ appId: 'app-1', dirty: false, containerHead: null, savedHead: null })
  // The honest default for a signal nothing has reported: `unknown` holds whatever is showing and
  // asserts nothing. Never `'clean'` — an absent answer read as good news is the one behaviour the
  // whole four-valued type exists to forbid.
  api.fetchCompileState.mockResolvedValue('unknown')
  api.saveProject.mockResolvedValue({ appId: 'app-1', headSha: 'ccc' })
  api.getDeployment.mockResolvedValue(deployment())
})

afterEach(() => cleanup())

describe('loading a project address frames the running app, with no chat in the story', () => {
  it('★ frames the app on a direct project load, with no conversation ever mounted', async () => {
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)

    await waitFor(() => expect(frame()).toBeTruthy())
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
  })

  it('★ publishes a pane even for a project with NOTHING built, so the pane says so', async () => {
    // Two columns are the REST STATE of the project screen — nothing built shows the empty-state
    // sentence IN the pane, not a hidden pane the citizen has to interpret.
    api.fetchPreviewState.mockResolvedValue(preview({ state: 'never_built', restorable: false }))
    render(<Workspace project={{ ...PROJECT, appId: null, hasRelaunchableSnapshot: false }} />)

    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())
    expect(paneRegion()).toBeTruthy()
    expect(screen.getByTestId('app-pane-empty').textContent).toMatch(/describe what you want to build/i)
    // Counting forbids a second renderer — `getAllByText` alone would tolerate one.
    expect(screen.queryAllByText(/describe what you want to build/i)).toHaveLength(1)
    expect(frame()).toBeNull()
    expect(frameWrapper()).toBeNull()
  })

  it('★ a saved, not-running project offers the ONE start control, on the project screen', () => {
    // Cannot live in `ProjectPage.test.tsx`: that suite renders the page WITHOUT the shell, so
    // there is no pane in its tree and no assertion there would go red if the control disappeared.
    //
    // Mutation receipt: stop rendering `state.action` in `AppPane`'s no-frame arm and this goes red,
    // along with eight scenarios in `AppPane.test.tsx`.
    api.fetchPreviewState.mockResolvedValue(preview({ state: 'asleep', restorable: true }))
    render(<Workspace />)

    return waitFor(() => {
      expect(screen.getByRole('button', { name: /launch application/i })).toBeTruthy()
      // Exactly one: two controls would race the same endpoint.
      expect(screen.getAllByRole('button', { name: /launch application/i })).toHaveLength(1)
    })
  })

  it('frames NOTHING when the read says the workspace is asleep', async () => {
    // Only the `alive` state's `previewUrl` is framable — a pane framing the wrong thing is worse
    // than a pane framing nothing.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'asleep', restorable: true, previewUrl: APP_URL }),
    )
    render(<Workspace />)

    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())
    expect(frame()).toBeNull()
  })
})

describe('★ the project screen states no build outcome', () => {
  it('★ frames a running app and claims NOTHING about a build, on screen or in the region', async () => {
    // ★ THE DEFECT: this screen published `completedLive: true` unconditionally, and that flag drew
    // "Build complete — your app is live below". A route where a build can NEVER run therefore
    // stated a build outcome — on every cold load, and after every restore, where no build had
    // happened at all. Measured at t=691ms on ~20 loads with zero build-initiating requests.
    //
    // AND IT COULD NOT SIMPLY BE FLIPPED. Publishing `false` would have overridden the value the
    // pane host held across the chat→project hop and collapsed the iframe right after a successful
    // build — the regression `AppPaneHost`'s own docblock is written against. The flag is gone in
    // both directions instead: the claim left with the chip, and liveness moved onto the address,
    // where this screen READS it from the preview-state answer rather than asserting it.
    //
    // ASSERT-ABSENCE, PAIRED WITH LIVENESS: the app has to be framed in the same breath, or a
    // screen that rendered nothing would satisfy every absence below.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)

    await waitFor(() => expect(frame()).toBeTruthy())
    // THE FRAME'S OWN BEACON, FIRED DELIBERATELY — a load alone no longer reveals. Without it the
    // pane never REVEALS in jsdom, and the announcement arm this scenario is about is unreachable —
    // so every assertion below would pass against a screen that still made the claim. The absence
    // has to be measured in the state where the claim would be made.
    vouch()

    // LIVENESS: the app is on screen, revealed, at the address the read named.
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
    expect(document.querySelector('[data-testid="device-card"]')?.className).toMatch(/opacity-100/)
    // ABSENCE: no completion claim anywhere, and none announced either. The pane's live region is
    // permanent, so this reads it rather than testing for its existence.
    expect(screen.queryByText(/build complete/i)).toBeNull()
    expect(screen.queryByText(/your app is live below/i)).toBeNull()
    for (const region of screen.getAllByRole('status')) {
      expect(region.textContent).not.toMatch(/preview is live/i)
      expect(region.textContent).not.toMatch(/build complete/i)
    }
  })

  it('the claim is absent whether or not an app is serving', async () => {
    // The other half, and the reason the scenario above is not just "the chip moved". A project
    // with nothing serving never had a chip to lose, so a fix that only silenced the framed case
    // would pass there and leave the claim reachable the moment an app came up.
    api.fetchPreviewState.mockResolvedValue(preview({ state: 'asleep', restorable: true }))
    render(<Workspace />)

    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())
    // LIVENESS: the screen is up and saying something about the workspace…
    expect(await screen.findByRole('button', { name: /launch application/i })).toBeTruthy()
    // …and none of what it says is a build outcome.
    expect(screen.queryByText(/build complete/i)).toBeNull()
  })
})

describe('★ the compile verdict, gated on liveness', () => {
  const alive = () =>
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
  /** The pane's permanent live region, which is what the pane ANNOUNCES through. */
  const spoken = () => screen.getAllByRole('status').map((r) => r.textContent ?? '').join(' | ')

  it('★ THE COVER RE-CHECKS ON THE POLL, not once per visit to this screen', async () => {
    // DEFECT E2, AND THE ONE FIX ON THIS BRANCH THAT SHIPPED WITHOUT A TEST. The compile read was
    // keyed so it ran once per live workspace: a citizen watching a build sat under "Getting your
    // app ready…" until they navigated away and back, because nothing on this screen ever asked a
    // second time. `readTick` is the fix — the workspace poll now signals every completed read, so
    // the cover re-derives on the poll's own cadence.
    //
    // NAVIGATION IS DELIBERATELY NOT USED HERE. The sibling test above gets its second read by
    // going to chat and back, which REMOUNTS — and a remount re-reads whatever the keying is, so a
    // test written that way passes with the fix reverted. This one keeps the component mounted and
    // drives a second poll through the hook's own focus listener, which is the only shape that can
    // tell "re-reads on a tick" apart from "re-reads on a mount".
    //
    // Mutation check: drop `workspace.readTick` from the compile effect's dependency array in
    // ProjectWorkspace and this goes red — the second read never happens and the pane keeps
    // showing the stale verdict.
    const patient = { timeout: 5000 }
    alive()
    api.fetchCompileState.mockResolvedValue('unknown')
    render(<Workspace />)
    await waitFor(() => expect(api.fetchCompileState).toHaveBeenCalled(), patient)
    const readsBefore = api.fetchCompileState.mock.calls.length

    // The build finishes badly while the citizen is still sitting on this screen.
    api.fetchCompileState.mockResolvedValue('failed')
    fireEvent(window, new Event('focus'))

    await waitFor(
      () => expect(api.fetchCompileState.mock.calls.length).toBeGreaterThan(readsBefore),
      patient,
    )
    // …and the new verdict actually reaches the screen, which is the part a citizen experiences.
    await waitFor(
      () => expect(screen.getAllByText(COVER_SAYS_BROKEN).length).toBeGreaterThan(0),
      patient,
    )
    // LIVENESS: the same app is still framed at the same address, so this is one mounted pane
    // changing its mind — not a remount that would have re-read under any keying at all.
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
  })

  it('★ a build that failed to compile is NAMED, in the pane and in the live region', async () => {
    // WHAT THIS ADDS, AND WHAT IT DOES NOT. The sentence already existed — one plain sentence with
    // one route back into the chat, already selected when the verdict is `failed`. There is no
    // second sentence, on purpose: two ways of saying the same thing is how a product ends up
    // arguing with itself. What was missing is that the verdict could never REACH that state on
    // this screen, because the screen passed `compileState: null` and asked nothing.
    //
    // ANNOUNCED, not merely rendered. The citizen who most needs to be told their app is not
    // running is the one who cannot see the cover.
    alive()
    api.fetchCompileState.mockResolvedValue('failed')
    render(<Workspace />)

    await waitFor(() => expect(api.fetchCompileState).toHaveBeenCalledWith('pA'))
    // The sentence, and the one route back into the chat that comes with it.
    await waitFor(() => expect(screen.getAllByText(COVER_SAYS_BROKEN).length).toBeGreaterThan(0))
    expect(spoken()).toMatch(COVER_SAYS_BROKEN)
    expect(spoken()).toMatch(/send a message describing what you’d like/i)
  })

  it('★ an UNREADABLE verdict asserts nothing in either direction — "not failure" is never success', async () => {
    // An inherited rule, not rebuilt here: `unknown` is what the client answers for a refusal, an
    // unreadable body, a thrown request or a container image older than the signal, and it must
    // read as "no idea" — never as `clean`. The failure mode this rejects is the natural
    // implementation: treat anything that is not `failed` as fine, and republish the very claim
    // about a build outcome on exactly the reload where nothing had been verified.
    //
    // Mutation check: map the client's answer through `verdict === 'failed' ? 'failed' : 'clean'`
    // and this goes red — a mutant that reads plausible and is the whole point of the third value.
    alive()
    api.fetchCompileState.mockResolvedValue('unknown')
    render(<Workspace />)

    await waitFor(() => expect(frame()).toBeTruthy())
    await waitFor(() => expect(api.fetchCompileState).toHaveBeenCalled())
    // THE FRAME'S OWN BEACON, FIRED DELIBERATELY — a load alone no longer reveals. The reveal has
    // to actually happen, or the "no success claim" half below is unreachable and would pass
    // against a pane that made one.
    vouch()
    // NOTHING IS CLAIMED, in either direction: no failure sentence, and no success claim either.
    expect(screen.queryByText(COVER_SAYS_BROKEN)).toBeNull()
    expect(spoken()).not.toMatch(/preview is live/i)
    expect(screen.queryByText(/build complete/i)).toBeNull()
    // LIVENESS, PAIRED: the pane really did render its app, so the absences above are a refusal to
    // claim rather than a component that failed to draw.
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
  })

  it('★ an unreadable answer HOLDS a cover that is already up — it is never translated to clean', async () => {
    // ★ THE ONLY PLACE `unknown` AND `clean` DIVERGE, and therefore the only scenario that can
    // catch the natural mistake: mapping the client's answer through
    // `verdict === 'failed' ? 'failed' : 'clean'` on the way through. That reads plausible, keeps
    // every other assertion in this block green, and uncovers the frame over the exact error screen
    // the cover exists to hide.
    //
    // THE SEQUENCE IS THE REAL ONE. Read one says the build failed, so the cover goes up and names
    // it. The citizen steps into a chat and comes back — this surface remounts and reads again —
    // and this time the platform cannot tell (the HMR socket is down, or the container predates the
    // signal). Nothing has been learned, so nothing may change: the cover stays, because the app
    // behind it is still broken.
    //
    // Mutation receipt: apply that ternary and this is the ONLY scenario in the file that goes red.
    // GENEROUS TIMEOUTS THROUGHOUT, because this scenario is two mounts and two round trips deep and
    // the default one second is a measurement of the machine rather than of the behaviour.
    const patient = { timeout: 5000 }
    alive()
    api.fetchCompileState.mockResolvedValue('failed')
    render(<Workspace />)
    await waitFor(
      () => expect(screen.getAllByText(COVER_SAYS_BROKEN).length).toBeGreaterThan(0),
      patient,
    )

    // The second read cannot tell. The pane keeps the SAME app framed throughout, which is what
    // makes "hold" meaningful — a new app would legitimately reset the cover.
    api.fetchCompileState.mockResolvedValue('unknown')
    fireEvent.click(screen.getByText('to chat'))
    fireEvent.click(screen.getByText('to project'))
    await waitFor(() => expect(api.fetchCompileState.mock.calls.length).toBeGreaterThan(1), patient)

    // HELD: the frame is still COVERED. Asserted on the cover element rather than on its sentence,
    // because the wording legitimately follows the current verdict — `unknown` is not `failed`, so
    // the cover reverts to its neutral idle line. What must not change is that it is still there:
    // an unreadable answer taught the platform nothing, so it may not uncover an app it has been
    // told is broken. Under the mutant the cover is GONE and the framework's error screen is
    // showing through.
    await waitFor(() => expect(cover()).toBeTruthy(), patient)
    // LIVENESS: over a frame that is still mounted, at the same address — so this is a held cover
    // rather than a pane that lost its app.
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
  })

  it('a call that FAILS answers unreadable, and still nothing is claimed', async () => {
    // The client never throws — it swallows and answers `unknown` — and this pins that the screen
    // does not undo that by translating a failure into good news on the way through.
    alive()
    api.fetchCompileState.mockRejectedValue(new Error('network is down'))
    render(<Workspace />)

    await waitFor(() => expect(frame()).toBeTruthy())
    expect(screen.queryByText(COVER_SAYS_BROKEN)).toBeNull()
    expect(screen.queryByText(/build complete/i)).toBeNull()
  })

  it('★ the read is NOT ISSUED when the workspace is not alive — asserted on the request log', async () => {
    // The actual constraint here is narrower than it might sound: the screen must not START a
    // stopped container. The route already short-circuits before any attach when nothing is
    // live, and the read is gated on the same liveness the save read is — so a dark pane costs
    // nothing.
    //
    // ON THE REQUEST LOG, NOT ON RENDERED TEXT. A screen that made the call and ignored the answer
    // renders identically to one that never asked, so only the log can tell them apart.
    api.fetchPreviewState.mockResolvedValue(preview({ state: 'asleep', restorable: true }))
    render(<Workspace />)

    // LIVENESS: wait until the screen has genuinely settled on its answer, so "no call" is not just
    // "nothing has happened yet".
    expect(await screen.findByRole('button', { name: /launch application/i })).toBeTruthy()
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())
    expect(api.fetchCompileState).not.toHaveBeenCalled()
  })

  it('a compiled build says nothing, and reveals the app', async () => {
    // The success arm, stated for what it is: an affirmative `clean` is the only value that
    // uncovers, and uncovering is the whole of what it earns. It buys no sentence.
    alive()
    api.fetchCompileState.mockResolvedValue('clean')
    render(<Workspace />)

    await waitFor(() => expect(frame()).toBeTruthy())
    await waitFor(() => expect(api.fetchCompileState).toHaveBeenCalled())
    expect(screen.queryByText(COVER_SAYS_BROKEN)).toBeNull()
    expect(screen.queryByText(/build complete/i)).toBeNull()
  })
})

describe('the app survives the round trip, in BOTH directions', () => {
  it('project → chat → project keeps the SAME iframe node', async () => {
    // The direction the existing shell suite doesn't exercise: it starts from a chat. The return
    // trip is where a cold first commit can retire an address the departing surface left standing.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())
    const original = frame()

    fireEvent.click(screen.getByText('to chat'))
    expect(frame()).toBe(original)

    fireEvent.click(screen.getByText('to project'))
    await waitFor(() => expect(screen.getByTestId('description-editor')).toBeTruthy())
    expect(frame()).toBe(original)
    expect(frame()?.getAttribute('src')).toBe(APP_URL)
  })

  it('★ does not blank the pane while its own read is still in flight', async () => {
    // A naive publish of `{url: null}` on a cold remount would retire the address the chat left
    // standing; `usePublishAddress`'s abstain rule is what prevents it.
    let resolveRead: (value: unknown) => void = () => {}
    api.fetchPreviewState.mockImplementation(
      () => new Promise((resolve) => { resolveRead = resolve }),
    )
    render(<Workspace entry="/chat/c1" />)
    const original = frame()
    expect(original).toBeTruthy()

    fireEvent.click(screen.getByText('to project'))
    // The read has NOT resolved. The frame must still be standing.
    expect(frame()).toBe(original)

    resolveRead(preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }))
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())
    expect(frame()).toBe(original)
  })

  it('fires no start of its own on a remount', async () => {
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'asleep', restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())

    fireEvent.click(screen.getByText('to chat'))
    fireEvent.click(screen.getByText('to project'))
    await waitFor(() => expect(screen.getByTestId('description-editor')).toBeTruthy())

    expect(api.relaunchPreview).not.toHaveBeenCalled()
  })
})

describe('the stacked crossing is a class, not a remount', () => {
  it('expresses both layouts on ONE grid element, with no measurement anywhere', async () => {
    // No `matchMedia`, no `ResizeObserver`: the container carries both directions as responsive
    // classes, so the crossing cannot remount the frame — there is only ever one tree.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())

    expect(grid().className).toMatch(/flex-col/)
    expect(grid().className).toMatch(/wide:flex-row/)
  })

  it('gives the project rail the narrower of the two OPENING widths', async () => {
    render(<Workspace />)
    await waitFor(() => expect(screen.getByTestId('description-editor')).toBeTruthy())

    expect(rail().style.getPropertyValue('--rail-w')).toBe('400px')
    expect(rail().getAttribute('data-rail-mode')).toBe('details')
  })
})

describe('the collapse control — hidden, not unmounted, and never a one-way door', () => {
  it('★ lives in the TOOLBAR ROW, so it is still reachable once the rail is hidden', async () => {
    // A toggle placed inside the rail itself would vanish when collapsed (`w-0` and `invisible`
    // take it out of the tab order and the accessibility tree) — nothing short of a reload could
    // undo the press. The row survives both a collapse and the pane going away, so the control
    // has one home in every state.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())

    const toggle = screen.getByRole('button', { name: /hide details/i })
    expect(screen.getByTestId('workspace-toolbar').contains(toggle)).toBe(true)
    expect(paneRegion()?.contains(toggle)).toBe(false)
    expect(rail().contains(toggle)).toBe(false)

    fireEvent.click(toggle)
    // `\bw-0\b` would also match the `min-w-0` this element always carries — the boundary has to
    // be whitespace, not a word boundary.
    expect(rail().className).toMatch(/(^|\s)w-0(\s|$)/)
    expect(rail().className).toMatch(/invisible/)
    const back = screen.getByRole('button', { name: /show details/i })
    expect(back.getAttribute('aria-expanded')).toBe('false')
    expect(back.getAttribute('aria-controls')).toBe(rail().id)

    fireEvent.click(back)
    expect(rail().className).not.toMatch(/(^|\s)w-0(\s|$)/)
  })

  it('keeps the rail MOUNTED while collapsed, so nothing inside it is discarded', async () => {
    render(<Workspace />)
    await waitFor(() => expect(screen.getByTestId('description-editor')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /hide details/i }))

    // The subtree is still in the document — a draft and a scroll position survive the cycle.
    expect(screen.getByTestId('description-editor')).toBeTruthy()
    expect(rail().className).toMatch(/invisible/)
  })

  it('★ is reachable on a project with NOTHING BUILT, where there is no frame to hang it on', async () => {
    // The toggle can't live in the pane's toolbar slot: that toolbar is rendered by `LivePreview`,
    // which only mounts once there is something to frame, so a project with nothing built would
    // have NO toggle at all. Its home has to be a surface that always renders.
    api.fetchPreviewState.mockResolvedValue(preview({ state: 'never_built', restorable: false }))
    render(<Workspace project={{ ...PROJECT, appId: null, hasRelaunchableSnapshot: false }} />)
    await waitFor(() => expect(api.fetchPreviewState).toHaveBeenCalled())
    expect(frame()).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /hide details/i }))
    expect(rail().className).toMatch(/(^|\s)w-0(\s|$)/)
    fireEvent.click(screen.getByRole('button', { name: /show details/i }))
    expect(rail().className).not.toMatch(/(^|\s)w-0(\s|$)/)
  })

  it('leaves the frame alone across a collapse — it is a class change, not a remount', async () => {
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())
    const original = frame()

    fireEvent.click(screen.getByRole('button', { name: /hide details/i }))

    expect(frame()).toBe(original)
  })
})

describe('the channel is left as the next surface needs to find it', () => {
  it('clears the pane and its visibility on the way out, and keeps the address', async () => {
    // The channel's per-payload rules, exercised from a SECOND publisher — the table in
    // `workspaceChannel.ts` states them.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())
    const original = frame()

    fireEvent.click(screen.getByText('to chat'))

    // The chat surface publishes its own pane, so the frame survives on the address, not the pane.
    expect(frame()).toBe(original)
    expect(screen.getByTestId('chat-surface')).toBeTruthy()
  })
})

/**
 * WHAT THE SHELL DOES FOR A CHAT THAT WANTS NO PANE.
 *
 * The SURFACE half — that the panel fills the rail, that a plan chat centres its column, that the
 * board's footer line appears on one kind and not the other — is `ConversationSurface-panel.test.tsx`'s,
 * where the real conversation surface renders. What is only visible HERE, through the real shell,
 * is the relationship between the two columns: who gets the width, and whether the frame survives.
 */
describe('a chat that declares no pane', () => {
  it('★ takes the whole rail, and the frame stays mounted rather than being torn down', async () => {
    // The hide treatment, never an unmount: the same node throughout.
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())
    const original = frame()

    fireEvent.click(screen.getByText('to plan chat'))

    expect(rail().className).toMatch(/flex-1/)
    expect(rail().className).not.toMatch(/lg:w-\[520px\]/)
    expect(frameWrapper()).toBeTruthy()
    expect(frame()).toBe(original)
    // AWAITED, because the app is taken off the screen rather than snatched off it:
    // the column holds its size for one animation while the card slides out, and only then does
    // the hide treatment land. The frame identity above is the assertion that must hold throughout.
    await waitFor(() => expect(frameWrapper()?.className).toMatch(/invisible/))
    expect(frame()).toBe(original)
  })

  it('★ and the app does not reload on the way back either', async () => {
    api.fetchPreviewState.mockResolvedValue(
      preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
    )
    render(<Workspace />)
    await waitFor(() => expect(frame()).toBeTruthy())
    const original = frame()

    fireEvent.click(screen.getByText('to plan chat'))
    fireEvent.click(screen.getByText('to project'))

    expect(frame()).toBe(original)
  })
})

/**
 * ★ LAST SAVED TELLS THE TRUTH AFTER A SAVE.
 *
 * THE DEFECT. The rail's LAST SAVED row is drawn from `savedHead`/`savedAt`, which are fields of
 * the DEPLOYMENT read — and Save wrote a new bundle without telling that read anything. The row
 * went on naming the previous version, or "We could not tell" on a project that had never been
 * saved, on a screen whose own chip had just changed to "Saved". Nothing looked broken, which is
 * why it took a citizen reading the row to find it.
 *
 * WHY THESE RUN THROUGH THE SHELL. The row and the chip hold SEPARATE reads of the same endpoint
 * and the fix is the `bial:deployment-changed` nudge that reconciles both. A suite that mocked
 * `usePublishState` would be asserting against the very seam the defect lived in, and one that
 * rendered the panel alone could not see the chip agree.
 */
const SAVED_ONE = '11ab22cd33ef44ab55cd66ef77ab88cd99ef00ab'
const SAVED_TWO = '99ff88ee77dd66cc55bb44aa33998877665544ff'
const AT_ONE = '2026-09-05T10:15:00Z'
const AT_TWO = '2026-09-05T11:40:00Z'

/**
 * The route's own half, which `ProjectPage` performs. Only these scenarios need it: the toolbar
 * reads `heading.projectId` before it will mount the publish chip at all, so a harness without a
 * heading can never watch the chip and the rail's row agree.
 */
function WithHeading({ children }: { children: ReactNode }) {
  usePublishHeading({ projectId: PROJECT.id, projectName: PROJECT.name, chatTitle: null, chatKind: null })
  return <>{children}</>
}

function SavingWorkspace() {
  return (
    <MemoryRouter initialEntries={['/projects/pA']}>
      <Routes>
        <Route element={<WorkspaceShell />}>
          <Route
            path="/projects/:projectId"
            element={
              <WithHeading>
                <Surface />
              </WithHeading>
            }
          />
        </Route>
      </Routes>
    </MemoryRouter>
  )
}

/** Running, with work the saved bundle does not have — the only state in which Save is pressable. */
const dirtyAndAlive = () => {
  api.fetchPreviewState.mockResolvedValue(
    preview({ state: 'alive', alive: true, previewUrl: APP_URL, restorable: true }),
  )
  api.fetchSaveState.mockResolvedValue({ appId: 'app-1', dirty: true, containerHead: 'aaa', savedHead: 'bbb' })
}

const savedRow = () => screen.getByTestId('status-row-saved')
const pressSave = async () => fireEvent.click(await screen.findByTestId('save-project'))

describe('★ the LAST SAVED row after a save', () => {
  it('★ moves off "We could not tell" on the FIRST save a project ever has', async () => {
    // A project with nothing saved yet: both halves of the row are null, so it says so in words.
    dirtyAndAlive()
    render(<SavingWorkspace />)
    await waitFor(() => expect(screen.getByTestId('status-row-saved-unknown')).toBeTruthy())

    api.getDeployment.mockResolvedValue(deployment('draft', { savedHead: SAVED_ONE, savedAt: AT_ONE }))
    await pressSave()

    await waitFor(() => expect(savedRow().textContent).toContain('11ab22c'))
    expect(savedRow().textContent).toContain(formatStamp(AT_ONE))
    // The "cannot tell" rendering is a different element, not merely different text — its absence
    // is what says the row is now making a claim rather than declining to.
    expect(screen.queryByTestId('status-row-saved-unknown')).toBeNull()
  })

  it('★ names the NEW version on the second save, not the one before it', async () => {
    // THE ARM THAT LOOKS PLAUSIBLE WHILE STALE. A row that names a real commit and a real time
    // reads as correct from across the desk; only the value tells you it is the previous save's.
    // So this asserts what the row SAYS, and that what it said a moment ago is gone.
    dirtyAndAlive()
    api.getDeployment.mockResolvedValue(deployment('draft', { savedHead: SAVED_ONE, savedAt: AT_ONE }))
    render(<SavingWorkspace />)
    await waitFor(() => expect(savedRow().textContent).toContain('11ab22c'))

    api.getDeployment.mockResolvedValue(deployment('draft', { savedHead: SAVED_TWO, savedAt: AT_TWO }))
    await pressSave()

    await waitFor(() => expect(savedRow().textContent).toContain('99ff88e'))
    expect(savedRow().textContent).toContain(formatStamp(AT_TWO))
    expect(savedRow().textContent).not.toContain('11ab22c')
    expect(savedRow().textContent).not.toContain(formatStamp(AT_ONE))
  })

  it('★ keeps the row it already had when the re-read FAILS, rather than blanking the panel', async () => {
    // THE RULE NOBODY WROTE DOWN. `usePublishState` sets `loadError` on any failure and the
    // panel renders that branch FIRST — pill, every provenance row and the action all replaced by
    // one line — so a 500 on the read that follows a save would blank the whole section on a
    // screen that has just said "Saved". A stale row is worse than a fresh one and far better
    // than no panel; the first read is still allowed to report its own failure.
    //
    // MUTATION RECEIPT: delete `if (everRead.current) return` from the hook's catch — restoring
    // the blanking branch — and this goes red on the `status-row-saved` query, which finds
    // nothing because the panel has become the error line.
    dirtyAndAlive()
    api.getDeployment.mockResolvedValue(deployment('draft', { savedHead: SAVED_ONE, savedAt: AT_ONE }))
    render(<SavingWorkspace />)
    await waitFor(() => expect(savedRow().textContent).toContain('11ab22c'))

    api.getDeployment.mockRejectedValue(new Error('Failed to read the deployment'))
    await pressSave()

    await waitFor(() => expect(api.getDeployment).toHaveBeenCalledTimes(2))
    expect(savedRow().textContent).toContain('11ab22c')
    expect(screen.getByTestId('app-status-panel').getAttribute('data-publish-state')).toBe('draft')
    expect(screen.queryByTestId('status-recheck')).toBeNull()
    expect(screen.getByTestId('status-pill').textContent).toContain('Draft')
  })

  it('★ the toolbar chip and the rail row agree afterwards — one nudge reconciles both', async () => {
    // Both surfaces are mounted here: the rail is collapsed, which is the state the toolbar mounts
    // its chip in, and a collapsed rail is HIDDEN rather than unmounted so the panel keeps its own
    // read. Two reads of one endpoint is the arrangement the nudge exists for.
    dirtyAndAlive()
    api.getDeployment.mockResolvedValue(deployment('live_current', { savedHead: SAVED_ONE, savedAt: AT_ONE }))
    render(<SavingWorkspace />)
    await waitFor(() => expect(savedRow().textContent).toContain('11ab22c'))

    fireEvent.click(screen.getByRole('button', { name: /hide details/i }))
    await waitFor(() => expect(screen.getByTestId('publish-chip').textContent).toContain('Live'))

    // The save moves the app off `live_current`: what is live is now one version behind.
    api.getDeployment.mockResolvedValue(
      deployment('live_newer_work', { savedHead: SAVED_TWO, savedAt: AT_TWO }),
    )
    await pressSave()

    await waitFor(() =>
      expect(screen.getByTestId('publish-chip').getAttribute('data-publish-state')).toBe('live_newer_work'),
    )
    expect(screen.getByTestId('publish-chip').textContent).toContain('newer work saved')
    // …and the panel behind it says the same thing about the same app, off its own read.
    expect(screen.getByTestId('app-status-panel').getAttribute('data-publish-state')).toBe('live_newer_work')
    expect(screen.getByTestId('status-pill').textContent).toContain('newer work saved')
    expect(savedRow().textContent).toContain('99ff88e')
  })

  it('★ issues exactly ONE deployment read per save — the nudge must not stampede', async () => {
    // A window event with no origin is delivered to every listener, so the cost of getting this
    // wrong is a fan-out that grows with the surfaces on screen — or, if a refresh could raise a
    // nudge of its own, one that never settles. The rail is open here, so the panel is the only
    // publish read in the tree and the arithmetic is exact.
    dirtyAndAlive()
    render(<SavingWorkspace />)
    await waitFor(() => expect(api.getDeployment).toHaveBeenCalledTimes(1))

    await pressSave()
    await waitFor(() => expect(api.getDeployment).toHaveBeenCalledTimes(2))

    // A second save, which is also what proves the count above was not a settling race: a
    // stampede from the first press would have arrived by now and pushed this past three.
    await pressSave()
    await waitFor(() => expect(api.getDeployment).toHaveBeenCalledTimes(3))
    expect(api.saveProject).toHaveBeenCalledTimes(2)
  })
})
