/**
 * ★ LivePreview AUTHORS NO WORKSPACE SENTENCE, AND THIS FILE IS WHERE THAT IS KEPT TRUE.
 *
 * WHAT THIS FILE USED TO BE. It drove the pane through four "gone" states — asleep, slot_taken,
 * never_built, unknown — and pinned a headline and a body for each: `GONE_TITLE` and `goneBody`,
 * four titles and six bodies, plus the `showUnavailable` and `showTerminal` cards that drew them.
 * Every one of those sentences already had an author. `workspace/workspaceState.ts` computes ONE
 * state for the whole workspace and `AppPane` draws it, so this component was the SECOND author of
 * every one — and two authors of one sentence is not a duplication, it is a contradiction waiting
 * for the composition nobody tested. On 2026-09-10 the composition arrived: "Your workspace is
 * asleep" was drawn over an app the map was at that moment calling up.
 *
 * ★ AND THE OLD TESTS WOULD NOT HAVE CAUGHT THE DELETION GOING WRONG. They pinned that copy against
 * this component IN ISOLATION, so every one of them stayed green through a deletion that left the
 * composed product with no sentence at all. This repo has that written down as a lesson —
 * assert-absence tests false-green — and it applies to a whole FILE just as it does to one
 * assertion. So the file is cut deliberately rather than trusted to fail, and what replaces it
 * asserts the two halves that are actually load-bearing now: that no workspace verdict is spoken
 * here, and that the covers which are NOT verdicts still render.
 *
 * THE OWNER CARVE-OUT, STATED BECAUSE IT IS THE EASIEST THING TO DELETE BY ACCIDENT. The
 * frame-stall card and the loading cover STAY. They are the only thing in the platform watching
 * the CITIZEN's own wire: the serving proof the `alive` reading now rests on is a loopback GET to
 * 127.0.0.1:3000 inside the container, while the citizen's browser reaches the same app through
 * the portal edge → the ACA ingress, and a 502 with an empty body lives in that gap. "The platform
 * watched it answer" and "this browser can fetch it" are two different facts, and these covers
 * observe the second. Every test below that asserts one of them asserts its PRESENCE — a test that
 * would still pass with the cover deleted is not doing its job.
 *
 * ★ AND THE REVEAL ITSELF MOVED ONTO THAT WIRE, WHICH IS WHY THIS SUITE NO LONGER FIRES `load` TO
 * SHOW AN APP. `load` fires for that bodyless 502 exactly as it does for a page, so it proves a
 * response arrived and nothing more; a timer proves only that time passed. The one witness on the
 * citizen's side of the network is the framed document, and the template's platform-owned
 * `instrumentation-client.ts` posts `bial:app-mounted` once its root layout has rendered. Every
 * reveal below goes through `vouch()`, and the tests that still fire `load` fire it to prove the
 * OPPOSITE — that a load alone leaves the labelled wait exactly where it was.
 *
 * These tests drive the component through the SAME parser the browser uses (`fetchPreviewState`),
 * so a backend that stops sending `state`, or a parser that starts coercing it, fails here rather
 * than in production. The wire values themselves are pinned in
 * `backend/tests/api/v1/build_sessions/test_preview_state.py`.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, render, cleanup, fireEvent, screen } from '@testing-library/react'
import LivePreview from '../LivePreview'
import { fetchPreviewState } from '../../utils/buildSessionApi'
import type { PreviewState } from '../../utils/buildSessionApi'

afterEach(cleanup)

const SANDBOX_URL = 'https://app-xyz.example.azurecontainerapps.io/'
// The origin half of the pane's provenance gate. A beacon that does not carry it is a stranger's
// word about somebody else's document, and this pane's whole reveal now rests on the beacon.
const SANDBOX_ORIGIN = 'https://app-xyz.example.azurecontainerapps.io'

// THE PANE'S TIMERS AND BOUNDS, mirrored here so a test that DRIVES the wait says what it is
// driving instead of advancing a magic number. Every one of them is pinned against the component's
// own constants by the structural test at the bottom of this file, so a value that drifts on one
// side goes red on the other rather than quietly turning a timing test into a test of nothing.
//
// ★ AND THE SEQUENCE THEY SPELL OUT, because no single one of them is the behaviour: ASKING COMES
// BEFORE RE-REQUESTING. A silent document is pinged `PINGS_BEFORE_RELOAD` times, re-requested only
// then, and re-requested at most `VOUCH_RETRY_LIMIT` times — so the stall card arrives at frame key
// `#3.0`, with two pings spent on that last document too. A re-request tears down the very
// hydration that produces the beacon, which is why the cheap question is always asked first.
//
// ★ AND THE TWO THAT SERVE A DOCUMENT THAT ANSWERS. `ALIVE_PINGS_BEFORE_STALL` is how many times a
// document that replies "painting" is asked AGAIN rather than fetched again — an alive page is
// never torn down. `HEARTBEAT_MS` is the slow ask a REVEALED frame still gets: a page can go blank
// after it painted, with no `load` for the pane to see, so the reveal is re-earned on a clock
// rather than trusted forever.
const FRAME_LOAD_CAP_MS = 20_000
const VOUCH_AFTER_LOAD_MS = 5_000
const PINGS_BEFORE_RELOAD = 2
const ALIVE_PINGS_BEFORE_STALL = 12
const VOUCH_RETRY_LIMIT = 3
const HEARTBEAT_MS = 15_000
const BUILDING_COVER_MAX_MS = 30_000

/**
 * The frame this pane rendered. Throws rather than returning null for the same reason
 * `deviceCard` does — an `?.` here would let a whole test run over a pane that framed nothing.
 */
function frameOf(container: HTMLElement): HTMLIFrameElement {
  const frame = container.querySelector('iframe')
  if (!frame) throw new Error('frameOf(): the pane is framing nothing')
  return frame
}

/**
 * An inbound `message` as the browser delivers it, with the two facts the pane's provenance gate
 * reads spelled out at the call site: WHERE the bytes came from (`origin`) and WHICH window sent
 * them (`source`). Both halves are parameters because both halves are refusable, and a helper
 * that could only ever produce the passing combination would prove nothing about the gate.
 */
function postToPane(source: Window | null, data: unknown, origin: string = SANDBOX_ORIGIN) {
  act(() => {
    window.dispatchEvent(new MessageEvent('message', { data, origin, source }))
  })
}

/**
 * ★ THE BEACON — the framed document vouching for ITSELF, which is the only thing that reveals
 * this pane now.
 *
 * It passes BOTH halves of the provenance gate: the sandbox origin, and the window of the frame
 * this pane actually rendered. `load` is deliberately not part of it — a bodyless 502 from the
 * ingress fires `load` exactly as a page does, which is how a citizen came to watch a blank white
 * rectangle presented as their app on 2026-09-10.
 *
 * ★ AND IT CARRIES A `path`, WHICH IS THE HALF THAT SURVIVES ONE HOSTNAME FOR EVERY APP. BIAL
 * refused a wildcard certificate, so the origin says only “an app” and the window says only “the
 * frame I rendered” — a frame that navigated itself to a DIFFERENT app is still that frame. The
 * path is what says “the app at this address”, so a message without one is not a beacon at all:
 * it is forwarded to `onFrameMessage` like any other message and reveals nothing.
 */
function vouch(container: HTMLElement, origin: string = SANDBOX_ORIGIN, path: string = '/') {
  // Not ceremony: a `vouch` over a pane that is framing nothing would dispatch a message no
  // listener could match, and every expectation after it would read as a behaviour rather than as
  // a test that set nothing up — `frameOf` throws instead.
  postToPane(frameOf(container).contentWindow, { type: 'bial:app-mounted', path }, origin)
}

/**
 * The device card, which carries the reveal in two places: the `opacity` class the citizen sees
 * and the `data-revealed` attribute a probe can read. Throws rather than returning null — an `?.`
 * here would let every reveal assertion below pass over a pane that rendered no card at all,
 * which is this repo's own false-green rule applied to the one fact this file is about.
 */
function deviceCard(container: HTMLElement): HTMLElement {
  const el = container.querySelector<HTMLElement>('[data-testid="device-card"]')
  if (!el) throw new Error('deviceCard(): the pane is framing nothing, so nothing can be revealed')
  return el
}

/** The React `key` written where a test can read it — the seam that makes a re-request a fact
 *  about the document rather than a fact about React's internals. */
function frameKeyOf(container: HTMLElement): string | null {
  return frameOf(container).getAttribute('data-frame-key')
}

/**
 * ONE expiry of the pane's vouch wait.
 *
 * ONE `act` PER EXPIRY, DELIBERATELY. Every expiry ends in a state update — a ping bumps the
 * re-arm counter, a re-request bumps the auto reload nonce — and the next timer is not armed until
 * that update has rendered, which `act` only flushes when it RETURNS. So advancing the whole four
 * minutes in a single call fires exactly ONE timer and the other eleven expiries never happen at
 * all. A loop is the only shape that drives this to its end.
 *
 * The cap is the LONGER of the two waits, so one expiry fires whichever of them is armed.
 */
function tickVouchWait() {
  act(() => {
    vi.advanceTimersByTime(FRAME_LOAD_CAP_MS + 1)
  })
}

/**
 * ★ ASKING COMES BEFORE RE-REQUESTING: spend the `PINGS_BEFORE_RELOAD` expiries the document
 * currently in the frame is entitled to, and prove each one really was a PING into that document
 * rather than a timer that expired and did nothing.
 *
 * THE SPY GOES ON THE CURRENT FRAME'S WINDOW, re-taken every time this is called, because a
 * re-request replaces the iframe element and with it the window a ping can land in. A spy captured
 * once and reused across a remount goes quiet for a reason that has nothing to do with the pane —
 * which is this repo's own false-green rule, wearing a mock.
 */
function spendPingsOf(container: HTMLElement) {
  const win = frameOf(container).contentWindow
  if (!win) throw new Error('spendPingsOf(): the framed window is gone, so nothing can be asked')
  const asked = vi.spyOn(win, 'postMessage').mockImplementation(() => {})
  try {
    for (let i = 1; i <= PINGS_BEFORE_RELOAD; i += 1) {
      const key = frameKeyOf(container)
      tickVouchWait()
      expect(asked, `expiry ${i} did not ask the document to vouch`).toHaveBeenCalledTimes(i)
      expect(asked).toHaveBeenLastCalledWith({ type: 'bial:ping' }, SANDBOX_ORIGIN)
      // A ping is an ASK, never a re-request: the document being asked to answer has to still be
      // in the frame when it does. Mutation check: reorder the wait's branches so the nonce bumps
      // before the pings are spent, and this goes red on the very first expiry.
      expect(frameKeyOf(container), 'a ping tore down the document it was asking').toBe(key)
    }
  } finally {
    asked.mockRestore()
  }
}

/**
 * Drive the vouch wait through `remounts` whole key cycles — two pings into the document that is
 * in the frame, then the expiry that gives up on it and re-requests the address.
 *
 * The re-request is asserted on `data-frame-key`, which is the React `key` written where a test
 * can read it: a timer that expired without bumping the nonce leaves the key where it was, and
 * that is a pane waiting out a document it never asked for again.
 */
function spendVouchWaits(container: HTMLElement, remounts: number) {
  for (let i = 1; i <= remounts; i += 1) {
    spendPingsOf(container)
    tickVouchWait()
    expect(frameKeyOf(container), `expiry ${i} re-requested nothing`).toBe(`${SANDBOX_URL}#${i}.0`)
  }
}

/** Put a real server body through the real client parser — no hand-built props. */
async function asTheBrowserSeesIt(body: unknown): Promise<PreviewState> {
  const res = { ok: true, json: async () => body } as unknown as Response
  return await fetchPreviewState('proj-1', { fetchImpl: async () => res })
}

