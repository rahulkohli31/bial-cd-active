/**
 * U22 (R16/R17) — THE POLL ENDS ON A SETTLED ANSWER, AND NOTHING PINS "GONE".
 *
 * Two halves, and the second is the one with the teeth.
 *
 * The first is trivial: once the server has said `asleep` / `slot_taken` / `never_built`, the
 * 45-second timer can only ever hear the same sentence again, so it stops. `unknown` is
 * pointedly NOT one of those — it decided nothing, and a poll that stopped on it would pin
 * "we could not check" for the life of the tab.
 *
 * The second is the reason this file exists. The moment the timer stops re-asking, a verdict
 * is only as true as the last thing that invalidated it — and a RESTORED CONTAINER REUSES THE
 * SAME PREVIEW URL, byte for byte. The poll effect used to key on `[projectId,
 * framedPreviewUrl]` alone, so nothing would ever have re-run it, and "Your workspace is
 * asleep" would sit on screen over a running app until the user reloaded the page. Today the
 * 45-second tick self-corrects within one tick; this unit removes that corrective, so the
 * replacement is explicit and is asserted here.
 *
 * The naive replacement ("re-ask when the user sends a new prompt") is tested BECAUSE IT IS
 * NOT SUFFICIENT: it fires mid-provision, hears `alive=false` — truthfully, the container is
 * not up yet — and stops again, permanently. The workspace lifecycle, not the prompt, is the
 * invalidation signal.
 *
 * Also pinned here, on the same surface: the precedence between the poll's `restorable` and
 * the `projectHasSavedBuild` prop, which shipped in U17 with no test at all.
 *
 * CHAT-KIND MIGRATION (sfw-002). This page now renders ONLY a `build` chat, fixed at creation —
 * every composer send already holds the write toolset (BuilderPage.tsx's routing-rule docblock),
 * so there is no more plan-card confirm to drive a build through here. `handleBuildIt`'s card
 * still exists, but pressing it now creates a SECOND, different chat and navigates there — its
 * turn never touches this page, so it cannot be what `framedBuild` drives. An ordinary `send()`
 * is the trigger instead: the plain `readTurnStream` call it makes IS the open socket every
 * frame in this file is pushed into.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import {
  FakeEventSource, PREVIEW_URL, makeClient, primeClient, renderBuilder,
  waitForGateOpen, composer, T_WORKSPACE, T_PREVIEW,
} from './_builderSession.jsx'
import type { PreviewLifeState, PreviewState } from '../../utils/buildSessionApi'

/** BuilderPage's own cadence (`PREVIEW_PROBE_MS`), which it does not export. Mirrored, not
 *  imported, so a change to it is a deliberate edit here rather than a silently-passing test. */
const PROBE_MS = 45_000

const h = vi.hoisted(() => ({
  loadBuilds: vi.fn(), newBuild: vi.fn(), createBuild: vi.fn(), getBuild: vi.fn(),
  deleteBuild: vi.fn(), listProjectConversations: vi.fn(), buildUserParts: vi.fn(),
  startTurn: vi.fn(), readTurnStream: vi.fn(), buildFromPlan: vi.fn(),
  resolvePlanOptions: vi.fn(),
  start: vi.fn(), stop: vi.fn(), getStatus: vi.fn(), forceEnd: vi.fn(),
  relaunchPreview: vi.fn(),
  fetchPreviewState: vi.fn(), fetchSaveState: vi.fn(),
}))

