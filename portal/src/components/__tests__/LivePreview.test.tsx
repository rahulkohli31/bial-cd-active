/**
 * LivePreview — four states, not one boolean (R16, R17, R18 / C3 §8.3).
 *
 * The pane used to take `previewReclaimed: boolean`, fed from `!alive`, which meant a Redis
 * blip, a sleeping workspace, a sibling project holding the one-per-user slot and a project
 * nobody ever built all arrived here identically and were all rendered as "Preview
 * unavailable" — a sentence that describes a platform fault for three situations that are not
 * one, and for a fourth that was only ever a failed question.
 *
 * These tests drive the component through the SAME parser the browser uses
 * (`fetchPreviewState`), so a backend that stops sending `state`, or a parser that starts
 * coercing it, fails here rather than in production. That is the round trip this file can
 * honestly assert; the wire values themselves are pinned in
 * `backend/tests/api/v1/build_sessions/test_preview_state.py`.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, fireEvent, screen } from '@testing-library/react'
import LivePreview from '../LivePreview'
import { fetchPreviewState } from '../../utils/buildSessionApi'
import type { PreviewState } from '../../utils/buildSessionApi'

afterEach(cleanup)

const SANDBOX_URL = 'https://app-xyz.example.azurecontainerapps.io/'

/** Put a real server body through the real client parser — no hand-built props. */
async function asTheBrowserSeesIt(body: unknown): Promise<PreviewState> {
  const res = { ok: true, json: async () => body } as unknown as Response
  return await fetchPreviewState('proj-1', { fetchImpl: async () => res })
}

/** The pane, wired from a parsed server verdict exactly as BuilderPage wires it. */
function paneFor(state: PreviewState, extra: Record<string, unknown> = {}) {
  return render(
    <LivePreview
      previewUrl={SANDBOX_URL}
      status="ended"
      completedLive
      previewState={state.state}
      occupyingProjectName={state.occupyingProjectName}
      hasSavedBuild={state.restorable}
      {...extra}
    />,
  )
}