/**
 * Matches of `re` that a SIGHTED citizen can see — i.e. everything outside the permanent sr-only
 * live region. That region speaks in every state, so an unfiltered `getByText` here would be
 * satisfied by the announcement alone and prove nothing about the screen.
 */
function seenNotJustSaid(re: RegExp) {
  const spoken = screen.getByRole('status')
  return screen.getAllByText(re).filter((el) => !spoken.contains(el))
}

/**
 * The pane, wired from a parsed server verdict exactly as the host wires it.
 *
 * TWO PROPS ARE GONE FROM THIS HELPER and their absence is the change: `occupyingProjectName` and
 * `hasSavedBuild` existed only to fill in a sentence about the WORKSPACE ("Baggage Reconciliation
 * is using your build workspace", "your saved app is still there"), and the map owns every one of
 * those now. They are not accepted props any more, so a test reaching for one is a compile error
 * rather than a value going quietly nowhere.
 */
function paneFor(state: PreviewState, extra: Record<string, unknown> = {}) {
  return render(
    <LivePreview
      previewUrl={SANDBOX_URL}
      status="ended"
      serving
      previewState={state.state}
      {...extra}
    />,
  )
}

/** The four sentences this component used to write about somebody else's subject. */
const RETIRED_WORKSPACE_COPY = [
  /workspace is asleep/i,
  /nothing is lost/i,
  /another project has your workspace/i,
  /using your build workspace/i,
  /nothing has been built here yet/i,
  /preview unavailable/i,
  /no longer running/i,
  /could not check on your preview/i,
  /start fresh/i,
]

describe('★ this pane speaks for the FRAME, and for nothing else', () => {
  it.each(['asleep', 'slot_taken', 'never_built', 'unknown'] as const)(
    '★ writes no headline, no body and no button for a `%s` workspace',
    async (state) => {
      // Each of these used to pick a title and a body out of this file's own copy table. The map
      // says all four now, on a board `AppPane` draws — and this pane is not even mounted for
      // three of them, because the frame veto refuses every reading but `running`.
      const verdict = await asTheBrowserSeesIt({
        state,
        alive: false,
        previewUrl: null,
        restorable: true,
      })
      const { container } = paneFor(verdict)

      for (const retired of RETIRED_WORKSPACE_COPY) {
        expect(container.textContent ?? '', `${state} still says ${retired}`).not.toMatch(retired)
      }
      // ★ LIVENESS, AND IT IS THE WHOLE POINT OF PAIRING IT. Every assertion above passes just as
      // happily on a component that threw and rendered nothing at all — which is exactly the
      // false-green this repo has written down. The pane really mounted, really has its permanent
      // region, and really is framing the app it was handed.
      expect(screen.getByRole('status').getAttribute('aria-live')).toBe('polite')
      expect(container.querySelector('iframe')).toBeTruthy()
    },
  )

  it('★ and offers no start control under any of its retired labels', async () => {
    // `RelaunchAffordance` and its four render sites are gone. Exactly ONE control starts the app —
    // `workspace/StartAppControl.tsx`, drawn by `AppPane` from the one computed state, whose action
    // union contains no destructive verb. The four placeholder buttons said the same thing five
    // times over, each in the vocabulary the client replaced ("preview" is the developer's word).
    const verdict = await asTheBrowserSeesIt({ state: 'asleep', alive: false, restorable: true })
    const { container } = paneFor(verdict)

    for (const label of [/bring it back/i, /relaunch/i, /launch application/i, /try again/i]) {
      expect(screen.queryByRole('button', { name: label })).toBeNull()
    }
    // LIVENESS: the pane rendered and framed. The affordance's new home is asserted where it
    // lives — `AppPane.test.tsx` pins that every no-frame state still offers a reachable way to
    // start the app, which is the half that would otherwise go missing silently.
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('★ no `role="alert"` survives here at all — a workspace verdict is not this pane`s emergency', async () => {
    for (const state of ['asleep', 'slot_taken', 'never_built', 'unknown'] as const) {
      const verdict = await asTheBrowserSeesIt({ state, alive: false, restorable: true })
      const view = paneFor(verdict)
      expect(screen.queryByRole('alert'), state).toBeNull()
      // LIVENESS beside each absence, per this repo's own rule.
      expect(view.container.querySelector('iframe'), state).toBeTruthy()
      view.unmount()
    }
  })
})

describe('★ `starting` is the pane`s own last word on never framing a container that is not answering', () => {
  it('★ withholds the frame AND puts a visible wait in its place', async () => {
    // A container the platform is still bringing up answers 502 at its own edge, and the apps
    // router turns a 502 into the "This app isn't running right now" page. Framed, that page is
    // shown to a citizen whose app is being started for them — the opposite of the truth, told at
    // the one moment they are watching. It was reported from production as a black panel over a
    // running build, and measured again on 2026-09-10.
    //
    // IT SURVIVES THE VETO THAT MAKES IT UNREACHABLE, ON PURPOSE. `AppPane` mounts this component
    // if and only if the workspace reading is `running`, so a `starting` reading should never get
    // this far. "Should never" is exactly the claim that was true of those eight seconds, and this
    // refusal costs one boolean.
    const verdict = await asTheBrowserSeesIt({
      state: 'starting',
      alive: false,
      previewUrl: null,
      restorable: null,
    })
    expect(verdict.state).toBe('starting')

    const { container } = paneFor(verdict)

    // Mutation check: drop `starting` from `showFrame`'s guard and this goes red with an iframe.
    expect(container.querySelector('iframe')).toBeNull()
    // ★ TAKING THE FRAME AWAY IS ONLY HALF A STATE, and the first version of this shipped only
    // that half: frame withheld, nothing in its place, an EMPTY RECTANGLE with the sentence
    // reaching screen-reader users and nobody else. The liveness assertion has to be the VISIBLE
    // one, because the sr-only region is mounted permanently and speaks in every state — asserting
    // on it was true over a pane drawing literally nothing.
    //
    // Mutation check: drop `starting` from `showLoading` and this goes red.
    expect(seenNotJustSaid(/opening your app/i)).toHaveLength(1)
    expect(screen.getByRole('status').textContent).toMatch(/opening your app/i)
  })

  it('★ and says nothing about the workspace while it waits', async () => {
    const verdict = await asTheBrowserSeesIt({ state: 'starting', alive: false, restorable: null })
    const { container } = paneFor(verdict, { serving: false })

    for (const retired of RETIRED_WORKSPACE_COPY) {
      expect(container.textContent ?? '').not.toMatch(retired)
    }
    // LIVENESS: the wait is on screen, so the silence above is a withheld verdict rather than a
    // pane that rendered nothing.
    expect(seenNotJustSaid(/opening your app/i)).toHaveLength(1)
  })
})

/**
 * ★ THE OWNER CARVE-OUT — the covers that are NOT verdicts, and that therefore STAY.
 *
 * Their rule is that they may describe the document in front of them and nothing else. A test in
 * this block that would still pass with its cover deleted is not doing its job, so every one of
 * them asserts the cover's PRESENCE, on screen, outside the sr-only region.
 */
describe('★ the frame-stall card and the loading cover STAY — they watch the citizen`s own wire', () => {
  it('★ the loading cover holds the screen from "no URL yet" to the framed document`s own beacon', () => {
    // It used to be destroyed the instant `previewUrl` arrived, which is precisely when the 5-7s
    // first-route compile begins: the spinner vanished and left an unlabelled blank white card at
    // the exact moment the citizen had been told their app was ready.
    const { container, rerender } = render(<LivePreview previewUrl={null} status="provisioning" />)
    expect(seenNotJustSaid(/setting up your sandbox/i)).toHaveLength(1)

    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    // The URL arrived and the frame is mounted, but nothing has vouched for what is inside it —
    // the third wait, which needs a line of its own because "Building your app" is stale by then
    // and silence is a blank card.
    expect(seenNotJustSaid(/opening your app/i)).toHaveLength(1)
    expect(container.querySelector('iframe')).toBeTruthy()
    // And the frame is MOUNTED but not revealed: an iframe that never mounts never loads, and a
    // document that never loads can never post the beacon that reveals it.
    expect(deviceCard(container).className).toMatch(/opacity-0/)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')

    // ★ THE LOAD IS NOT THE REVEAL ANY MORE, AND THIS IS WHERE THAT IS KEPT TRUE. A bodyless 502
    // at the ingress fires `load` exactly as a page does — measured on 2026-09-10, with the public
    // address answering 502 with zero bytes while the control plane had already stamped the app
    // served. Mutation check: put `frameLoaded` back into `revealed` and this pair goes red, with
    // the wait gone and an empty document faded in as the citizen's app.
    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    expect(seenNotJustSaid(/opening your app/i)).toHaveLength(1)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')

    // …and then the document itself says its root layout rendered, which is the one thing that
    // ends this wait.
    vouch(container)
    expect(screen.queryAllByText(/opening your app/i)).toHaveLength(0)
    expect(deviceCard(container).className).toMatch(/opacity-100/)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
  })

  it('★ the frame-stall card renders when the asking and the re-requests both run out, and says so in words', () => {
    // ★ THE CARD THIS BLOCK EXISTS FOR. It is bounded degradation, not a verdict: the frame stays
    // MOUNTED underneath, so a beacon that lands after the re-requests ran out still wins and
    // reveals — which is why the sentence says "slow", never "dead". Unmounting the frame would
    // make the stall permanent by construction, because the beacon it waits for could never come.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      // Before the budget runs out it is the ordinary wait, so the card below is a state change
      // rather than something that was always on screen.
      expect(seenNotJustSaid(/opening your app/i)).toHaveLength(1)
      expect(screen.queryAllByText(/taking longer than usual to open/i)).toHaveLength(0)

      // ★ THE STALL IS EARNED, NOT WAITED OUT, AND IT IS EARNED IN THE CONTRACT'S OWN ORDER: each
      // document is ASKED twice and only then re-requested. The 502 gap this mechanism exists for
      // closes on its own once the ingress catches up, so asking again is the honest answer and
      // giving up on the first silence is not — and a re-request would tear down the very
      // hydration that produces the beacon. `spendVouchWaits` asserts both halves as it goes.
      spendVouchWaits(container, VOUCH_RETRY_LIMIT)

      // Mutation check: drop the `vouchRetriesRef.current < VOUCH_RETRY_LIMIT` guard (so the first
      // expiry stalls) or lower the bound, and this pair goes red — the card would already be up
      // while the pane still had re-requests owed to the citizen.
      expect(seenNotJustSaid(/opening your app/i)).toHaveLength(1)
      expect(screen.queryAllByText(/taking longer than usual to open/i)).toHaveLength(0)
      // …and the three re-requests really happened, counted on the one seam that can see a remount
      // from outside. A timer that expired without bumping the nonce leaves this at `#0.0`, which
      // is a pane waiting out a document it never asked for again.
      expect(frameKeyOf(container)).toBe(`${SANDBOX_URL}#${VOUCH_RETRY_LIMIT}.0`)

      // ★ AND THE LAST DOCUMENT IS ASKED TWICE TOO, which is the half a "spend the whole budget"
      // loop would quietly skip. The budget that ran out is the RE-REQUEST budget; a document that
      // would have answered a ping is not made to pay for its predecessors' silence.
      spendPingsOf(container)
      expect(screen.queryAllByText(/taking longer than usual to open/i)).toHaveLength(0)

      tickVouchWait()

      expect(seenNotJustSaid(/taking longer than usual to open/i)).toHaveLength(1)
      expect(screen.getByRole('status').textContent).toMatch(/taking longer than usual to open/i)
      // THE COPY NAMES NO CONTROL THIS CARD DOES NOT HAVE — the one start control lives in
      // `AppPane`, and an instruction pointing at nothing is worse than no instruction.
      expect(seenNotJustSaid(/it will appear here the moment it loads/i)).toHaveLength(1)
      // ★ THE FRAME IS STILL THERE. This is the assertion that makes the card bounded degradation
      // rather than a fifth workspace verdict, and it is the one that would go red if somebody
      // "simplified" the card into a replacement for the frame.
      expect(container.querySelector('iframe')).toBeTruthy()
      // …pointed at the SAME address it always was: giving up spends no thirteenth re-request.
      expect(frameKeyOf(container)).toBe(`${SANDBOX_URL}#${VOUCH_RETRY_LIMIT}.0`)
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ tells the caller when the wait is labelled slow, and again when a late beacon takes it down', () => {
    // A stalled frame is the only sign this pane gets of an app whose dev server has stopped, so the
    // caller has to hear it — and has to hear it END, or a tab would go on asking the server about
    // an app the citizen is now looking at. Mutation check: announce once per frame key instead of
    // on the edge, and the `false` below never arrives.
    vi.useFakeTimers()
    try {
      const onStallChange = vi.fn()
      const { container } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ready" onStallChange={onStallChange} />,
      )
      spendVouchWaits(container, VOUCH_RETRY_LIMIT)
      spendPingsOf(container)
      // A slow document is not a stall until the budget runs out, so nothing has been said yet.
      expect(onStallChange).not.toHaveBeenCalled()

      tickVouchWait()
      expect(seenNotJustSaid(/taking longer than usual to open/i)).toHaveLength(1)
      expect(onStallChange.mock.calls).toEqual([[true]])

      vouch(container)
      expect(onStallChange.mock.calls).toEqual([[true], [false]])
    } finally {
      vi.useRealTimers()
    }
  })

  it('says nothing about a frame that vouches in time', () => {
    const onStallChange = vi.fn()
    const { container } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" onStallChange={onStallChange} />,
    )

    vouch(container)

    // LIVENESS: the frame really was revealed, so the silence is an answer rather than a crash.
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
    expect(onStallChange).not.toHaveBeenCalled()
  })

  it('★ a beacon that lands AFTER the stall still wins — the card says slow, never dead', () => {
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      // The full sequence, in the contract's order: three documents each asked twice and then
      // re-requested, and a fourth asked twice with nothing left to spend on it.
      spendVouchWaits(container, VOUCH_RETRY_LIMIT)
      spendPingsOf(container)
      tickVouchWait()
      expect(seenNotJustSaid(/taking longer than usual to open/i)).toHaveLength(1)

      // The container was slow, not dead: the first route compile finished and the template's
      // instrumentation posted the beacon on its own. Mutation check: unmount the frame under the
      // stall card and this line is unreachable by construction — `vouch` throws, because there is
      // no document left to speak for itself.
      vouch(container)

      expect(screen.queryAllByText(/taking longer than usual to open/i)).toHaveLength(0)
      expect(deviceCard(container).className).toMatch(/opacity-100/)
      expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ the reconnecting cover renders, and it is NO LONGER CAPPED', () => {
    // THE BOUND MOVED TO THE SERVER RATHER THAN VANISHING. A 20-second cap used to collapse this
    // cover into the "preview unavailable" card — one of the four workspace verdicts this file has
    // stopped authoring — so an expiry now has nowhere honest to go: an empty rectangle says
    // nothing, and re-mounting the frame over a dev server that is genuinely down frames the apps
    // router's error page, which is the exact defect this change exists to end. A crash that never
    // recovers clears the SERVING STAMP on the server, the reading stops being `running`, and
    // `AppPane` unmounts this pane and draws the one card.
    vi.useFakeTimers()
    try {
      const { container } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ended" serving reconnecting previewState="alive" />,
      )
      expect(seenNotJustSaid(/reconnecting to your preview/i)).toHaveLength(1)

      act(() => { vi.advanceTimersByTime(120_000) })

      // STILL THE COVER, two minutes later. What this pane owes that citizen is not a verdict — it
      // is to keep saying, honestly, that it is still waiting.
      expect(seenNotJustSaid(/reconnecting to your preview/i)).toHaveLength(1)
      expect(container.textContent ?? '').not.toMatch(/preview unavailable/i)
      // And the dead frame is replaced rather than shown: the cover IS the pane while it is up.
      expect(container.querySelector('iframe')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ the compile cover still holds over a frame, and it describes the PAGE, not the workspace', () => {
    // IDLE_BUSY_TEXT used to read "Getting your app ready…", which is word for word the sentence
    // the workspace map says while nothing is serving, and IDLE_BROKEN_TEXT used to open "Your app
    // isn't running right now" — the same claim the apps router's own error page makes, told from
    // inside a pane that exists only because the app is up. Two authors, one sentence; on
    // 2026-09-10 the two of them contradicted each other on screen.
    const { container, rerender } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" serving compileState="building" turnRunning />,
    )
    expect(seenNotJustSaid(/putting the latest change together/i)).toHaveLength(1)

    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" serving compileState="building" />)
    expect(seenNotJustSaid(/putting this page together/i)).toHaveLength(1)
    expect(container.textContent ?? '').not.toMatch(/getting your app ready/i)

    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" serving compileState="failed" />)
    expect(seenNotJustSaid(/this page can’t open/i)).toHaveLength(1)
    expect(container.textContent ?? '').not.toMatch(/isn’t running/i)
    // LIVENESS across all three: the frame is under the cover the whole time, which is what makes
    // these covers rather than states.
    expect(container.querySelector('iframe')).toBeTruthy()
  })
})

