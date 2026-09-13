/**
 * THE ACCELERATED PROBE ON THE CHAT SURFACE, AND WHAT IT IS NOT ALLOWED TO ACCELERATE.
 *
 * The chat surface polls the same preview state the project surface does, on the same cadence and
 * through the same `keepAsking`/`stopAsking` seam — so shortening the interval while a workspace is
 * `starting` lands here too, and the pane leaves "Getting your app ready." when the app is ready
 * rather than up to forty-five seconds later.
 *
 * BUT THIS TICK DOES MORE THAN THAT ONE. The same probe also reads the compile state and asks
 * whether the workspace has been taken, and BOTH ARE CONTAINER EXECS — the project surface's poll
 * makes neither (`useWorkspaceState`'s own docblock says why). So the acceleration is allowed to buy
 * the sentence and the frame with cheap reads and nothing else: an accelerated tick asks the preview
 * state, full stop, and the two container reads wait for the next background tick.
 *
 * That is not a cost optimisation dressed up as a rule. An accelerated window is open because this
 * surface is watching a workspace come up, so the container the two reads would reach has been alive
 * for seconds — still unpacking a snapshot, still booting a dev server. "Did the last build compile?"
 * has no formed answer yet, and "has somebody else taken this workspace?" is being asked about a
 * container we just watched start for this very project.
 *
 * THE MUTANT THIS FILE EXISTS FOR: drop `!accelerated` from either gate in the probe and the first
 * scenario below goes red.
 *
 * AND ONE COST THAT IS NOT A REQUEST. Ticking thirteen times as often also re-renders this surface
 * thirteen times as often, unless an answer that has not changed is allowed to keep its old object.
 * The second describe below owns that half of the bill and carries its own reasoning.
 *
 * WHY THE CADENCES ARE IMPORTED RATHER THAN MIRRORED, unlike `ConversationSurface-poll.test.tsx`.
 * That file mirrors `PREVIEW_PROBE_MS` so a change to it is a deliberate edit there; this file's
 * subject is the RELATIONSHIP between the two cadences, not either number, so mirroring both would
 * make a legitimate re-tuning look like a bug here instead of the deliberate edit it is.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { Profiler } from 'react'
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import {
  FakeEventSource, makeClient, primeClient, waitForGateOpen,
  T_STEP, T_WORKSPACE, T_PREVIEW, T_BUILD_END, T_DELTA, PREVIEW_URL, inWorkspace,
} from './_builderSession.jsx'
import { PREVIEW_PROBE_MS, STARTING_PROBE_LIMIT, STARTING_PROBE_MS } from '../../components/workspace/workspaceState'

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), getBuild: vi.fn(),
  listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(), stopTurn: vi.fn(),
  resolvePlanOptions: vi.fn(),
  relaunchPreview: vi.fn(), stop: vi.fn(), getStatus: vi.fn(),
  fetchPreviewState: vi.fn(), fetchCompileState: vi.fn(), fetchSaveState: vi.fn(),
  checkWorkspace: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, getBuild: h.getBuild, deriveTitle: (t) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({
  // The send path creates the chat before its first upload; stubbed so no network is reached.
  createConversation: async () => ({ id: 'conv-created' }),
  listProjectConversations: h.listProjectConversations,
}))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../components/AttachmentChips', () => ({ default: () => null }))
/** EVERY ADDRESS THE PANE WAS HANDED, IN ORDER. The frame's own `src` can only be sampled between
 *  acts, and the failure this file guards against — the app being unframed for one commit by an
 *  effect that tore itself down — lives inside one. Recorded as a prop, so nothing is coalesced
 *  away between the assertions. */
const framedSeen = []
vi.mock('../../components/LivePreview', () => ({
  default: (props) => {
    framedSeen.push(props.previewUrl ?? null)
    return null
  },
}))
vi.mock('../../utils/attachmentStore', async (orig) => ({
  ...(await orig()), buildUserParts: h.buildUserParts,
}))
vi.mock('../../utils/chatErrors', async (orig) => await orig())
// The probe and the two container reads are the subject: counted, not stubbed away.
vi.mock('../../utils/buildSessionApi', async (orig) => ({
  ...(await orig()),
  fetchPreviewState: (...a) => h.fetchPreviewState(...a),
  fetchCompileState: (...a) => h.fetchCompileState(...a),
  fetchSaveState: (...a) => h.fetchSaveState(...a),
  checkWorkspace: (...a) => h.checkWorkspace(...a),
  relaunchPreview: (...a) => h.relaunchPreview(...a),
}))
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig()),
  startTurn: (...a) => h.startTurn(...a),
  readTurnStream: (...a) => h.readTurnStream(...a),
  buildFromPlan: (...a) => h.buildFromPlan(...a),
  stopTurn: (...a) => h.stopTurn(...a),
  resolvePlanOptions: (...a) => h.resolvePlanOptions(...a),
}))