describe('LivePreview — the four states a workspace can be in', () => {
  it('ASLEEP reads as sleep, not failure, and promises the work back (AE9)', async () => {
    const verdict = await asTheBrowserSeesIt({
      state: 'asleep',
      alive: false,
      previewUrl: null,
      restorable: true,
    })
    const { container } = paneFor(verdict)

    expect(container.querySelector('iframe')).toBeNull() // stop framing a dead origin
    expect(container.textContent).toMatch(/workspace is asleep/i)
    expect(container.textContent).toMatch(/nothing is lost/i)
    // The words that made ordinary housekeeping read as a fault.
    expect(container.textContent).not.toMatch(/preview unavailable/i)
    expect(container.textContent).not.toMatch(/reclaimed/i)
  })

  it('ASLEEP does NOT promise a restore the server cannot make (restorable === false)', async () => {
    // The blocker this test exists for: the copy used to say "your work is saved" / "nothing is
    // lost" UNCONDITIONALLY. `restorable === false` is a reachable backend state — the server
    // holds neither a recovery slot nor a saved bundle — and reassuring a builder there is the
    // one lie this unit exists to stop.
    const verdict = await asTheBrowserSeesIt({
      state: 'asleep',
      alive: false,
      previewUrl: null,
      restorable: false,
    })
    const { container } = paneFor(verdict)

    expect(container.textContent).toMatch(/workspace is asleep/i)
    expect(container.textContent).not.toMatch(/nothing is lost/i)
    expect(container.textContent).not.toMatch(/your work is saved/i)
    expect(container.textContent).toMatch(/start fresh/i)
  })

  it('ASLEEP claims nothing when the store was unreachable (restorable === null)', async () => {
    // The tri-state's whole point: `null` is "we could not ask", which is not "yes" and not
    // "no". Guessing either way here is what `dirty` and `hasSavedBuild` already refuse to do.
    const verdict = await asTheBrowserSeesIt({
      state: 'asleep',
      alive: false,
      previewUrl: null,
      restorable: null,
    })
    const { container } = paneFor(verdict)

    expect(container.textContent).toMatch(/workspace is asleep/i)
    expect(container.textContent).not.toMatch(/nothing is lost/i)
    expect(container.textContent).not.toMatch(/start fresh/i)
  })

  it('SLOT_TAKEN names the project standing in the way', async () => {
    const verdict = await asTheBrowserSeesIt({
      state: 'slot_taken',
      alive: false,
      previewUrl: null,
      occupyingProjectName: 'Baggage Reconciliation',
      restorable: true,
    })
    const { container } = paneFor(verdict)

    expect(container.textContent).toMatch(/another project has your workspace/i)
    expect(container.textContent).toMatch(/Baggage Reconciliation/)
    expect(container.textContent).toMatch(/nothing is lost/i)
  })

  it('SLOT_TAKEN with no attributable project still explains itself, naming nobody', async () => {
    // A ghost container. Naming the wrong project in a sentence about somebody's unsaved work
    // is worse than naming none, so the server sends null and the copy stays general.
    const verdict = await asTheBrowserSeesIt({
      state: 'slot_taken',
      alive: false,
      previewUrl: null,
      occupyingProjectName: null,
      restorable: false,
    })
    const { container } = paneFor(verdict)

    expect(container.textContent).toMatch(/another project is using your build workspace/i)
    expect(container.textContent).toMatch(/another project has your workspace/i)
  })

  it('NEVER_BUILT says nothing has been built, and promises no restore', async () => {
    const verdict = await asTheBrowserSeesIt({
      state: 'never_built',
      alive: false,
      previewUrl: null,
      restorable: false,
    })
    const { container } = paneFor(verdict, { onRelaunch: vi.fn() })

    expect(container.textContent).toMatch(/nothing has been built here yet/i)
    expect(screen.queryByRole('button', { name: /bring it back/i })).toBeNull()
  })

  it('ALIVE keeps framing the app', async () => {
    const verdict = await asTheBrowserSeesIt({
      state: 'alive',
      alive: true,
      previewUrl: SANDBOX_URL,
      restorable: true,
    })
    const { container } = paneFor(verdict)

    expect(container.querySelector('iframe')).toBeTruthy()
    expect(container.textContent).not.toMatch(/asleep/i)
  })

  it('UNKNOWN renders "unknown", never "gone" — it leaves the frame exactly where it was', async () => {
    // THE defect. A registry read that failed decided nothing; the old boolean turned it into
    // "your preview is gone" and pulled a live app off the screen.
    //
    // Mutation-check: add `'unknown'` to `notServing` in LivePreview and this goes red on the
    // iframe assertion.
    const verdict = await asTheBrowserSeesIt({
      state: 'unknown',
      alive: false,
      previewUrl: null,
      restorable: null,
    })
    const { container } = paneFor(verdict)

    expect(container.querySelector('iframe')).toBeTruthy()
    expect(container.textContent).not.toMatch(/preview unavailable/i)
    expect(container.textContent).not.toMatch(/asleep/i)
    // Said out loud rather than hidden — politely, and without touching the pane. It waits its
    // turn behind the framing wait, which outranks it: "your app is opening" is the more
    // useful sentence while that is still true.
    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    expect(screen.getByRole('status').textContent).toMatch(/could not check on your preview/i)
  })

  it('STARTING (U13) parses as its own state, not a coerced "unknown", and is never treated as gone', async () => {
    // The closed-list defect this state exists to catch: an unwidened `PREVIEW_LIFE_STATES`
    // would fall through `asPreviewLifeState`'s fallback straight to 'unknown' (`alive` is
    // false), which is a confident-sounding "nothing to report" for a fact the server DID
    // report — a start is already under way. Distinguished from `unknown` at the assertion,
    // not merely by the parse: `unknown` gets "could not check"; this state must not.
    const verdict = await asTheBrowserSeesIt({
      state: 'starting',
      alive: false,
      previewUrl: null,
      restorable: null,
    })
    expect(verdict.state).toBe('starting')

    const { container } = paneFor(verdict)
    // Not routed through the "gone" card: a start in flight is the opposite of gone, and
    // `GONE_TITLE`/`goneBody` would tell a citizen to "send a prompt" over a container the
    // platform is already bringing up.
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(container.textContent).not.toMatch(/asleep|nothing has been built here yet|another project has your workspace/i)
    fireEvent.load(container.querySelector('iframe') as HTMLIFrameElement)
    expect(screen.getByRole('status').textContent).not.toMatch(/could not check on your preview/i)
  })
})