/**
 * ★ THE PROVENANCE GATE, WHICH IS NOW THE WHOLE OF THE PANE'S TRUST IN ANYTHING.
 *
 * The reveal rests on one inbound message, so the two questions the listener asks about it —
 * WHERE did these bytes come from, and WHICH window sent them — are the reveal's only defence. The
 * origin half stopped discriminating on its own the day BIAL refused a wildcard certificate: every
 * generated app is served from ONE hostname now, so an origin comparison proves "an app" and never
 * "the app I am framing". The reachable sender is not an unrelated tab (the portal severs
 * `window.opener` everywhere it opens an app) — it is another frame in this very document, whose
 * origin is identical to this pane's.
 */
describe('★ a beacon is trusted only from this pane`s own frame, at this pane`s own origin', () => {
  it('★ a stranger`s origin and another window`s beacon both reveal nothing — and this frame`s own then does', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)

    // (a) THE RIGHT SHAPE, THE RIGHT WINDOW, THE WRONG ORIGIN. Mutation check: drop the
    // `e.origin !== previewOriginRef.current` guard and this reveals the citizen's pane on the
    // word of any document on the internet that knows the message shape.
    vouch(container, 'https://not-your-app.example.test')
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')

    // (b) THE RIGHT SHAPE, THE RIGHT ORIGIN, ANOTHER WINDOW ON THE SAME HOST — a sibling frame in
    // this same document, which is exactly what one hostname for every app makes reachable.
    // Mutation check: drop the `e.source !== frameWindow` half of the gate and this goes red, with
    // one app's mount revealing a different app's pane.
    const sibling = document.createElement('iframe')
    document.body.appendChild(sibling)
    try {
      // A REAL SECOND WINDOW, ASSERTED. Posting from a null source would be refused by the same
      // line for a different reason (`e.source !== frameWindow` is true of null too), so this test
      // would pass while proving nothing about a sibling app at all.
      const siblingWindow = sibling.contentWindow
      if (!siblingWindow) throw new Error('the sibling frame has no window of its own to post from')
      postToPane(siblingWindow, { type: 'bial:app-mounted', path: '/' })
      expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
      // …and the pane is still visibly WAITING, not merely unrevealed: two refusals that left an
      // empty rectangle behind would be a worse answer than the one being refused.
      expect(seenNotJustSaid(/opening your app/i)).toHaveLength(1)
    } finally {
      sibling.remove()
    }

    // ★ LIVENESS, AND IT IS WHAT MAKES BOTH REFUSALS MEAN ANYTHING. The identical message from
    // this pane's OWN frame reveals. Without this line the two absences above are equally true of
    // a listener nobody attached, a beacon shape that quietly changed, or a pane that threw.
    vouch(container)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
  })

  it('★ the beacon is this pane`s evidence, not a report — it never reaches `onFrameMessage`, and an ordinary message still does', () => {
    // The two halves of one rule. The beacon is CONSUMED here: it is what this pane knows, not
    // news for the client-error relay, which feeds the harness's health verdict — forwarding it
    // would report every app's own successful mount as something to triage. Everything else that
    // clears the gate is still the relay's, untouched.
    const onFrameMessage = vi.fn()
    const { container } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" onFrameMessage={onFrameMessage} />,
    )

    // (a) ★ A `bial:app-mounted` WITH NO `path` IS NOT A BEACON AT ALL, which is the whole of the
    // revision-2 delta. The origin says "an app" and the window says "the frame I rendered"; the
    // path is the only half that says "the app at THIS address", so a message missing it proves
    // nothing and is treated as any other message — forwarded, and revealing nothing.
    //
    // Mutation check: drop `typeof data.path !== 'string'` from `isMountedBeaconFor` and this pair
    // goes red — the pane reveals on a message that never named which app rendered, and the relay
    // is robbed of a message it was owed.
    const pathless = { type: 'bial:app-mounted' }
    postToPane(frameOf(container).contentWindow, pathless)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    expect(onFrameMessage).toHaveBeenCalledWith(pathless)

    // (b) the same message WITH its path is the beacon, and it goes no further than this pane.
    onFrameMessage.mockClear()
    vouch(container)

    // Mutation check: drop the `return` after the beacon is recorded and this goes red.
    expect(onFrameMessage).not.toHaveBeenCalled()
    // ★ LIVENESS ON BOTH SIDES OF THAT ABSENCE, because it needs both: the beacon really landed
    // (it revealed the pane), and the seam really is wired (case (a) above and case (c) below both
    // arrive). Either one alone leaves "was not called" true of a message that never got past the
    // gate at all.
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')

    // (c) and everything else that clears the gate is still the relay's, byte for byte: this is
    // the arm of self-heal that turns a browser crash into a not-green health verdict.
    const clientError = { type: 'bial:client-error', message: 'ReferenceError: x is not defined' }
    postToPane(frameOf(container).contentWindow, clientError)

    expect(onFrameMessage).toHaveBeenCalledTimes(1)
    expect(onFrameMessage).toHaveBeenCalledWith(clientError)
  })
})