import ConversationSurface from '../../components/chat/ConversationSurface'

/** EVERY COMMIT THE SURFACE MADE, counted. A `Profiler` rather than a mocked child, because the
 *  subject is the WHOLE surface — message list, composer and toolbar — and no single child stands
 *  for all three: `RailOutlet` is memoised with no props, so nothing above the surface can inflate
 *  this, and nothing below it can hide a re-render from it either. */
let surfaceCommits = 0
const countSurfaceCommit = () => { surfaceCommits += 1 }

function renderThread(chatId = 'thread-1') {
  const fake = new FakeEventSource(chatId)
  const deps = { client: makeClient(h), eventSourceFactory: () => fake }
  return render(
    <MemoryRouter initialEntries={[`/chat/${chatId}`]}>
      <Routes>
        {inWorkspace(<Route path="/chat/:chatId" element={
          <Profiler id="surface" onRender={countSurfaceCommit}>
            <ConversationSurface projectId="p1" buildSessionDeps={deps} />
          </Profiler>
        } />)}
      </Routes>
    </MemoryRouter>,
  )
}

const composer = () => screen.getByPlaceholderText(/ask for another change/i)

/** The consolidating snapshot every real subscribe gets FIRST — it carries the turn id this page
 *  reads into `liveTurnIdRef`, which both container reads below are gated on. */
const T_SNAPSHOT = (turnId = 't1', seq = 1) => ({
  type: 'snapshot', seq, turnId, turnStatus: 'running', items: [], parts: [], working: false,
})

function scriptTurn(opening = [T_SNAPSHOT(), T_WORKSPACE(undefined, 2)]) {
  const live = { emit: null, close: null }
  const impl = async ({ onFrame }) => {
    live.emit = onFrame
    for (const frame of opening) onFrame(frame)
    return new Promise((resolve) => { live.close = resolve })
  }
  return {
    impl,
    frame: async (...frames) => {
      await act(async () => { for (const frame of frames) live.emit?.(frame) })
    },
    end: async (outcome = 'completed') => {
      await act(async () => { live.close?.(outcome); await Promise.resolve() })
    },
  }
}

const preview = (state) => ({
  state,
  alive: state === 'alive',
  previewUrl: state === 'alive' ? PREVIEW_URL : null,
  occupyingProjectName: null,
  occupyingProjectId: null,
  restorable: true,
})

/** The address the pane is holding right now — the last one it was handed. */
const framedUrl = () => framedSeen.at(-1) ?? null

/**
 * An address in hand, no live turn, a standing completion claim — and a workspace the server says
 * is STARTING. The claim is what gates `checkWorkspace` (it costs a container exec, so a project
 * that has never been told its app is finished has nothing to be wrong about); the absent live turn
 * gates both container reads; and `starting` is what opens the accelerated window. Nothing is
 * FRAMED yet — `alive` is the one state whose `previewUrl` the wire calls framable — but the pane
 * is holding the turn's address, which is what the third scenario watches for a blink.
 *
 * FAKE TIMERS ARE ARMED BEFORE THE PREVIEW FRAME, deliberately — the same reasoning
 * `ConversationSurface-poll.test.tsx` records. That frame is what re-arms the poll's interval, and
 * arming the clock afterwards would leave it on the real one, where `advanceTimersByTime` could
 * never reach it: the test would then "prove" a cadence by looking away from it.
 */