vi.mock('../../utils/builderHistory', () => ({
  loadBuilds: h.loadBuilds, newBuild: h.newBuild, createBuild: h.createBuild,
  getBuild: h.getBuild, deleteBuild: h.deleteBuild, deriveTitle: (t: string) => (t || '').slice(0, 40),
}))
vi.mock('../../utils/conversationApi', () => ({ listProjectConversations: h.listProjectConversations }))
vi.mock('../../utils/chatHistory', () => ({ relativeTime: () => 'now' }))
vi.mock('../../components/layout/Navbar', () => ({ default: () => null }))
vi.mock('../../utils/attachmentStore', async (orig) => ({
  ...(await orig<typeof import('../../utils/attachmentStore')>()),
  buildUserParts: h.buildUserParts,
}))
// `switchMode` is GONE — a chat's kind is fixed at creation, so there is no per-thread setting
// left to switch. `resolvePlanOptions` is a real export, kept mocked only because
// the surface reaches for it when a plan offer is answered — never exercised here, since this
// suite never renders an offer.
vi.mock('../../utils/turnStreamApi', async (orig) => ({
  ...(await orig<typeof import('../../utils/turnStreamApi')>()),
  startTurn: (...a: unknown[]) => h.startTurn(...a),
  readTurnStream: (...a: unknown[]) => h.readTurnStream(...a),
  buildFromPlan: (...a: unknown[]) => h.buildFromPlan(...a),
  resolvePlanOptions: (...a: unknown[]) => h.resolvePlanOptions(...a),
}))
// The probe itself is the subject: it is counted, not stubbed away. `fetchSaveState` rides
// along only so the page's save-state effect cannot reach a real `fetch` under fake timers.
vi.mock('../../utils/buildSessionApi', async (orig) => ({
  ...(await orig<typeof import('../../utils/buildSessionApi')>()),
  fetchPreviewState: (...a: unknown[]) => h.fetchPreviewState(...a),
  fetchSaveState: (...a: unknown[]) => h.fetchSaveState(...a),
}))

function deps() {
  const fake = new FakeEventSource('x')
  return { client: makeClient(h), eventSourceFactory: () => fake }
}

/**
 * Script an ordinary send's own turn stream as an OPEN socket a test can push frames into by
 * hand. Not `_builderSession.jsx`'s `scriptBuildTurn` — that helper still branches on whether
 * `readTurnStream` was called WITH a `turnId`, which was how the old Build-it watch (subscribing
 * to a turn already known to be a build) told itself apart from an ordinary send (subscribing
 * with none, and getting back a streamed plan). That distinction is gone: `fireRelayTurn` never
 * passes a `turnId`, and never asks the chat's kind either — every send on this BUILD-chat page
 * opens the one plain subscription, and that IS the build.
 *
 * The opening snapshot mirrors what every real subscribe gets FIRST
 * (`backend/src/api/v1/conversations/turns.py`'s own docstring: "emit the first frame BEFORE any
 * model byte — the snapshot serves that role"), carrying the `turnId` this page reads into
 * `liveTurnIdRef` — the fact several of this file's assertions (Stop, the compile-probe gate)
 * depend on being true the moment a turn opens, not only once it ends.
 */
function scriptTurn(opening: unknown[] = [
  { type: 'snapshot', seq: 1, turnId: 't1', turnStatus: 'running', items: [], textSoFar: '', steps: [] },
  T_WORKSPACE(undefined, 2),
]) {
  const live: { emit: ((frame: unknown) => void) | null; close: ((outcome: string) => void) | null } = {
    emit: null, close: null,
  }
  const impl = async ({ onFrame }: { onFrame: (frame: unknown) => void }) => {
    live.emit = onFrame
    for (const frame of opening) onFrame(frame)
    return new Promise<string>((resolve) => { live.close = resolve })
  }
  return {
    impl,
    /** Push more frames into the open turn (wrapped in act, so effects flush between). */
    frame: async (...frames: unknown[]) => {
      await act(async () => { for (const frame of frames) live.emit?.(frame) })
    },
    /** Close the socket. The TRANSPORT outcome only; the frames decide the semantic one. */
    end: async (outcome = 'completed') => {
      await act(async () => { live.close?.(outcome); await Promise.resolve() })
    },
  }
}

/** Type into the composer and send — no plan, no card, no `Build it` press. */
async function send(text = 'a visitor app') {
  await waitForGateOpen()
  fireEvent.change(composer(), { target: { value: text } })
  fireEvent.keyDown(composer(), { key: 'Enter' })
}

/** A whole preview-state body, in the shape `fetchPreviewState` parses one into. */
const answer = (state: PreviewLifeState, restorable: boolean | null = null): PreviewState => ({
  state,
  alive: state === 'alive',
  previewUrl: state === 'alive' ? PREVIEW_URL : null,
  occupyingProjectName: null,
  occupyingProjectId: null,
  restorable,
})