describe('★ what the wait does once a document has vouched — and what a second `load` takes back', () => {
  it('★ a frame that has vouched is asked again once per heartbeat, and never torn down for it', () => {
    // ★ WHY A REVEALED PAGE IS STILL ASKED AT ALL. It can go blank AFTER it painted — a client-side
    // route to nothing — and no `load` fires for the pane to see, so a reveal held forever on one
    // beacon is a claim nobody re-checks. The heartbeat is that re-check: a slow ask, which a page
    // that has gone blank answers with `bial:app-painting`, and the listener takes the reveal back.
    //
    // ★ AND WHAT MUST NEVER HAPPEN IS THE OTHER HALF, WHICH THE WORKING CASE PAYS FOR. The wait's
    // ordinary branches either ping the framed document or TEAR IT DOWN and re-fetch it, so a
    // vouched frame falling through to them would take a citizen's working app away from them —
    // scroll position, form state and the HMR socket with it. Asked: yes. Re-requested: never.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      const frame = frameOf(container)
      const win = frame.contentWindow
      if (!win) throw new Error('the pane framed no window to ask')
      const asked = vi.spyOn(win, 'postMessage').mockImplementation(() => {})

      // The load's own ask — and this suite's LIVENESS FOR THE SPY, since every count below is a
      // DELTA on it: a spy that could never see a ping would satisfy the "not yet" half of each
      // window just as happily.
      fireEvent.load(frame)
      expect(asked).toHaveBeenCalledTimes(1)

      vouch(container)
      const keyWhenVouched = frameKeyOf(container)

      // THREE WHOLE WINDOWS, each asserted in two halves — silence until the beat is due, then
      // EXACTLY one ask — because "one ping per 15s" is two claims, and a single count taken after
      // three windows is satisfied by a burst of three inside the first one.
      for (let beat = 1; beat <= 3; beat += 1) {
        act(() => {
          vi.advanceTimersByTime(HEARTBEAT_MS - 1)
        })
        // ★ THE MUTANT THIS HALF KILLS: arm the heartbeat off `VOUCH_AFTER_LOAD_MS` — the wait's
        // own number, three lines down in the same effect — and the vouched document is asked
        // three times per window instead of once, a ping storm into a page that is behaving.
        expect(asked, 'the heartbeat asked before it was due').toHaveBeenCalledTimes(beat)

        act(() => {
          vi.advanceTimersByTime(2)
        })
        // ★ AND THE MUTANT THIS ONE KILLS: keep revision 3's bare `if (frameVouched) return` — no
        // timer at all once a document has vouched — and the count never moves again, so a page
        // that went blank after it painted stays revealed for the rest of the session. That is
        // the reveal nobody re-checks, which is what the heartbeat exists to end.
        expect(asked, 'the heartbeat did not ask').toHaveBeenCalledTimes(beat + 1)
        expect(asked).toHaveBeenLastCalledWith({ type: 'bial:ping' }, SANDBOX_ORIGIN)
        // ★ AND THE MUTANT THE KEY KILLS: drop the vouched branch entirely so a revealed frame
        // falls through to the ordinary wait, and this goes red by the third beat — two pings and
        // then a re-request, the citizen's working app fetched again underneath them.
        expect(frameKeyOf(container), 'the heartbeat tore the document down').toBe(keyWhenVouched)
      }

      // LIVENESS, PAIRED WITH THE KEY ASSERTION ABOVE: the app is still on screen, in the same
      // element, so all of that asking is a question put to a REVEALED document rather than a pane
      // that quietly went back to waiting. The retraction belongs to the listener — an answer of
      // `bial:app-painting` — and nothing answers here, so the reveal stands through every beat.
      expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
      expect(frameOf(container)).toBe(frame)
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ a second `load` at the same key takes the reveal back at once, and the new document has to earn it', () => {
    // A page that reloaded itself from the INSIDE — which a dev-server restart does routinely, and
    // which this pane sees as nothing but a second `load` on the same element. The moment a
    // bodyless 502 lands in a frame that HAD vouched is exactly this one, and a vouch left standing
    // through it is the whole 2026-09-10 defect wearing a fresh document.
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    const frame = frameOf(container)

    fireEvent.load(frame)
    vouch(container)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
    const keyWhenVouched = frameKeyOf(container)

    // Mutation check: drop the `reloaded` arm of `onFrameLoad`, or read `loadedKey` from the render
    // instead of the ref, and this goes red — the stale vouch reveals whatever just replaced the
    // app. NO TIMER IS ADVANCED HERE, deliberately: the retraction is immediate, and a fix that
    // waited for the next expiry would leave the wrong document faded in for five seconds.
    fireEvent.load(frame)

    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    expect(deviceCard(container).className).toMatch(/opacity-0/)
    // …and it is a RETRACTION, not a re-request: the same element is still in the pane, being
    // asked again rather than torn down. A remount here would cost the citizen a second load of a
    // document that may be perfectly fine.
    expect(frameKeyOf(container)).toBe(keyWhenVouched)
    expect(frameOf(container)).toBe(frame)
    // The pane says so, too — a hidden frame with nothing over it is only half a state.
    expect(seenNotJustSaid(/opening your app/i)).toHaveLength(1)

    // ★ LIVENESS: the new document vouches and the pane reveals it again, so the retraction above
    // is a state that RESOLVES rather than a pane stuck at "Opening your app…" for good.
    vouch(container)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
  })
})

describe('LivePreview — one persistent status region announces every state', () => {
  it('the region is mounted even when the pane has nothing to say', () => {
    // Mounted ALWAYS, on purpose: inserting a live region together with its text announces
    // inconsistently, so the element outlives every state and only its text changes.
    //
    // Mutation-check: gate the region on `announcement` being non-empty and this goes red.
    const { container } = render(<LivePreview previewUrl={null} status={null} />)
    const region = container.querySelector('[role="status"]')
    expect(region).toBeTruthy()
    expect(region?.getAttribute('aria-live')).toBe('polite')
    expect(region?.textContent).toBe('')
  })

  it('routes a RESTORE through the labelled wait, announced — not through a terminal card', () => {
    // "Behind a labelled wait, and at no point is an error shown," RE-POINTED. The wait it
    // used to drive was `showRestoring`, keyed off a `relaunching` prop nothing could set. The
    // restore a citizen can actually run comes back as a `previewUrl`, and the wait that labels it
    // is the frame's own vouch gate — it holds until the restored document says it rendered.
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)

    expect(seenNotJustSaid(/opening your app/i)).toHaveLength(1)
    expect(screen.getByRole('status').textContent).toMatch(/opening your app/i)
    expect(container.querySelector('[data-testid="preview-ended-card"]')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('★ falls SILENT once the frame reveals with no verdict — nothing checked the app', () => {
    // This asserted `/preview is live/i`, which the pane published from the framed document's
    // `load` alone: an event that fires for a 500 exactly as it does for a 200 on a frame whose
    // status code this pane cannot read. The wait ENDING is real and still asserted; what is no
    // longer asserted is a verdict nothing had evidence for.
    const { container } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ended" serving previewState="alive" />,
    )
    expect(screen.getByRole('status').textContent).toMatch(/opening your app/i)

    // NO `load` HERE, DELIBERATELY. The beacon is sufficient on its own, and it has to be: a
    // document that vouches from its own instrumentation before the frame's load event settles
    // would otherwise sit behind the wait with its app already painted.
    //
    // Mutation check: add `frameLoaded &&` back into `revealed` and this goes red still saying
    // "Opening your app…" over a document that has told this pane it rendered.
    vouch(container)

    expect(screen.getByRole('status').textContent).toBe('')
    // LIVENESS, PAIRED: the frame is up and revealed, so the silence is the announcement chain
    // reaching its end rather than a pane that failed to render.
    expect(deviceCard(container).className).toMatch(/opacity-100/)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
  })

  /**
   * The other half of the false "preview is live" claim, and why it was not simply deleted.
   *
   * Removing it outright left the SUCCESS path silent while the failure path spoke: a citizen
   * using a screen reader heard the wait end and then nothing, and could not tell "it worked"
   * from "the pane stopped talking". The failure verdict gets a sentence, so its opposite does
   * too — but only where there is evidence, which is a serving container AND a clean compile
   * verdict, never the framed document's `load`.
   */
  it('says the preview is live once the build is verified clean, and only then', () => {
    const view = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ended" serving previewState="alive" compileState="clean" />,
    )
    // The reveal this sentence hangs off is the document's own beacon; `serving` and a `clean`
    // verdict are what let the pane go on to make a claim ABOUT it.
    vouch(view.container)
    expect(screen.getByRole('status').textContent).toMatch(/preview is live/i)

    // ★ THE MUTANT THIS KILLS: `compileState !== 'failed'` instead of `=== 'clean'`. That is the
    // three-into-two collapse this exact-match check forbids, and it republishes the same false
    // claim on exactly the reload where nothing has been verified. An unreadable verdict must
    // assert NOTHING.
    view.rerender(
      <LivePreview previewUrl={SANDBOX_URL} status="ended" serving previewState="alive" compileState="unknown" />,
    )
    expect(screen.getByRole('status').textContent).toBe('')
    // LIVENESS, PAIRED: the pane is still framing the app, so the silence above is the rule
    // firing rather than a component that stopped rendering.
    expect(view.container.querySelector('iframe')).not.toBeNull()

    // And a container that is not answering cannot be called live however clean the build was.
    view.rerender(
      <LivePreview previewUrl={SANDBOX_URL} status="ended" serving={false} previewState="alive" compileState="clean" />,
    )
    expect(screen.getByRole('status').textContent).not.toMatch(/preview is live/i)
  })

  it('★ and that claim is held up by `AppPane``s VETO, not by this pane`s own inputs', () => {
    // ★ WRITTEN DOWN BECAUSE IT IS LOAD-BEARING AND INVISIBLE, and because the tempting version of
    // this claim is false. It is NOT true that the serving stamp reaches every input of the live
    // sentence: `serving` has three arms (`utils/previewAddress.ts`) and only `fromProject`
    // consults the preview-state poll — `fromTurn` and `fromSession` are a live turn's own word for
    // it and never see the stamp. So this component, handed a turn-sourced `serving` and a clean
    // compile, will announce the app live over a workspace reading that is nowhere near `running`.
    //
    // That is exactly what this test shows, and it is not a bug HERE: the sentence is honest in
    // the product because `AppPane` will not mount this component at all unless the reading is
    // `running`. Weaken that veto and the claim goes back to being unearned on the turn-sourced
    // arms, with nothing in this file to catch it — which is why the veto has its own exhaustive
    // test in `workspace/__tests__/AppPane.test.tsx` and why this one points at it.
    const view = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ended" serving previewState="asleep" compileState="clean" />,
    )
    vouch(view.container)

    expect(screen.getByRole('status').textContent).toMatch(/preview is live/i)
    // …over a reading the workspace map calls SAVED. One level up is where that is refused.
    expect(view.container.querySelector('iframe')).toBeTruthy()
  })
})

// The retraction, on the surface the citizen is actually looking at.
describe('a workspace found reverted while the tab sat idle', () => {
  // ★ IT OUTRANKS EVERY OTHER COVER SENTENCE, running turn or not. It is the only one that is a
  // fact about what is IN THE FRAME rather than about a compile; the others all describe the
  // citizen's own app, mid-change. A progress line over a workspace that has been wiped is exactly
  // the false-progress claim this pane must never make.
  //
  // Mutation check: move `workspaceLost` below `turnRunning` in the cover's ternary and the
  // during-a-turn case goes red.
  it.each([
    ['idle', false],
    ['during a turn', true],
  ])('names what is in the frame and promises the restore (%s)', (_when, turnRunning) => {
    render(
      <LivePreview
        previewUrl="https://app.example.test/"
        status="ended"
        serving
        previewState="alive"
        compileState="clean"
        turnRunning={turnRunning}
        workspaceLost
      />,
    )

    // TWO NODES, DELIBERATELY: the visible cover and the pane's permanent live region, which
    // announces the same sentence. `getAllBy` rather than `getBy` for that reason — and asserting
    // on BOTH is the point, because a cover nobody hears is half the retraction.
    expect(screen.getAllByText(/isn’t your app any more/i)).toHaveLength(2)
    // IT PROMISES A RESTORE, and unlike every other sentence in this component it is entitled to:
    // the next turn's integrity gate puts the app back from the last durable copy.
    expect(screen.getAllByText(/we’ll restore it/i).length).toBeGreaterThan(0)
    // ★ AND IT NAMES THE FRAME, NOT THE WORKSPACE. This sentence used to open "Your app stopped
    // running" — a verdict on the workspace that this component cannot reach and that contradicts
    // its own mounting condition.
    expect(screen.queryByText(/stopped running/i)).toBeNull()
  })

  it('leaves the ordinary idle wording alone when the workspace is fine', () => {
    render(
      <LivePreview
        previewUrl="https://app.example.test/"
        status="ended"
        serving
        previewState="alive"
        compileState="building"
      />,
    )

    expect(screen.queryByText(/isn’t your app any more/i)).toBeNull()
    // LIVENESS: the cover really is up, so the absence above is a choice of wording rather than
    // a component that rendered nothing at all.
    expect(screen.getAllByText(/putting this page together/i).length).toBeGreaterThan(0)
  })
})