async function watchingAStart() {
  h.fetchPreviewState.mockResolvedValue(preview('starting'))
  const turn = scriptTurn()
  h.readTurnStream.mockImplementation(turn.impl)
  renderThread()
  await waitForGateOpen()
  fireEvent.change(composer(), { target: { value: 'a visitor app' } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
  await waitFor(() => expect(h.readTurnStream).toHaveBeenCalled())
  await turn.frame(T_STEP('Scaffolding your app…'))

  vi.useFakeTimers()
  await turn.frame(T_PREVIEW(), T_DELTA('Build complete — your app is live below.', 6), T_BUILD_END())
  await turn.end()
  // The turn has to be genuinely OVER, not merely closed: both container reads below are gated on
  // `liveTurnIdRef.current === null`, so a helper that left the turn live would prove their absence
  // by the wrong mechanism entirely. Well under one accelerated interval, so no tick rides on it.
  await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
  return turn
}

beforeEach(() => {
  framedSeen.length = 0
  surfaceCommits = 0
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([])
  h.buildUserParts.mockImplementation(async (text) => [{ type: 'text', text }])
  h.startTurn.mockResolvedValue({ turnId: 't1' })
  h.fetchCompileState.mockResolvedValue('clean')
  h.checkWorkspace.mockResolvedValue(false)
  h.fetchSaveState.mockResolvedValue({ appId: 'a1', dirty: false, containerHead: null, savedHead: null, recoveryAt: null })
})

afterEach(() => {
  vi.useRealTimers()
  cleanup()
})

describe('the chat surface asks faster while a workspace is starting', () => {
  it('hears the app come up within one accelerated read, and asks the container NOTHING to do it', async () => {
    await watchingAStart()
    h.fetchCompileState.mockClear()
    h.checkWorkspace.mockClear()
    const before = h.fetchPreviewState.mock.calls.length

    // The workspace serves, and the accelerated tick is the one that finds out.
    h.fetchPreviewState.mockResolvedValue(preview('alive'))
    await act(async () => { await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + 1) })

    // LIVENESS FIRST, so the two absences below are about a probe that ran rather than one that
    // never fired: three seconds bought a read, which at the background cadence it would not have.
    expect(h.fetchPreviewState.mock.calls.length).toBe(before + 1)
    expect(framedUrl()).toBe(PREVIEW_URL)

    // ABSENCE: neither container exec rode the accelerated tick.
    expect(h.fetchCompileState).not.toHaveBeenCalled()
    expect(h.checkWorkspace).not.toHaveBeenCalled()

    // AND THEY ARE NOT LOST. The next background tick asks both — within one accelerated interval
    // of when they would have been asked with no acceleration at all.
    await act(async () => { await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS) })
    expect(h.fetchCompileState).toHaveBeenCalledWith('p1')
    expect(h.checkWorkspace).toHaveBeenCalledWith('p1')
  })

  it('reverts to the background cadence once the workspace is not starting', async () => {
    await watchingAStart()
    h.fetchPreviewState.mockResolvedValue(preview('alive'))
    await act(async () => { await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + 1) })
    const settled = h.fetchPreviewState.mock.calls.length

    // Ten accelerated intervals over a running app add nothing — the whole product does not go on
    // a three-second poll because one workspace once started.
    await act(async () => { await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS * 10) })
    expect(h.fetchPreviewState.mock.calls.length).toBe(settled)

    // Quiet because it is slow, not because it is dead.
    await act(async () => { await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS) })
    expect(h.fetchPreviewState.mock.calls.length).toBe(settled + 1)
  })

  it('★ a start that goes dark is bounded too — a failed probe SPENDS from the window', async () => {
    // THE OTHER HALF OF THE BOUND. `fetchPreviewState` throws on any non-2xx and on a dropped
    // connection, and for as long as only the success path could advance `fastReads`, a workspace
    // that reached `starting` and then began erroring was probed every three seconds for the life
    // of the tab — twenty requests a minute, from the chat route as well as the project one, with
    // the 40-read ceiling that exists to stop a hung start never moving. `spendProbeCadence` in
    // the `catch` is the fix; this counts the reads it is supposed to stop.
    await watchingAStart()
    const before = h.fetchPreviewState.mock.calls.length
    h.fetchPreviewState.mockRejectedValue(new Error('500 from preview-state'))

    // Comfortably past the bound. The ceiling is on ELAPSED fast polling, so however the ticks
    // land against this window, no more than the bound may ride it.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS * (STARTING_PROBE_LIMIT + 2))
    })
    const spent = h.fetchPreviewState.mock.calls.length
    expect(spent).toBeGreaterThan(before) // liveness: the fast timer really was running
    expect(spent - before).toBeLessThanOrEqual(STARTING_PROBE_LIMIT)

    // AND THE FAST TIMER IS GONE. Ten more accelerated intervals of the same broken endpoint buy
    // nothing at all — this is the assertion the unfixed probe cannot pass.
    await act(async () => { await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS * 10) })
    expect(h.fetchPreviewState.mock.calls.length).toBe(spent)

    // NOTHING WAS RECLASSIFIED ON THE WAY. Forty failures say nothing about a container, so the
    // pane still says a start is happening — no "we could not check", no "gone", no retry verb.
    expect(screen.getByText('Getting your app ready.')).toBeTruthy()
    expect(screen.queryByText(/we could not check/i)).toBeNull()

    // ABSENCE PAIRED WITH LIVENESS: quiet because it is slow, not because it died.
    await act(async () => { await vi.advanceTimersByTimeAsync(PREVIEW_PROBE_MS) })
    expect(h.fetchPreviewState.mock.calls.length).toBeGreaterThan(spent)
  })

  it('changes cadence WITHOUT re-running the effect, so the pane never blinks', async () => {
    // THE MECHANISM, not just the symptom. The tempting implementation — put the polled state in
    // this effect's dependency list — reaches the running app too, and reaches it by tearing the
    // poll down and building it again on the very transition the feature exists to catch. Its
    // first statement is `setPolledPreview(null)`, and the framed address is a function
    // of that answer, so the mutation costs an extra request AND hands the pane a blank address in
    // the same commit that says the app is running. The reschedule happens inside the read for
    // exactly this reason, and both halves are asserted: the read count an effect gives itself
    // away by, and every address the pane was handed while it happened.
    await watchingAStart()
    expect(framedUrl()).toBe(PREVIEW_URL) // liveness: there is an address here to lose
    const reads = h.fetchPreviewState.mock.calls.length
    const commits = framedSeen.length

    h.fetchPreviewState.mockResolvedValue(preview('alive'))
    await act(async () => { await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + 1) })

    expect(h.fetchPreviewState.mock.calls.length).toBe(reads + 1)
    expect(framedSeen.slice(commits)).not.toContain(null)
    expect(framedUrl()).toBe(PREVIEW_URL)
  })
})