const probeCount = () => h.fetchPreviewState.mock.calls.length
const goneCard = () => screen.queryByTestId('preview-unavailable-card')
const framedUrl = () => document.querySelector('iframe')?.getAttribute('src') ?? null

/** Let the in-flight probe's promise settle without moving the clock. */
const settle = () => act(async () => { await vi.advanceTimersByTimeAsync(0) })
/** Move the clock by whole poll cadences. */
const tick = (cadences = 1) =>
  act(async () => { await vi.advanceTimersByTimeAsync(PROBE_MS * cadences) })

/**
 * Render, drive a build until a live preview is framed, and hand back the open turn socket.
 *
 * Fake timers are armed BEFORE the preview frame, deliberately: that frame is what mounts the
 * poll's `setInterval`, and arming them afterwards would leave the interval on the real clock
 * where `advanceTimersByTime` could never reach it — the test would then "prove" the poll had
 * stopped when all it had done was look away.
 */
async function framedBuild(hasSavedBuild: boolean | null = null) {
  const turn = scriptTurn()
  h.readTurnStream.mockImplementation(turn.impl)
  renderBuilder({ deps: deps(), hasSavedBuild })
  await send()
  await waitFor(() => expect(h.readTurnStream).toHaveBeenCalled())
  vi.useFakeTimers()
  await turn.frame(T_PREVIEW())
  await settle()
  return turn
}

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  primeClient(h)
  h.relaunchPreview.mockResolvedValue({
    appId: 'a1', previewUrl: PREVIEW_URL, status: 'ready', restoredFromFailedBuild: false, ready: true,
  })
  h.newBuild.mockReturnValue('build-Y')
  h.createBuild.mockResolvedValue({ ok: true })
  h.getBuild.mockResolvedValue(null)
  h.loadBuilds.mockResolvedValue([])
  h.listProjectConversations.mockResolvedValue([
    { id: 'build-X', kind: 'build', title: 'My build', updatedAt: new Date().toISOString() },
  ])
  h.buildUserParts.mockImplementation(async (text: string) => [{ type: 'text', text }])
  h.startTurn.mockResolvedValue({ turnId: 't1' })
  h.fetchSaveState.mockResolvedValue({ appId: 'a1', dirty: false, savedHead: null, containerHead: null, recoveryAt: null })
  h.fetchPreviewState.mockResolvedValue(answer('alive'))
})
afterEach(() => {
  vi.useRealTimers()
  cleanup()
})