describe('LivePreview — a reclaimed container is never an error (R17)', () => {
  it.each(['asleep', 'slot_taken', 'never_built'] as const)(
    'renders NO danger-styled alert for %s',
    async (state) => {
      const verdict = await asTheBrowserSeesIt({
        state,
        alive: false,
        previewUrl: null,
        restorable: true,
      })
      const { container } = paneFor(verdict, { onRelaunch: vi.fn() })

      // `role="alert"` is reserved for things that actually went wrong (a failed relaunch, a
      // failed save). A container the platform took back on purpose is not one of them.
      expect(screen.queryByRole('alert')).toBeNull()
      const card = container.querySelector('[data-testid="preview-unavailable-card"]')
      expect(card).toBeTruthy()
      expect(card?.querySelector('[class*="danger"]')).toBeNull()
      expect(card?.className).not.toMatch(/danger/)
    },
  )

  it('a genuine dead dev server still says "Preview unavailable" — the two are NOT merged', async () => {
    // The reconnect cap expiring is a real failure: the process died and did not come back.
    // Softening THAT would be the opposite mistake, so the card keeps its original wording and
    // its severed-connection icon, distinguished by `data-preview-state`.
    vi.useFakeTimers()
    try {
      const { container } = render(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ended"
          completedLive
          reconnecting
          previewState="alive"
          hasSavedBuild
        />,
      )
      await vi.advanceTimersByTimeAsync(20001)
      const card = container.querySelector('[data-testid="preview-unavailable-card"]')
      expect(card?.getAttribute('data-preview-state')).toBe('disconnected')
      expect(card?.textContent).toMatch(/preview unavailable/i)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does NOT route a sleeping workspace through "Reconnecting…" — that promises a recovery nobody is bringing', async () => {
    const verdict = await asTheBrowserSeesIt({ state: 'asleep', alive: false, restorable: true })
    const { container } = paneFor(verdict, { status: 'ready', completedLive: false, reconnecting: true })

    expect(container.textContent).not.toMatch(/reconnecting to your preview/i)
    expect(container.textContent).toMatch(/workspace is asleep/i)
  })
})

describe('LivePreview — the restore offer is driven by `restorable` (R18)', () => {
  it('INERTNESS GUARD: the four start buttons are gone, and the explanation is not', async () => {
    // AE10 used to be asserted here by pressing "Bring it back". That control moved — R3 says
    // exactly ONE control starts the app, and four scattered through this file's placeholder arms
    // is the same requirement satisfied five times over, in a vocabulary the client replaced
    // ("preview" is the developer's word; the person's word is their app).
    //
    // TWO HALVES, AND THE SECOND IS WHY THIS IS NOT JUST A DELETION. The absence assertion below
    // would pass just as happily on a pane that renders nothing at all, so it is paired with the
    // copy that must survive: this placeholder still has to SAY what happened. The affordance's
    // new home is asserted where it lives — `AppPane.test.tsx` pins that every no-frame state
    // still offers a reachable way to start the app, which is the half that would otherwise go
    // missing silently.
    const verdict = await asTheBrowserSeesIt({
      state: 'asleep',
      alive: false,
      previewUrl: null,
      restorable: true,
    })
    expect(verdict.restorable).toBe(true)

    const onRelaunch = vi.fn()
    const { container } = paneFor(verdict, { onRelaunch })

    // Liveness: the pane still explains the state.
    expect(container.textContent).toMatch(/workspace is asleep/i)
    // Inertness: no start control, under any of its retired labels, and the prop it was wired to
    // is never called from here.
    expect(screen.queryByRole('button', { name: /bring it back/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(onRelaunch).not.toHaveBeenCalled()
  })

  it('claims NOTHING when `restorable` is null — the object store was unreachable', async () => {
    // Tri-state discipline: `null` is UNKNOWN, and the reassuring answer is the one you must
    // never give on someone else's behalf. No button, and no "there is nothing to relaunch"
    // either — the pane simply does not say.
    const verdict = await asTheBrowserSeesIt({
      state: 'asleep',
      alive: false,
      previewUrl: null,
      restorable: null,
    })
    expect(verdict.restorable).toBeNull()

    const { container } = paneFor(verdict, { onRelaunch: vi.fn() })

    expect(screen.queryByRole('button', { name: /bring it back/i })).toBeNull()
    expect(container.textContent).not.toMatch(/nothing to relaunch/i)
    expect(container.textContent).not.toMatch(/no saved build/i)
  })

  it('a missing `restorable` field parses to null, not to false', async () => {
    // The parser is where a coercion would do its damage silently.
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
})

describe('LivePreview — one persistent status region announces every state', () => {
  it('the region is mounted even when the pane has nothing to say', async () => {
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

  it('routes the RESTORE through the labelled wait, announced — not through a terminal card', async () => {
    // AE9's "behind a labelled wait, and at no point is an error shown". The labelled wait
    // already existed (`showRestoring`); what it never had was a voice — it carried
    // `aria-busy`, which announces nothing at all.
    const { container } = render(
      <LivePreview previewUrl={null} status="ended" relaunching hasSavedBuild onRelaunch={vi.fn()} />,
    )

    expect(container.textContent).toMatch(/restoring your app/i)
    expect(screen.getByRole('status').textContent).toMatch(/restoring your app/i)
    expect(container.querySelector('[data-testid="preview-ended-card"]')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('announces a sleeping workspace politely, and an app that came back', async () => {
    const asleep = await asTheBrowserSeesIt({ state: 'asleep', alive: false, restorable: true })
    const view = paneFor(asleep)
    expect(screen.getByRole('status').textContent).toMatch(/workspace is asleep/i)

    const alive = await asTheBrowserSeesIt({
      state: 'alive',
      alive: true,
      previewUrl: SANDBOX_URL,
      restorable: true,
    })
    view.rerender(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ended"
        completedLive
        previewState={alive.state}
        occupyingProjectName={alive.occupyingProjectName}
        hasSavedBuild={alive.restorable}
      />,
    )
    // The frame has not loaded in jsdom yet, so the honest line is the wait — not a claim
    // that an app nobody has seen is live.
    expect(screen.getByRole('status').textContent).toMatch(/starting your app/i)

    const frame = view.container.querySelector('iframe')
    fireEvent.load(frame as HTMLIFrameElement)
    expect(screen.getByRole('status').textContent).toMatch(/preview is live/i)
  })
})


// U4/R7 — the retraction, on the surface the citizen is actually looking at.
describe('a workspace found reverted while the tab sat idle', () => {
  // ★ IT OUTRANKS EVERY OTHER COVER SENTENCE, running turn or not. It is the only one that is a
  // fact about the WORKSPACE rather than about a compile; the others all describe an app that is
  // still there. "Getting your app ready" over a workspace that has been wiped is the exact
  // false-progress claim this plan exists to remove.
  //
  // Mutation check: move `workspaceLost` below `turnRunning` in the cover's ternary and the
  // during-a-turn case goes red.
  it.each([
    ['idle', false],
    ['during a turn', true],
  ])('says the app stopped running and promises the restore (%s)', (_when, turnRunning) => {
    render(
      <LivePreview
        previewUrl="https://app.example.test/"
        status="ended"
        completedLive
        previewState="alive"
        compileState="clean"
        turnRunning={turnRunning}
        workspaceLost
      />,
    )

    // TWO NODES, DELIBERATELY: the visible cover and the pane's permanent live region, which
    // announces the same sentence. `getAllBy` rather than `getBy` for that reason — and asserting
    // on BOTH is the point, because a cover nobody hears is half the retraction.
    expect(screen.getAllByText(/stopped running and needs to be brought back/i)).toHaveLength(2)
    // IT PROMISES A RESTORE, and unlike every other sentence in this component it is entitled to:
    // the next turn's integrity gate puts the app back from the last durable copy.
    expect(screen.getAllByText(/we\u2019ll restore it/i).length).toBeGreaterThan(0)
  })

  it('leaves the ordinary idle wording alone when the workspace is fine', () => {
    render(
      <LivePreview
        previewUrl="https://app.example.test/"
        status="ended"
        completedLive
        previewState="alive"
        compileState="building"
      />,
    )

    expect(screen.queryByText(/stopped running/i)).toBeNull()
    // LIVENESS: the cover really is up, so the absence above is a choice of wording rather than
    // a component that rendered nothing at all.
    expect(screen.getAllByText(/Getting your app ready/i).length).toBeGreaterThan(0)
  })
})

describe('LivePreview — what Plan F removed, and what it deliberately did not', () => {
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

  it('keeps everything from the frame inward untouched', async () => {
    // The removal was of NO-FRAME chrome. The security seam, the cover, the frame key and the
    // device widths are the parts of this component the workspace redesign explicitly does not
    // touch, and a sweep that took them with the placeholders would be a silent regression on the
    // one thing this file is genuinely load-bearing for.
    const source = (await import('../LivePreview?raw')).default as string

    expect(source).toMatch(/e\.source/)          // the inbound-message gate, on origin AND source
    expect(source).toMatch(/sandbox=/)           // the sandbox token list
    expect(source).toMatch(/const frameKey =/)   // the frame's identity
    // The device WIDTHS are still read here; the TABLE moved up to the toolbar row with the
    // control that picks them (plan 002, U2), so this asserts the import rather than the literal —
    // two copies of it is the drift this guard exists to prevent, not one copy in a new file.
    expect(source).toMatch(/import \{ DEVICES, type DeviceName \} from '\.\/workspace\/WorkspaceToolbar'/)
    expect(source).toMatch(/DEVICES\[device\]\.width/)
    expect(source).toMatch(/setCovered/)         // the cover that holds on an unknown
  })
})