/**
 * THE OTHER HALF OF THE ACCELERATION'S BILL — the one nobody sends a request for.
 *
 * `fetchPreviewState` PARSES A FRESH OBJECT EVERY TICK, so a poll that records its answer
 * unconditionally hands this surface a new `polledPreview` identity three seconds apart forever,
 * and React re-renders the whole thing — transcript, composer, toolbar — for a reading nobody's
 * screen can tell apart from the one already up. `useWorkspaceState` has compared the FIELDS
 * before recording since it was written; the chat surface's copy of the same poll never did, and
 * accelerating the cadence turned that from one wasted render every forty-five seconds into one
 * every three, straight through the window a citizen sits watching their app come up.
 *
 * THE MUTANT THIS EXISTS FOR: collapse the recorder back to
 * `setPolledPreview((prev) => (state.state === 'unknown' && prev ? prev : { projectId, state }))`
 * and the first assertion below counts one commit per tick instead of none.
 *
 * WHY THE ANSWERS ARE FRESH OBJECTS AND NOT ONE SHARED ONE. A `mockResolvedValue` hands back the
 * same reference every call, which `samePreviewState`'s `a === b` would satisfy on its own — the
 * test would pass over a guard that only ever compares identities, which is precisely the guard
 * the wire cannot use. A new object per read is what the parser really does, so it is what this
 * asks the guard to survive.
 */
describe('an unchanged reading re-renders nothing', () => {
  it('four accelerated ticks saying the same thing cost zero renders — and a changed one still lands', async () => {
    await watchingAStart()
    // Fresh, field-identical objects from here on. See the docblock.
    h.fetchPreviewState.mockImplementation(async () => preview('starting'))

    // DRAIN THE ONE-SHOTS FIRST. The turn that just ended leaves `useBuildSession`'s "still
    // working" overlay on a 4s timer of its own (`ITERATION_QUIET_MS`), and its commit belongs to
    // that turn, not to the poll. Counting through it would measure the wrong thing — and would
    // pass a mutant by exactly the margin it hid.
    await act(async () => { await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS * 2 + 1) })
    const reads = h.fetchPreviewState.mock.calls.length
    surfaceCommits = 0

    // ONE TICK PER `act`, deliberately. Four ticks inside a single `act` are one React commit —
    // the batcher collapses them — so a poll re-rendering on every single tick would present
    // itself here as one render and slip through at four-to-one odds. Flushed one at a time, the
    // count is what it says it is.
    for (let tick = 0; tick < 4; tick += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + 1) })
    }

    // LIVENESS FIRST, so the zero below is a poll that ran four times and decided nothing had
    // changed — not a poll that quietly stopped, and not a surface that stopped rendering at all.
    expect(h.fetchPreviewState.mock.calls.length).toBe(reads + 4)
    expect(screen.getByText('Getting your app ready.')).toBeTruthy()
    expect(surfaceCommits).toBe(0)

    // AND THE SURFACE IS STILL LISTENING. A genuinely different answer re-renders it AND reaches
    // the screen: the wait sentence goes and the app is framed.
    h.fetchPreviewState.mockImplementation(async () => preview('alive'))
    await act(async () => { await vi.advanceTimersByTimeAsync(STARTING_PROBE_MS + 1) })

    expect(surfaceCommits).toBeGreaterThan(0)
    expect(screen.queryByText('Getting your app ready.')).toBeNull()
    expect(framedUrl()).toBe(PREVIEW_URL)
  })
})