describe('BuilderPage — the preview poll stops on a terminal answer (R16)', () => {
  it('stops asking once the workspace is settled asleep', async () => {
    h.fetchPreviewState.mockResolvedValue(answer('asleep', true))
    await framedBuild()

    // The first probe fires on mount of the framed preview and settles the question.
    expect(probeCount()).toBe(1)
    expect(goneCard()?.getAttribute('data-preview-state')).toBe('asleep')

    // Six cadences — four and a half minutes of a tab left open — and not one more request.
    await tick(6)
    expect(probeCount()).toBe(1)
  })

  it.each<PreviewLifeState>(['slot_taken', 'never_built'])(
    'stops asking on a settled "%s" too — all three are facts, not faults',
    async (state) => {
      h.fetchPreviewState.mockResolvedValue(answer(state, false))
      await framedBuild()

      expect(probeCount()).toBe(1)
      await tick(4)
      expect(probeCount()).toBe(1)
    },
  )

  it('KEEPS asking after an "unknown" — a question nobody answered must not end the asking', async () => {
    // The mutation this pins: adding `unknown` to the settled set. It reads like a fourth
    // "not alive" state and it is not one — it is the ERROR arm, and stopping on it would
    // leave a tab that blinked once never checking again for the rest of its life.
    h.fetchPreviewState.mockResolvedValue(answer('unknown'))
    await framedBuild()

    expect(probeCount()).toBe(1)
    expect(goneCard()).toBeNull() // and it changes nothing on screen, either
    await tick(3)
    expect(probeCount()).toBe(4)
  })

  it('keeps asking while the container is alive', async () => {
    await framedBuild()

    await tick(2)
    expect(probeCount()).toBe(3)
    expect(framedUrl()).toBe(PREVIEW_URL)
  })

  // R3/U4 (Plan F) — RE-POINTED, NOT AN INERTNESS GUARD. This test's real subject is the POLL's
  // stopping rule, not the button: it pins that a settled-but-undecided `restorable: null` keeps
  // the timer running, and that it stops the moment the store gives a DEFINITE answer. That
  // precedence is exactly as testable without the button as with it — `hasSavedBuild`'s tri-state
  // copy on the card is still driven by the same resolved value the button used to gate on, so
  // the copy is the liveness half now instead of the button's presence.
  it('KEEPS asking when the workspace is settled but `restorable` decided nothing', async () => {
    // HALF AN ANSWER IS NOT A TERMINAL ANSWER. `asleep` is a settled fact about the container;
    // `restorable: null` is the tri-state's explicit "no claim", returned when the object store
    // could not be reached. Stopping here strands the builder on the one sentence this pane
    // must never say wrongly — "no saved build yet, so it will start fresh" — painted over a
    // workspace that is sitting safely on Blob, with no timer left to correct it.
    //
    // Mutation this pins: drop `&& state.restorable !== null` from the stopping rule. The poll
    // then settles on probe 1 and the recovery is never offered, while every other test here
    // stays green.
    h.fetchPreviewState.mockResolvedValue(answer('asleep', null))
    await framedBuild(false)

    // The worst screen this pane can render: the workspace is gone, the store was unreachable,
    // and the prop was a cold `false` — so the builder is told their work never existed.
    expect(screen.getByTestId('preview-unavailable-card').textContent).toContain('no saved build yet')
    expect(screen.queryByRole('button', { name: /bring it back|relaunch/i })).toBeNull()

    expect(probeCount()).toBe(1)
    await tick(3)
    expect(probeCount()).toBe(4)

    // ...and the moment the store answers, the poll settles — pinned by the copy flipping to the
    // confirmed-true reassurance ("Nothing is lost"), since the button that used to carry the
    // same claim is gone (R3: it moved to `StartAppControl`, which is not reachable from this
    // still-framed pane state — see the session report for that finding).
    h.fetchPreviewState.mockResolvedValue(answer('asleep', true))
    await tick(1)
    const settledAt = probeCount()
    expect(screen.getByTestId('preview-unavailable-card').textContent).toContain('Nothing is lost')
    await tick(3)
    expect(probeCount()).toBe(settledAt)
  })
})