describe('the wire parser — where a coercion would do its damage silently', () => {
  it('a missing `restorable` field parses to null, not to false', async () => {
    const verdict = await asTheBrowserSeesIt({ state: 'asleep', alive: false })
    expect(verdict.restorable).toBeNull()
  })

  it('an unreadable body is `unknown`, never a confident "gone"', async () => {
    const verdict = await asTheBrowserSeesIt('not json at all')
    expect(verdict.state).toBe('unknown')
    expect(verdict.alive).toBe(false)
    expect(verdict.restorable).toBeNull()
  })

  it('an unrecognised state falls back only as far as `alive` can prove', async () => {
    // A tab that outlives a deploy. `alive: true` is still a fact; anything else is unknown —
    // never a confident "gone", which is what the old parser would have produced.
    expect((await asTheBrowserSeesIt({ alive: true, previewUrl: SANDBOX_URL })).state).toBe('alive')
    expect((await asTheBrowserSeesIt({ alive: false, state: 'gone-ish' })).state).toBe('unknown')
  })

  it('STARTING parses as its own state, not a coerced "unknown"', async () => {
    // The closed-list defect this state exists to catch: an unwidened `PREVIEW_LIFE_STATES` would
    // fall through `asPreviewLifeState`'s fallback straight to 'unknown' (`alive` is false), which
    // is a confident-sounding "nothing to report" for a fact the server DID report.
    expect((await asTheBrowserSeesIt({ state: 'starting', alive: false })).state).toBe('starting')
  })
})