describe('BuilderPage — stopping the poll must not pin "gone" (R17)', () => {
  it('re-arms on the workspace coming back, even though the restored preview URL is IDENTICAL', async () => {
    const turn = await framedBuild()
    expect(framedUrl()).toBe(PREVIEW_URL)

    // The container is reclaimed while the tab sits there. One tick hears it, and the poll
    // settles — this is the state the whole unit is about being able to LEAVE.
    h.fetchPreviewState.mockResolvedValue(answer('asleep', true))
    await tick()
    expect(goneCard()?.getAttribute('data-preview-state')).toBe('asleep')
    expect(framedUrl()).toBeNull()
    const settled = probeCount()

    // THE TRAP. The user sends a prompt; the workspace starts provisioning. Re-asking here is
    // right, but the answer is still "not serving" — the container genuinely is not up yet —
    // so a design that treated the prompt as its only trigger would stop again and stay
    // stopped, with the reclaimed card sitting over the app for the rest of the session.
    await turn.frame(T_WORKSPACE('preparing'))
    await settle()
    expect(probeCount()).toBe(settled + 1)
    expect(goneCard()).not.toBeNull() // still honestly gone, mid-provision

    // …and the workspace coming up is its own invalidation. The restore hands back the SAME
    // url, so `framedPreviewUrl` never changes and nothing here can be keyed on it.
    h.fetchPreviewState.mockResolvedValue(answer('alive'))
    await turn.frame(T_WORKSPACE('ready'))
    await settle()

    expect(goneCard()).toBeNull()
    expect(framedUrl()).toBe(PREVIEW_URL)
  })

  it('re-arms on a relaunch, which is the one restore that never goes through a turn frame', async () => {
    // `hasSavedBuild` comes back true from the poll, which is what puts "Bring it back" on the
    // reclaimed card at all (see the precedence block below).
    h.fetchPreviewState.mockResolvedValue(answer('asleep', true))
    await framedBuild()
    expect(probeCount()).toBe(1)

    const bringItBack = screen.getByRole('button', { name: /bring it back/i })
    h.fetchPreviewState.mockResolvedValue(answer('alive'))
    await act(async () => { fireEvent.click(bringItBack) })
    await settle()

    // The relaunch resolved with the identical url. Nothing about the FRAME changed, so the
    // only reason the pane is honest again is that the relaunch itself invalidated the verdict.
    expect(h.relaunchPreview).toHaveBeenCalled()
    expect(probeCount()).toBeGreaterThan(1)
    expect(goneCard()).toBeNull()
    expect(framedUrl()).toBe(PREVIEW_URL)
  })

  it('re-probes on focus — the backstop for a restore this tab could not see', async () => {
    // NEWLY LOAD-BEARING, and it was untested. The invalidation list covers what happens in THIS
    // tab: a turn frame, a relaunch. It cannot see a sibling tab restoring the same workspace,
    // and once the timer stops there is nothing else left to notice. These listeners are kept
    // alive after `stopAsking()` precisely for that case — they fire on a deliberate human act
    // (tabbing back), never on a clock, so they are bounded by the user rather than by us.
    //
    // The mutation this pins: moving listener registration inside `keepAsking()`. That is the
    // natural-looking simplification — it is where the timer lives — and it would silently
    // delete the only recovery path for the sibling-tab restore, with the suite green.
    h.fetchPreviewState.mockResolvedValue(answer('asleep', true))
    await framedBuild()
    expect(probeCount()).toBe(1)
    await tick(3)
    expect(probeCount()).toBe(1) // settled: the timer is genuinely stopped

    // Another tab brought the workspace back. Nothing in THIS tab knows.
    h.fetchPreviewState.mockResolvedValue(answer('alive'))
    await act(async () => { fireEvent.focus(window) })
    await settle()

    expect(probeCount()).toBe(2)
    expect(goneCard()).toBeNull()
    expect(framedUrl()).toBe(PREVIEW_URL)
  })

  it('the OLDER of two overlapping probes cannot overwrite the newer answer', async () => {
    // TABBING BACK FIRES TWO PROBES ON ONE GESTURE. `visibilitychange` and `focus` both land,
    // and the interval can already be mid-flight underneath them, so up to three requests are in
    // the air at once — all with `live === true`, settling in whatever order the network picks.
    // Whichever finishes LAST wrote `previewState`, which is the one thing this pane must never
    // get wrong: a stale `asleep` painted over a fresh `alive` tells somebody their workspace is
    // gone while it is running in front of them, and offers to "bring back" a container that
    // never left.
    //
    // Held open deliberately. Every other test here resolves both probes in the same microtask
    // flush, so the ordering window is invisible unless a test forces it — the same reason the
    // sibling test above holds one answer open.
    //
    // Mutation-check: delete the `generation !== latestProbe` guard and this goes red while the
    // whole rest of the file stays green.
    let releaseStale: (v: PreviewState) => void = () => {}
    h.fetchPreviewState.mockResolvedValue(answer('alive'))
    await framedBuild()

    // Probe A — the stale one. Starts first, answers last.
    h.fetchPreviewState.mockReturnValueOnce(
      new Promise<PreviewState>((resolve) => { releaseStale = resolve }),
    )
    await act(async () => { fireEvent.focus(window) })
    // Probe B — started after A, and it answers immediately.
    h.fetchPreviewState.mockResolvedValue(answer('alive'))
    await act(async () => { fireEvent.focus(window) })
    await settle()
    expect(goneCard()).toBeNull()

    // …and NOW the older request comes back carrying the reading it took before B ran.
    await act(async () => { releaseStale(answer('asleep', true)); await Promise.resolve() })
    await settle()

    expect(goneCard()).toBeNull()
    expect(framedUrl()).toBe(PREVIEW_URL)
  })

  it('drops the stale verdict AT the invalidation, not when the answer arrives', async () => {
    // `setPreviewState(null)` at the top of the poll effect. Deleting it leaves every other test
    // in this file green, because the mocked probe resolves in the same microtask flush — so the
    // window it closes is invisible unless a test holds the answer open on purpose. That window
    // is a full network round trip with the reclaimed card painted over an app that is coming
    // back up, which is R17's symptom narrowed rather than removed.
    h.fetchPreviewState.mockResolvedValue(answer('asleep', true))
    await framedBuild()
    expect(goneCard()).not.toBeNull()

    // The next probe never answers. Any drop of the card from here is the invalidation itself.
    h.fetchPreviewState.mockReturnValue(new Promise<PreviewState>(() => {}))
    const bringItBack = screen.getByRole('button', { name: /bring it back/i })
    await act(async () => { fireEvent.click(bringItBack) })

    expect(goneCard()).toBeNull()
  })

  it('shows no reclaimed card while a restore is in flight — a restore outranks a stale verdict', async () => {
    h.fetchPreviewState.mockResolvedValue(answer('asleep', true))
    await framedBuild()
    expect(goneCard()).not.toBeNull()

    // Hold the relaunch open, so the assertion lands DURING the restore rather than after it.
    let finish: (v: unknown) => void = () => {}
    h.relaunchPreview.mockImplementation(
      () => new Promise((resolve) => { finish = resolve }),
    )
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /bring it back/i })) })

    expect(goneCard()).toBeNull() // the wait is labelled, not contradicted
    // Two nodes carry it — the visible wait and the pane's persistent status region — which is
    // the U17 announcement discipline, not a duplicate.
    expect(screen.getAllByText(/restoring your app/i).length).toBeGreaterThan(0)

    h.fetchPreviewState.mockResolvedValue(answer('alive'))
    await act(async () => {
      finish({ appId: 'a1', previewUrl: PREVIEW_URL, status: 'ready', restoredFromFailedBuild: false, ready: true })
    })
    await settle()
    expect(goneCard()).toBeNull()
  })
})