describe('★ the deletions, pinned structurally — because a rendered assertion cannot see them', () => {
  it('defines and exports no start affordance at all', async () => {
    // A STRUCTURAL guard, because the behavioural ones above can only see the states they set up.
    // Four render sites shared one component; deleting three and leaving the fourth is exactly the
    // partial removal that made this worth pinning, and no rendered assertion would have caught it.
    const source = (await import('../LivePreview?raw')).default as string
    const uses = source.split('RelaunchAffordance').length - 1

    // One mention survives — the note recording the removal and where the control went.
    expect(uses).toBe(1)
    expect(source).toMatch(/`RelaunchAffordance` IS GONE/)
    expect(source).not.toMatch(/function RelaunchAffordance/)
  })

  it('★ defines no workspace copy table, and no card to draw one from', async () => {
    // ★ THE PIN THE FILE-LEVEL DELETION NEEDED. The four titles and six bodies lived in
    // `GONE_TITLE` and `goneBody`, drawn by `showUnavailable` and `showTerminal`. A test that only
    // rendered the component would go green the moment those were deleted AND the moment somebody
    // reintroduced one under a new name behind a state this suite does not set up — so the
    // identifiers themselves are what is pinned.
    const source = (await import('../LivePreview?raw')).default as string

    // ASSERTED AS DEFINITIONS AND RENDER SITES, NOT AS MENTIONS, and the distinction is what keeps
    // this guard from fighting the documentation: the file's own docblock NAMES all four of these
    // while recording that they went, and a `not.toContain` would make writing that note down the
    // failure. What must not come back is a binding or a test hook, so that is what is matched.
    const cannotComeBack: [string, RegExp][] = [
      ['the copy table', /\b(const|let|function)\s+GONE_TITLE\b/],
      ['the body picker', /\b(const|let|function)\s+goneBody\b/],
      ['the unavailable card', /\b(const|let)\s+showUnavailable\s*=/],
      ['the terminal card', /\b(const|let)\s+showTerminal\s*=/],
      ['the unavailable card`s test hook', /preview-unavailable-card/],
      ['the terminal card`s test hook', /preview-ended-card/],
    ]
    for (const [what, definition] of cannotComeBack) {
      expect(source, `${what} is back in LivePreview.tsx`).not.toMatch(definition)
    }
    // AND THE FILE SAYS WHY, so the next person to reach for a workspace sentence here reads the
    // rule before they write one.
    expect(source).toMatch(/THIS FILE NO LONGER AUTHORS A SINGLE WORKSPACE SENTENCE/)
    // LIVENESS: the source really was read, so a bad import path cannot green the sweep above.
    expect(source).toMatch(/export default function LivePreview/)
  })

  it('★ accepts no prop whose only job was filling in a workspace sentence', async () => {
    // `hasSavedBuild` and `occupyingProjectName` went with the two cards that read them. They have
    // to leave `workspaceChannel.ts`'s `PaneView` in the same change — its `UnacceptedPaneProps`
    // assertion is what makes that a compile error rather than a field quietly going nowhere.
    const source = (await import('../LivePreview?raw')).default as string
    const props = source.slice(source.indexOf('export interface LivePreviewProps'), source.indexOf('export default function LivePreview'))

    expect(props).not.toMatch(/^\s*hasSavedBuild\??:/m)
    expect(props).not.toMatch(/^\s*occupyingProjectName\??:/m)
    expect(props).not.toMatch(/^\s*onRelaunch\??:/m)
    // LIVENESS: the slice really is the props block, and the props that stay are still declared.
    expect(props).toMatch(/^\s*previewUrl\?:/m)
    expect(props).toMatch(/^\s*previewState\?:/m)
  })

  it('keeps everything from the frame inward untouched', async () => {
    // The removal was of NO-FRAME chrome. The security seam, the cover, the frame key and the
    // device widths are the parts of this component the workspace redesign explicitly does not
    // touch, and a sweep that took them with the placeholders would be a silent regression on the
    // one thing this file is genuinely load-bearing for.
    const source = (await import('../LivePreview?raw')).default as string

    expect(source).toMatch(/e\.source/)          // the inbound-message gate, on origin AND source
    expect(source).toMatch(/sandbox=/)           // the sandbox token list
    expect(source).toMatch(/const frameKey =/)   // the frame's identity
    // The device WIDTHS are still read here; the TABLE moved out with the control that picks them,
    // so this asserts the import rather than the literal — two copies of it is the
    // drift this guard exists to prevent, not one copy in a new file.
    //
    // IT POINTS AT THE LEAF, not at the toolbar that draws the switcher. Importing the table from
    // the toolbar closed a five-module ring back into this file; `devices.ts` imports nothing of
    // ours, so nothing can import its way back here through it.
    expect(source).toMatch(/import \{ DEVICES, type DeviceName \} from '\.\/workspace\/devices'/)
    expect(source).toMatch(/DEVICES\[device\]\.width/)

    // AND THE LEAF IS STILL A LEAF. The whole value of the move is that `devices.ts` imports
    // nothing of ours, so no ring can form back through it; a relative import added there is what
    // would quietly rebuild the one this replaced.
    const table = (await import('../workspace/devices?raw')).default as string
    expect(table).toMatch(/export const DEVICES/)
    expect(table).not.toMatch(/from '\.\.?\//)
    // …the cover that holds on an unknown FOR AN UNVOUCHED FRAME — the verdict half latches, and
    // since revision 4 it remembers WHICH verdict raised it, because that is what the yield below
    // turns on: a `building` cover gives way to a vouched document once the signal goes dark, and
    // a `failed` cover never does. A bare boolean cannot express that difference, so the type
    // itself is what this pins.
    expect(source).toMatch(
      /const \[verdictCover, setVerdictCover\] = useState<'building' \| 'failed' \| null>/,
    )
  })

  it('★ and the timers, the bounds, the wire and the rule they serve are still in the file', async () => {
    // ★ THE OWNER CARVE-OUT, PINNED. The design deleted every other card in this file on the
    // strength of the serving stamp; the stall card stays because the stamp does not answer its
    // question. The proof is a loopback GET to 127.0.0.1:3000 INSIDE the container; the citizen's
    // browser reaches the same app through the portal edge → the ACA ingress, and a 502 with an
    // empty body lives in that gap and fires `load` exactly as a page does.
    const source = (await import('../LivePreview?raw')).default as string

    // THE NUMBERS THE TESTS ABOVE DRIVE, read out of the file rather than assumed. All six are
    // interpolated from this suite's own constants, so a change on either side is a failure here
    // instead of a timing test that silently stops reaching the state it names.
    expect(source).toMatch(new RegExp(`const FRAME_LOAD_CAP_MS = ${FRAME_LOAD_CAP_MS}`))
    expect(source).toMatch(new RegExp(`const VOUCH_AFTER_LOAD_MS = ${VOUCH_AFTER_LOAD_MS}`))
    expect(source).toMatch(new RegExp(`const PINGS_BEFORE_RELOAD = ${PINGS_BEFORE_RELOAD}`))
    expect(source).toMatch(
      new RegExp(`const ALIVE_PINGS_BEFORE_STALL = ${ALIVE_PINGS_BEFORE_STALL}`),
    )
    expect(source).toMatch(new RegExp(`const VOUCH_RETRY_LIMIT = ${VOUCH_RETRY_LIMIT}`))
    expect(source).toMatch(new RegExp(`const HEARTBEAT_MS = ${HEARTBEAT_MS}`))
    expect(source).toMatch(new RegExp(`const BUILDING_COVER_MAX_MS = ${BUILDING_COVER_MAX_MS}`))

    // ★ AND THE WIRE ITSELF, WHICH IS MATCHED BY VALUE ON BOTH SIDES AND THEREFORE CANNOT BE
    // RENAMED SAFELY BY EITHER. This suite dispatches `bial:app-mounted` in `vouch()` and asserts
    // `bial:ping` on the spy; the template posts the same three strings from
    // `sandbox/template/instrumentation-client.ts`. A constant renamed here compiles and a value
    // changed here goes silent — the pane simply never hears the beacon again — so the VALUES are
    // what is pinned, not the identifiers.
    expect(source).toMatch(/const MOUNTED_TYPE = 'bial:app-mounted'/)
    expect(source).toMatch(/const PAINTING_TYPE = 'bial:app-painting'/)
    expect(source).toMatch(/const PING_TYPE = 'bial:ping'/)
    // ★ AND THE TIMER THAT IS GONE STAYS GONE, WHICH IS A BEHAVIOUR AND NOT A TIDY-UP.
    // `PING_REPLY_MS` was a clock of its own — a window in which a ping had to be answered — and an
    // unanswered ping now simply costs the next expiry of the SAME wait. Bringing it back would
    // mean a second clock racing the one this suite drives, and every timing test here would still
    // pass. The `bial:app-painting` reply took its job: a document that answers is asked again on
    // the one wait, and the mark it earns is spent by the very expiry that reads it.
    //
    // ASSERT-ABSENCE, PAIRED WITH THE NINE LINES ABOVE: the constants block and the wire really
    // were read, so this is a deleted timer staying deleted rather than a regex matching an empty
    // file.
    expect(source).not.toMatch(/PING_REPLY_MS/)
    // ★ AND THE SENTENCE THAT ORDERS THE TWO OF THEM, which is the whole of the design and the one
    // thing no number in this file carries: a re-request tears down the very hydration that
    // produces the beacon, so a silent document is ASKED first and only a document that ignores
    // its pings is fetched again.
    expect(source).toMatch(/ASKING COMES BEFORE RE-REQUESTING/)

    // ★ AND THE RULE ALL FOUR OF THEM SERVE, which is the whole of the 2026-09-10 fix: none of
    // them reveals anything. `load` is not evidence, a timer is not evidence, and no status this
    // pane is HANDED is measured on the citizen's side of the network.
    expect(source).toMatch(/THE REVEAL RESTS ON THE FRAMED DOCUMENT VOUCHING FOR ITSELF, AND ON NOTHING ELSE\./)
    expect(source).toMatch(/const revealed = frameVouched && !covered/)
    // ASSERT-ABSENCE, PAIRED WITH THE LINE ABOVE: the reveal predicate really is in this file and
    // really is the beacon's, so this is `load` being kept out of it rather than a regex that
    // matched nothing because the expression was renamed out from under it.
    expect(source).not.toMatch(/const revealed = [^\n]*frameLoaded/)

    expect(source).toMatch(/frameStalled && !showCover/)
  })
})

/**
 * ★ THE BLANK WHITE RECTANGLE — the cover's fail-closed arm, guarded.
 *
 * THE INCIDENT, so nobody has to reconstruct it from the predicate. On 2026-09-10 the owner
 * described an app, the build turn ran, and this pane showed a COMPLETELY BLANK WHITE RECTANGLE —
 * no app, no loading state, no words at all — while the chat beside it said "Working on your app".
 * Reported three times. The chain: the container was framed 7,020ms after it started, inside a
 * fresh Next.js app's very first route compile, so the document that arrived was empty; the
 * compile signal was `unknown` for the entire run (the deployed supervisor read its own dev
 * server's handshake as protocol drift — see `sandbox/supervisor/test_app.py`); and `covered` was
 * derived from `compileState` ALONE, so `unknown` resolved to "show the bare frame, confidently".
 *
 * WHAT THESE TESTS PIN is the shipped predicate, read out of the file rather than guessed at:
 *
 *     const documentIsVouchedFor = compileState === 'clean' || !turnRunning
 *     const flyingBlind = showFrame && !documentIsVouchedFor
 *     const verdictHasGoneDark = compileState === 'unknown'
 *     const covered =
 *       (coveredByVerdict && !(verdictCover === 'building' && verdictHasGoneDark && frameVouched))
 *       || flyingBlind
 *
 * — and BOTH of its halves, because each one alone is a defect. Without the first tests below the
 * fix is indistinguishable from doing nothing; without the "must not cover forever" one it is
 * indistinguishable from a wait card that never resolves, which is the worse of the two failures
 * (a blank pane at least ends when you reload; a permanent wait does not).
 *
 * ★ AND THE COVER OUTRANKS THE BEACON, which is why every test here goes all the way — `load` AND
 * the document's own `bial:app-mounted` — and then asserts what is on SCREEN. The beacon says the
 * root layout rendered; it says nothing about WHICH version of the app rendered, so a document
 * served out of an app that is being rewritten as the citizen watches is vouched for and still
 * must not be shown. A test that stopped at the load, or that never sent the beacon at all, would
 * be green over a pane that revealed the half-written app the moment it spoke.
 *
 * ★ …UNTIL A `building` VERDICT'S OWN SIGNAL GOES DARK, WHICH IS THE ONE CLAUSE UNDER WHICH THE
 * BEACON WINS — and revision 4 narrows it to exactly that. The cover remembers WHICH verdict
 * raised it, and three things have to be true at once before it comes down for a document's word:
 * the cover is a `building` one (a compile takes seconds, so a reader that reported one and then
 * could not decide has almost certainly missed its end), the container ANSWERED with `unknown`
 * (`null` is this pane's own silence and asserts nothing at all), and the frame has VOUCHED.
 *
 * Each limit is a test below, because each one is a fail-open if it is dropped: a `failed` cover
 * that yielded would show a broken app on the strength of a vouch that predates the change which
 * broke it; a `null` that counted as dark would let the pane uncover on its own silence; and a
 * dark signal that uncovered an UNVOUCHED frame is the blank white rectangle of 13:44:12, revealed
 * on a signal that said nothing. `flyingBlind` is untouched by the clause — a running turn with no
 * `clean` verdict covers a vouched document exactly as it always did.
 */
describe('★ a document nothing vouches for is covered, and a document nothing is writing is not', () => {
  it('★ THE REPORTED CASE: a build turn running with no compile verdict shows WORDS, not a bare frame', () => {
    // The exact shape of the run the owner watched: a turn in flight, a container answering, and a
    // compile signal that says `unknown` because the platform could not read that container at all.
    const { container } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="building"
        serving
        previewState="alive"
        compileState="unknown"
        turnRunning
      />,
    )

    // ★ THE LOAD IS THE POINT, NOT THE SETUP. This is the 13:44:12 event: an empty document
    // arriving in the frame and firing `load` exactly as a working app would. Before the fix this
    // single line ended every wait on screen and left the citizen looking at nothing.
    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    // …AND THE BEACON ON TOP OF IT, which is the assertion the beacon fix could quietly have lost.
    // A document being rewritten as the citizen watches still renders a root layout and still
    // vouches for itself — truthfully. The cover is not a claim that nothing is in the frame; it
    // is a claim that what is in the frame is a snapshot of a half-written app, and the document's
    // own word cannot answer that.
    vouch(container)

    // Mutation check: restore `const covered = coveredByVerdict` (the pre-fix predicate), or drop
    // `!covered` from `revealed`, and this goes red with an empty pane — no cover, and a device
    // card at full opacity over a blank document. That is the defect, reproduced.
    expect(seenNotJustSaid(/putting the latest change together/i)).toHaveLength(1)
    expect(screen.getByRole('status').textContent).toMatch(/putting the latest change together/i)
    // …and the blank document stays HIDDEN behind those words rather than being presented as the
    // citizen's app. The cover is on top either way; leaving the frame revealed underneath is what
    // makes a 502's error page or an empty body the thing being faded in.
    expect(deviceCard(container).className).toMatch(/opacity-0/)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    // ★ LIVENESS, PAIRED, per this repo's own rule: the frame really is mounted underneath, so the
    // assertions above are a cover being drawn rather than a component that threw and rendered
    // nothing. An `opacity-0` assertion over a pane with no device card at all would false-green.
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('★ and the reveal clock is not stopped by it — `onRevealed` does not fire over a blank frame', () => {
    // The same predicate, seen from the one number the product reports. `onRevealed` is "this is
    // how long until the citizen saw their app", and it was being stopped by a blank white
    // rectangle — so the runs where they saw NOTHING were logged as the fastest views of the day.
    //
    // Mutation check: it dies to the same mutant as the test above (`covered = coveredByVerdict`),
    // because `revealed` is `frameVouched && !covered`. Asserted separately anyway: the counter is
    // a different consumer of the predicate, and a future change could keep the cover while
    // quietly re-arming the clock behind it.
    const onRevealed = vi.fn()
    const { container } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="building"
        serving
        previewState="alive"
        compileState="unknown"
        turnRunning
        onRevealed={onRevealed}
      />,
    )

    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    vouch(container)

    expect(onRevealed).not.toHaveBeenCalled()
    // LIVENESS: the load and the beacon really did land on a mounted frame, so the silence above
    // is the gate holding rather than two events that never reached the component.
    expect(seenNotJustSaid(/putting the latest change together/i)).toHaveLength(1)
  })

  it('★ no turn running and a permanently `unknown` signal still reveals ON THE BEACON', () => {
    // ★ THE HALF THAT MAKES THE FIX SHIPPABLE, and the failure it prevents is worse than the one
    // being fixed. Every container running an image older than the compile endpoint reports
    // `unknown` forever, and there are apps in the fleet doing exactly that today. Gating an IDLE
    // app on a positive verdict would put the whole of that fleet behind a wait card that no
    // reload can clear — a blank pane at least ends when you reload; that would not.
    //
    // So between turns the compatibility concession stands: nothing is being written, whatever is
    // in the frame IS the app as it stands, and the citizen is entitled to look at it — ONCE that
    // document has said it rendered. The concession is about the COVER, never about the reveal:
    // "no turn is running" is a fact about the platform, and this pane no longer shows a frame on
    // the strength of any fact measured on its own side of the network.
    const { container } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ended"
        serving
        previewState="alive"
        compileState="unknown"
        turnRunning={false}
      />,
    )

    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    vouch(container)

    // Mutation check: drop the `|| !turnRunning` arm — `documentIsVouchedFor = compileState ===
    // 'clean'` — and this goes red with the app hidden behind "Putting this page together…"
    // forever, for the whole of the fleet whose image cannot report a compile at all.
    expect(deviceCard(container).className).toMatch(/opacity-100/)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
    expect(screen.queryAllByText(/putting this page together/i)).toHaveLength(0)
    expect(screen.queryAllByText(/putting the latest change together/i)).toHaveLength(0)
    // The pane has nothing left to say once the app is on screen, which is the end of the
    // announcement chain rather than a state that forgot to speak.
    expect(screen.getByRole('status').textContent).toBe('')
    // LIVENESS: the app it is revealing is really there.
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('★ …and the same state with only a `load` behind it stays at the labelled wait', () => {
    // ★ THE SIBLING OF THE TEST ABOVE, AND THE ONE THAT MAKES IT MEAN SOMETHING. Identical props:
    // idle, serving, a compile signal that will never say anything. The ONLY difference is that
    // nothing on the citizen's side of the network has vouched for the document — which is the
    // exact state of the 13:44:12 frame, where a 502 with a zero-byte body fired `load` and the
    // pane confidently presented it as the citizen's app.
    //
    // Mutation check: put `frameLoaded` back into `revealed` (or restore `revealed = frameLoaded
    // && !covered`) and this goes red — the wait gone, the card at full opacity, and a blank
    // rectangle on screen with no words over it. That is the reported defect, reproduced from the
    // one side that can see it.
    const { container } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ended"
        serving
        previewState="alive"
        compileState="unknown"
        turnRunning={false}
      />,
    )

    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)

    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    expect(deviceCard(container).className).toMatch(/opacity-0/)
    // ★ AND IT IS A LABELLED WAIT, NOT A HIDDEN FRAME WITH NOTHING OVER IT. Taking the reveal away
    // is only half a state; the first version of the frame veto shipped only that half and drew an
    // empty rectangle. The visible assertion is the one that matters — the sr-only region speaks
    // in every state, so asserting on it alone is true over a pane drawing literally nothing.
    expect(seenNotJustSaid(/opening your app/i)).toHaveLength(1)
    expect(screen.getByRole('status').textContent).toMatch(/opening your app/i)
  })

  it('★ the cover comes off when the build ends and the app paints — and WITHOUT a reload', () => {
    // The recovery, end to end and on the same document. `turnRunning` falling clears the arm in
    // the SAME render: there is no second load to wait for and no remount to sit through, which
    // matters because a citizen who has already watched a wait will read a second one as the
    // build having restarted.
    const { container, rerender } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="unknown"
        turnRunning
      />,
    )
    const frame = container.querySelector('iframe') as HTMLIFrameElement
    fireEvent.load(frame)
    // The document vouches WHILE THE COVER IS UP, which is the ordinary case and not a corner one:
    // the app compiled its first route and rendered, the platform just could not read that its
    // compile was clean. The beacon is recorded against this frame key and waits under the cover.
    vouch(container)
    // The premise, guarded: the cover really was up before the turn ended, so what follows is a
    // state CHANGE rather than a pane that was never covered in the first place.
    expect(seenNotJustSaid(/putting the latest change together/i)).toHaveLength(1)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    const keyWhileCovered = frame.getAttribute('data-frame-key')

    // The turn ends. Nothing else moves — same url, same status, same (still unreadable) compile
    // signal. This is the ONLY input that changes.
    rerender(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="unknown"
        turnRunning={false}
      />,
    )

    expect(screen.queryAllByText(/putting the latest change together/i)).toHaveLength(0)
    expect(deviceCard(container).className).toMatch(/opacity-100/)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
    // ★ NO RELOAD, ASSERTED ON THE SEAM THAT CAN SEE ONE. `data-frame-key` is the React `key`
    // written where a test can read it, so a remount is visible from outside; the node identity
    // compare beside it catches a remount that reused the key. A fix that recovered by
    // re-requesting the document would pass every assertion above and still show the citizen a
    // second "Opening your app…" for their trouble.
    const after = container.querySelector('iframe') as HTMLIFrameElement
    expect(after.getAttribute('data-frame-key')).toBe(keyWhileCovered)
    expect(after).toBe(frame)
  })

  it('★ `clean` uncovers even mid-turn — evidence outranks the running turn, which is the point of it', () => {
    // The first of the two things that can vouch for a document, and the only one that works while
    // a turn is in flight: the platform asked the container what it compiled and got an answer.
    // Without this arm the fail-closed cover would swallow every mid-build reveal the compile
    // signal was built to give — the fleet that CAN report would be treated exactly like the fleet
    // that cannot.
    //
    // Mutation check: reduce the predicate to `documentIsVouchedFor = !turnRunning` and this goes
    // red with the app hidden behind "Putting the latest change together…" despite a clean build.
    const { container } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="building"
        serving
        previewState="alive"
        compileState="clean"
        turnRunning
      />,
    )

    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    vouch(container)

    expect(deviceCard(container).className).toMatch(/opacity-100/)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
    expect(screen.queryAllByText(/putting the latest change together/i)).toHaveLength(0)
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it.each(['building', 'failed'] as const)(
    '★ `%s` still covers a frame that has VOUCHED, exactly as it always covered a loaded one',
    (compileState) => {
      // The half of the cover that did NOT change, pinned beside the half that did. The fix split
      // `covered` into `coveredByVerdict || flyingBlind`, and a split is one edit away from
      // dropping the arm that was already working — in which case the reported defect would be
      // fixed and the ERROR-SCREEN case, which this mechanism originally shipped for, would be
      // silently reopened.
      //
      // Mutation check: drop `coveredByVerdict` from `covered` and BOTH rows go red — `failed`
      // outright, and `building` too, because a verdict that arrives between turns has no
      // `flyingBlind` arm to fall back on.
      const { container } = render(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="building"
          serving
          previewState="alive"
          compileState={compileState}
          turnRunning={false}
        />,
      )

      // THE BEACON IS SENT AND IT LOSES, which is the half the reveal rewrite could have dropped
      // without any other test noticing: a framework error screen is a rendered root layout, and a
      // route compiling on demand inside a perfectly healthy app posts one too. The document's own
      // word is what the pane REVEALS on; it is never what it uncovers on.
      fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
      vouch(container)

      const sentence = compileState === 'failed' ? /this page can’t open/i : /putting this page together/i
      expect(seenNotJustSaid(sentence)).toHaveLength(1)
      expect(deviceCard(container).className).toMatch(/opacity-0/)
      expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
      expect(container.querySelector('iframe')).toBeTruthy()
    },
  )

  it('★ THE FLEET CASE: a cover raised by `building` yields to the vouched document once the signal goes dark', () => {
    // ★ THE FAILURE THIS CLOSES, NAMED. The verdict latches on `building` and ONLY an affirmative
    // `clean` clears it — which is right while the signal is alive and a permanent lie once it is
    // not. A compile reader that drifts to `unknown` after a `building` is the fleet-wide shape the
    // 2026-09-10 handoff records twice, and no `clean` is ever coming from a supervisor that cannot
    // read its own dev server: a page the citizen's OWN BROWSER reported as showing sat behind
    // "Putting this page together…" with no stall card, no escalation and no way out.
    //
    // So the verdict's half of the cover yields — to the one witness on the citizen's side of the
    // network, and only once nobody can re-confirm the verdict any more.
    const { container, rerender } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="building"
        turnRunning
      />,
    )
    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    // The beacon lands UNDER the cover, which is the ordinary case rather than a corner one: the
    // app painted a route, the platform simply could not read what it compiled.
    vouch(container)
    // The premise, guarded: the cover really was up over a document that had already vouched, so
    // what follows is a state CHANGE rather than a pane that was never covered in the first place.
    expect(seenNotJustSaid(/putting the latest change together/i)).toHaveLength(1)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    const keyWhileCovered = frameKeyOf(container)

    // The signal goes dark and the turn ends. `unknown` does not clear `coveredByVerdict` — only a
    // `clean` does — so the `building` verdict is still latched underneath this render.
    rerender(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="unknown"
        turnRunning={false}
      />,
    )

    // Mutation check: drop the `!(verdictHasGoneDark && frameVouched)` term from `covered` and this
    // goes red — the citizen's app hidden behind "Putting this page together…" for as long as the
    // container cannot report, which on an image that predates the compile signal is forever.
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
    expect(deviceCard(container).className).toMatch(/opacity-100/)
    expect(screen.queryAllByText(/putting this page together/i)).toHaveLength(0)
    expect(screen.queryAllByText(/putting the latest change together/i)).toHaveLength(0)
    // Nothing is claimed ABOUT the app on the way out, either: an `unknown` verdict is not a clean
    // one, so the pane reveals and says nothing.
    expect(screen.getByRole('status').textContent).toBe('')
    // ★ LIVENESS, PAIRED: the app really is on screen, in the SAME document that vouched. A fix
    // that recovered by re-requesting would satisfy every absence above and still cost the citizen
    // a second "Opening your app…" for their trouble.
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(frameKeyOf(container)).toBe(keyWhileCovered)
  })

  it('★ the same signal going dark over an UNVOUCHED frame holds the cover, exactly as it always did', () => {
    // ★ THE SIBLING THAT MAKES THE TEST ABOVE MEAN SOMETHING, and the old rule kept whole: an
    // `unknown` asserts NOTHING, so on its own it can neither raise a cover nor take one down. What
    // takes the cover down above is the DOCUMENT, and nothing has spoken for this one — a `load` is
    // no witness at all, because the bodyless 502 at the ingress fires it exactly as a page does.
    const { container, rerender } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="building"
        turnRunning
      />,
    )
    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    // The premise, guarded: same cover, same latched verdict, and the ONLY difference from the test
    // above is that no beacon is sent.
    expect(seenNotJustSaid(/putting the latest change together/i)).toHaveLength(1)

    rerender(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="unknown"
        turnRunning={false}
      />,
    )

    // Mutation check: drop the `&& frameVouched` half of the yield (so a dark signal uncovers on
    // its own) and this goes red — the blank document of 13:44:12 presented as the citizen's app on
    // the strength of a signal that said nothing at all.
    expect(seenNotJustSaid(/putting this page together/i)).toHaveLength(1)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    expect(deviceCard(container).className).toMatch(/opacity-0/)
    // LIVENESS, PAIRED: the frame is mounted under that cover, so the refusal above is a cover
    // being held rather than a pane that rendered nothing.
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('★ `null` is not dark: a `building` cover holds over a vouched document while the signal reports NOTHING', () => {
    // ★ THE DIFFERENCE BETWEEN A READING AND A SILENCE, WHICH IS THE WHOLE OF THIS TEST. `unknown`
    // is an ANSWER — the platform asked the container what it compiled and the container could not
    // say — and that is what makes it evidence that nobody will ever re-confirm the `building`.
    // `null` is this pane's own "nothing has been reported on this turn at all", which every other
    // line in the component treats as no evidence whatsoever. Letting the pane uncover on its own
    // silence is the same fail-open as absent-reads-as-clean, wearing a different value.
    //
    // `turnRunning` is false throughout, deliberately: it takes `flyingBlind` out of the
    // expression, so the verdict half is the only thing holding this cover up and the mutant below
    // is visible rather than masked by the other arm.
    const { container, rerender } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="building"
        turnRunning={false}
      />,
    )
    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    vouch(container)
    // The premise, guarded: a `building` cover, up over a document that has already vouched.
    expect(seenNotJustSaid(/putting this page together/i)).toHaveLength(1)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')

    // The container reports nothing at all — the turn's signal simply stops arriving.
    rerender(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState={null}
        turnRunning={false}
      />,
    )

    // Mutation check: widen `verdictHasGoneDark` back to revision 3's
    // `compileState === 'unknown' || compileState === null` and this goes red — the pane takes its
    // own silence for a reading and uncovers a half-compiled route on the strength of it.
    expect(seenNotJustSaid(/putting this page together/i)).toHaveLength(1)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    expect(deviceCard(container).className).toMatch(/opacity-0/)

    // ★ LIVENESS FOR THE VOUCH ITSELF, WHICH THIS TEST NEEDS MORE THAN ANY OTHER: everything above
    // is also true of a pane whose beacon never landed, and a `vouch()` that silently missed would
    // make this test a tautology. So the SAME document, the SAME cover, one value different — the
    // container answers `unknown` — and the cover comes down. That is the fleet case above,
    // re-run here for one purpose: to prove the only thing holding the cover up two assertions
    // ago was `null` not being an answer.
    rerender(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="unknown"
        turnRunning={false}
      />,
    )
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
    expect(screen.queryAllByText(/putting this page together/i)).toHaveLength(0)
  })

  it('★ a `failed` cover NEVER yields to a vouch, however dark the signal goes', () => {
    // ★ WHY THIS IS THE OPPOSITE OF THE FLEET CASE ABOVE, AND NOT AN INCONSISTENCY. `building` is a
    // MOMENT — a compile takes seconds — so a reader that reported one and then went dark has
    // almost certainly missed its end, and the document's own word is the better evidence. `failed`
    // is a STATE: it does not expire while nobody is watching, and the app stays broken until
    // something fixes it. Worse, the vouch on offer PREDATES the change that broke: a compile that
    // failed replaces nothing, so the document still in the frame is the last good render, and it
    // vouches exactly as it did before the app broke. Revealing on that word would show a citizen
    // an app that is one failed change out of date and say nothing about it.
    //
    // So only three things clear a `failed` cover: an affirmative `clean`, a new app, or the
    // citizen's own Reload while the signal is dark — the two tests below.
    const { container, rerender } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="failed"
        turnRunning={false}
      />,
    )
    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    vouch(container)
    // The premise, guarded: a vouched document under a live `failed` verdict is covered, and the
    // sentence names the failure — the row the `it.each` above pins, asserted here as a starting
    // state rather than assumed.
    expect(seenNotJustSaid(/this page can’t open/i)).toHaveLength(1)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    const keyWhileCovered = frameKeyOf(container)

    rerender(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="unknown"
        turnRunning={false}
      />,
    )

    // Mutation check: drop the `verdictCover === 'building'` term from `covered` — restoring
    // revision 3, where any latched verdict yielded once its signal went dark — and this goes red
    // with the last good render presented as the citizen's app, on a vouch that was earned before
    // the change which broke it.
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    expect(deviceCard(container).className).toMatch(/opacity-0/)
    // …and the cover is still ON SCREEN, not merely still true in a predicate. THE SENTENCE IS
    // THE BUSY ONE, NOT THE BROKEN ONE, and that is a drift reported rather than endorsed:
    // `coverText` reads the `compileState` PROP, so the moment the signal stops saying `failed`
    // the cover stops saying so too, even though `verdictCover` still remembers which verdict
    // raised it. Pinned as it behaves, so the day the sentence learns to read that memory this
    // line is what says so out loud instead of a silent change of copy.
    expect(seenNotJustSaid(/putting this page together/i)).toHaveLength(1)
    // LIVENESS, PAIRED: the same document that vouched is still the one in the frame, so the
    // refusal above is a cover being held over a live beacon rather than a frame that went away
    // underneath it.
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(frameKeyOf(container)).toBe(keyWhileCovered)
  })

  it('★ a NEW app whose first report is also `building` gets a full expiry window of its own, not the clock of the app it replaced', () => {
    // The expiry effect watched the verdict string, and a new app's first `building` is
    // Object.is-equal to the outgoing app's, so the switch changed no dependency and the old
    // clock kept running: the new app's cover came down early. Keyed to `previewUrl`, a new app
    // gets the whole BUILDING_COVER_MAX_MS.
    const SECOND_APP_URL = 'https://app-xyz.example.azurecontainerapps.io/apps/second/'
    vi.useFakeTimers()
    try {
      const { container, rerender } = render(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ready"
          serving
          previewState="alive"
          compileState="building"
          turnRunning={false}
        />,
      )
      fireEvent.load(frameOf(container))
      vouch(container)
      // Two thirds of the first app's window go by, then the pane moves to another app that is
      // also compiling, and that app's document vouches straight away.
      act(() => { vi.advanceTimersByTime(20_000) })
      rerender(
        <LivePreview
          previewUrl={SECOND_APP_URL}
          status="ready"
          serving
          previewState="alive"
          compileState="building"
          turnRunning={false}
        />,
      )
      fireEvent.load(frameOf(container))
      vouch(container, SANDBOX_ORIGIN, '/apps/second')

      // Past the moment the first app's clock would have fired. Mutation check: drop `previewUrl`
      // from the expiry effect's dependencies and this goes red, revealed ten seconds into the new
      // app's compile.
      act(() => { vi.advanceTimersByTime(BUILDING_COVER_MAX_MS - 20_000 + 1) })
      expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')

      act(() => { vi.advanceTimersByTime(20_000) })
      expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ a `building` verdict that keeps being reported keeps its cover for as long as a compile takes — then the document`s word wins', () => {
    // The other half of the yield, REVISED 2026-09-11. It used to hold for as long as the container
    // kept saying `building`, on the reasoning that a live verdict is evidence about the app being
    // compiled right now. It is — for the seconds a compile takes. Production showed the other
    // case: a `building` never followed by anything, left standing over a FINISHED build while the
    // citizen's own browser had the page on screen — "Putting this page together…" was all they
    // could see, with no re-request, no escalation and no way out but a Reload nobody mentioned.
    // So once the turn is over the verdict keeps its cover for BUILDING_COVER_MAX_MS, and then
    // yields to the one witness on the citizen's side of the network.
    //
    // `turnRunning` is false throughout on purpose: it takes `flyingBlind` out of the expression so
    // the verdict is the only thing holding this cover up.
    vi.useFakeTimers()
    try {
      const { container, rerender } = render(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ready"
          serving
          previewState="alive"
          compileState="building"
          turnRunning={false}
        />,
      )
      fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
      vouch(container)
      // …and the container reports the same thing again on the next poll, which is what "the signal
      // keeps saying so" looks like from this side of the wire.
      rerender(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ready"
          serving
          previewState="alive"
          compileState="building"
          turnRunning={false}
        />,
      )
      const keyWhileCovered = frameKeyOf(container)

      // Just short of a compile's plausible length: the verdict still wins, however loudly the
      // document vouched. Mutation check: widen `verdictHasGoneDark` to `compileState !== 'clean'`
      // and this goes red here — a half-compiled route revealed while the platform is still
      // watching it compile.
      act(() => { vi.advanceTimersByTime(BUILDING_COVER_MAX_MS - 1) })
      expect(seenNotJustSaid(/putting this page together/i)).toHaveLength(1)
      expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
      expect(deviceCard(container).className).toMatch(/opacity-0/)

      // Mutation check: drop the expiry effect and this goes red — the app hidden for as long as
      // the container keeps repeating itself, which for a reader that missed the end of a compile
      // is forever.
      act(() => { vi.advanceTimersByTime(1) })
      expect(deviceCard(container).getAttribute('data-revealed')).toBe('true')
      expect(deviceCard(container).className).toMatch(/opacity-100/)
      expect(screen.queryAllByText(/putting this page together/i)).toHaveLength(0)
      // LIVENESS, PAIRED: the same document that vouched, not a re-request.
      expect(container.querySelector('iframe')).toBeTruthy()
      expect(frameKeyOf(container)).toBe(keyWhileCovered)
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ the same expiry over an UNVOUCHED frame drops into the WAIT, never into a reveal — and never while the turn is running', () => {
    // ★ THE SIBLING THAT MAKES THE TEST ABOVE MEAN SOMETHING. The expiry takes the VERDICT's word
    // away; it gives the document nothing. A frame nobody has spoken for goes back to being asked,
    // exactly as any silent document is, and a running turn keeps its own cover regardless.
    vi.useFakeTimers()
    try {
      const { container, rerender } = render(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ready"
          serving
          previewState="alive"
          compileState="building"
          turnRunning
        />,
      )
      fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
      const keyWhileCovered = frameKeyOf(container)
      // Mid-turn, ten expiries change nothing: the cover is the running turn's, and the wait's own
      // sentence never shows through it.
      act(() => { vi.advanceTimersByTime(10 * BUILDING_COVER_MAX_MS) })
      expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
      expect(screen.queryAllByText(/opening your app/i)).toHaveLength(0)
      expect(frameKeyOf(container)).toBe(keyWhileCovered)

      // The turn ends; the verdict is still `building`.
      rerender(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ready"
          serving
          previewState="alive"
          compileState="building"
          turnRunning={false}
        />,
      )
      expect(seenNotJustSaid(/putting this page together/i)).toHaveLength(1)
      act(() => { vi.advanceTimersByTime(BUILDING_COVER_MAX_MS) })
      // No witness spoke for this document, so it is NOT revealed — it is waited for, in words.
      expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
      expect(screen.queryAllByText(/putting this page together/i)).toHaveLength(0)
      expect(seenNotJustSaid(/opening your app/i)).toHaveLength(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ the citizen`s own Reload over a STANDING `failed` keeps the cover — the same render re-raises it', () => {
    // ★ THE HALF OF THE ESCAPE HATCH THAT IS A REFUSAL, and revision 4 is where it became one. A
    // Reload fetches a new document, so a verdict about the OLD one does not get to pre-judge it —
    // but a verdict that is STILL BEING REPORTED is not about the old document at all: it is the
    // container's word about the app, right now, and the app is broken whichever document the
    // browser fetches. Uncovering here would hand the citizen the framework's error screen as the
    // answer to pressing Reload.
    //
    // It holds because ONE effect derives the cover from `[previewUrl, compileState, nonce]` and
    // re-derives from scratch: the nonce moves, the effect runs, and `failed` sets the cover again
    // in that very render. There is no gap for the citizen to see.
    const { container, rerender } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="failed"
        turnRunning={false}
        reloadNonce={0}
      />,
    )
    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    vouch(container)
    // The premise, guarded: the cover is up over a document that HAD vouched, at the pane's first
    // frame key.
    expect(seenNotJustSaid(/this page can’t open/i)).toHaveLength(1)
    expect(frameKeyOf(container)).toBe(`${SANDBOX_URL}#0.0`)

    // The press. NOTHING ELSE MOVES — the container is still reporting `failed`.
    rerender(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="failed"
        turnRunning={false}
        reloadNonce={1}
      />,
    )

    // ★ THE MUTANT THIS KILLS, NAMED: a SECOND effect keyed on the nonce alone
    // (`useEffect(() => setVerdictCover(null), [externalReloadNonce])`) reads as the obvious way to
    // write "Reload clears the cover" and is the exact hole this file's own docblock records. The
    // standing `failed` is Object.is-equal to itself, so the verdict effect would not run again to
    // re-raise what the second writer just cleared, and the citizen's press would uncover a broken
    // app for good. One effect over both inputs, re-deriving from scratch, is what closes it.
    expect(seenNotJustSaid(/this page can’t open/i)).toHaveLength(1)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    expect(deviceCard(container).className).toMatch(/opacity-0/)
    // …and ONE thing is on screen, as always: the cover, never the wait beside it.
    expect(screen.queryAllByText(/opening your app/i)).toHaveLength(0)
    // ★ LIVENESS, PAIRED: the press REALLY LANDED — a brand-new element is in the frame at the new
    // key, having fetched nothing and vouched for nothing. Without this the assertions above are
    // equally true of a `reloadNonce` prop the pane ignores entirely, which is a cover held for
    // the wrong reason.
    expect(frameKeyOf(container)).toBe(`${SANDBOX_URL}#0.1`)
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('★ …but a Reload over a cover whose signal has gone dark DOES clear it, into the WAIT', () => {
    // ★ THE OTHER HALF, AND THE ONE THE CITIZEN PRESSES FOR. A verdict nobody is re-confirming is
    // a verdict about a document their Reload has just thrown away, so it does not get to
    // pre-judge the one arriving — and what replaces the cover is the honest wait rather than a
    // revealed frame: the new document has vouched for nothing, and the vouch the old one earned
    // went with the key.
    const { container, rerender } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="failed"
        turnRunning={false}
        reloadNonce={0}
      />,
    )
    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    vouch(container)
    expect(seenNotJustSaid(/this page can’t open/i)).toHaveLength(1)

    // The signal goes dark first — the container stops being able to say what it compiled. The
    // cover STAYS, because a `failed` cover never yields to a vouch (the test above), which is
    // what makes the press below the only thing that could have cleared it.
    rerender(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="unknown"
        turnRunning={false}
        reloadNonce={0}
      />,
    )
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    expect(seenNotJustSaid(/putting this page together/i)).toHaveLength(1)

    // The press.
    rerender(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="unknown"
        turnRunning={false}
        reloadNonce={1}
      />,
    )

    // Mutation check: drop `|| reloaded` from the one verdict effect and this goes red — a citizen
    // presses Reload, a fresh document arrives, and the cover raised over its predecessor is still
    // sitting on top of it with no signal left alive that could ever take it down.
    expect(screen.queryAllByText(/this page can’t open/i)).toHaveLength(0)
    expect(screen.queryAllByText(/putting this page together/i)).toHaveLength(0)
    // …and it is the WAIT that takes its place, over a brand-new element that has fetched nothing.
    // ★ THE MUTANT THIS KILLS, NAMED HONESTLY: the old document's vouch is forgotten TWICE over —
    // the render-time reset when the key changes, and `frameVouched` comparing what was recorded
    // against the CURRENT key — so dropping either one alone is masked by the other. Dropping BOTH
    // (a bare `vouched` boolean that survives the remount) goes red right here, with a document
    // nobody has fetched revealed on its predecessor's word.
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    expect(seenNotJustSaid(/opening your app/i)).toHaveLength(1)
    expect(frameKeyOf(container)).toBe(`${SANDBOX_URL}#0.1`)

    // …and then the new document's own compile is read, and it failed too. The reprieve is for ONE
    // document, never a disarming: mutation check — make the Reload a LATCH rather than an edge (a
    // `hasReloaded` flag the verdict effect consults before it raises) and this last pair goes red
    // on its own, with one press buying the citizen an uncovered error screen for the rest of the
    // session.
    rerender(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="failed"
        turnRunning={false}
        reloadNonce={1}
      />,
    )
    expect(seenNotJustSaid(/this page can’t open/i)).toHaveLength(1)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    // LIVENESS, PAIRED: the frame is still mounted under that re-raised cover.
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('★ and the reconnecting cover still outranks every reason to cover, new arm included', () => {
    // PRECEDENCE, WHICH IS EXPRESSED STRUCTURALLY RATHER THAN AS A CHAIN: `showCover` is
    // `showFrame && (covered || workspaceLost)`, and `showFrame` is false whenever the reconnecting
    // card is up. A dev server that crashed mid-build is the case where both reasons are live at
    // once — a `building` verdict on record AND a dead process — and the citizen must be told the
    // one that is actionable, not both at the same time.
    //
    // Mutation check: drop the `showFrame &&` conjunction from `showCover` and this goes red with
    // the compile cover drawn on top of the reconnecting card.
    const { container } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        serving
        previewState="alive"
        compileState="building"
        turnRunning
        reconnecting
      />,
    )

    expect(seenNotJustSaid(/reconnecting to your preview/i)).toHaveLength(1)
    expect(screen.getByRole('status').textContent).toMatch(/reconnecting to your preview/i)
    expect(container.textContent ?? '').not.toMatch(/putting the latest change together/i)
    // The dead frame is replaced rather than covered: the reconnecting card IS the pane while it
    // is up, which is what makes this precedence structural rather than a matter of z-index.
    expect(container.querySelector('iframe')).toBeNull()
  })
})