describe('BuilderPage — `restorable` vs the `projectHasSavedBuild` prop (U17, previously untested)', () => {
  // The intended order, from U17's plan section: this session's own Save first (it can only
  // move the answer toward "yes"), then the POLL — the freshest server answer, and the only
  // one that counts the platform's turn-boundary recovery copy — then the prop, which was read
  // once when the route resolved and is never refetched. `??`, never `||`, because the poll's
  // `null` is "no claim" rather than "no": an unreachable store (or an alive container, where
  // the server does not spend the round trip) must fall through to the older-but-real reading
  // instead of retracting a claim the server once made confidently.

  // U4 (Plan F) — RE-POINTED, NOT AN INERTNESS GUARD. The precedence under test (poll overrides a
  // stale cold-load prop) is a claim about the CARD'S COPY, which `hasSavedBuild` still drives
  // identically to before — only the button that used to accompany the same claim is gone (R3).
  it('the poll can OFFER a restore the cold-load prop denied — the recovery copy the prop never saw', async () => {
    h.fetchPreviewState.mockResolvedValue(answer('asleep', true))
    await framedBuild(false)

    expect(screen.queryByRole('button', { name: /bring it back|relaunch/i })).toBeNull()
    expect(screen.getByTestId('preview-unavailable-card').textContent).toContain('Nothing is lost')
  })

  it('the poll can WITHDRAW one the prop claimed — a confirmed false is fresher than a stale true', async () => {
    h.fetchPreviewState.mockResolvedValue(answer('asleep', false))
    await framedBuild(true)

    expect(goneCard()).not.toBeNull()
    expect(screen.queryByRole('button', { name: /bring it back|relaunch/i })).toBeNull()
    expect(screen.getByTestId('preview-unavailable-card').textContent).toContain('no saved build yet')
  })

  it('a null from the poll claims NOTHING and leaves the prop standing', async () => {
    h.fetchPreviewState.mockResolvedValue(answer('asleep', null))
    await framedBuild(true)

    expect(screen.queryByRole('button', { name: /bring it back|relaunch/i })).toBeNull()
    // The copy is driven by the SAME resolved value the button used to gate on, so the prop's
    // confirmed `true` is what the card promises. `null` withdraws nothing — it only declines to
    // speak.
    expect(screen.getByTestId('preview-unavailable-card').textContent).toContain('Nothing is lost')
  })
})
