/**
 * THE APP PANE — what it is called, how to get past it, and what it says instead.
 *
 * THE TRAP THIS FILE EXISTS FOR: an inertness-only assertion — "the old strings are gone" —
 * passes just as happily on a screen with no start control at all, which would satisfy
 * "exactly one control starts it" with zero. So every no-frame state that can carry a start
 * control is asserted here for the affordance's PRESENCE, not its absence.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import AppPane from '../AppPane'
import { WORKSPACE_RAIL_ID } from '../railId'
import {
  WorkspaceChannelProvider,
  createWorkspaceChannel,
  type WorkspaceChannel,
  type WorkspaceReport,
} from '../workspaceChannel'
import { asDecidedReading, resolveWorkspaceState, type DecidedPreview, type StartOutcome } from '../workspaceState'
import { ApiError } from '../../../utils/apiError'
import type { HandoverStep, PreviewState } from '../../../utils/buildSessionApi'

const api = vi.hoisted(() => ({ relaunchPreview: vi.fn(), handOverWorkspace: vi.fn() }))

vi.mock('../../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/buildSessionApi')>()),
  relaunchPreview: api.relaunchPreview,
  handOverWorkspace: api.handOverWorkspace,
}))

const STARTED = {
  appId: 'a1', previewUrl: 'https://app/', status: 'ready', restoredFromFailedBuild: false, ready: true,
}

beforeEach(() => {
  vi.clearAllMocks()
  api.relaunchPreview.mockResolvedValue(STARTED)
  api.handOverWorkspace.mockResolvedValue(undefined)
})

const reading = (over: Partial<PreviewState> = {}): PreviewState => ({
  state: 'asleep',
  alive: false,
  previewUrl: null,
  occupyingProjectName: null,
  occupyingProjectId: null,
  restorable: null,
  ...over,
})

/** A reading the platform stood behind — the memory an unreadable read falls back to. */
const settled = (over: Partial<PreviewState> = {}): DecidedPreview => {
  const decided = asDecidedReading(reading(over))
  if (decided === null) throw new Error('a settled reading may not be `unknown`')
  return decided
}

function reportFor(
  preview: PreviewState | null,
  startOutcome: StartOutcome | null = null,
  startInFlight = false,
  // WHAT THE PANE WAS SHOWING BEFORE, and it defaults to "nothing has ever been decided" so a
  // caller that does not care about the memory gets the cold-load answer rather than a smuggled
  // one. Every test that exercises decision D3 passes it explicitly.
  lastDecidedPreview: DecidedPreview | null = null,
): WorkspaceReport {
  return {
    state: resolveWorkspaceState({
      preview,
      lastDecidedPreview,
      projectHasSavedBuild: null,
      startOutcome,
      startInFlight,
    }),
    projectId: 'p1',
    onStarted: vi.fn(),
    onStartPending: vi.fn(),
    onStartOutcome: vi.fn(),
    onRefresh: vi.fn(),
    onReclaimRefusal: vi.fn(),
  }
}

/**
 * The pane under a channel primed exactly as a mounted surface would have left it.
 *
 * THE VISIBILITY IS PRIMED TOO, and it has to be: a mounted surface declares it (the project screen
 * unconditionally, a chat for every kind but `plan`), and the channel's resting value is `false`.
 * Leaving it at rest here would test the pane in a state no surface on screen ever puts it in —
 * every state below is one a citizen is LOOKING at.
 */
function renderPane(prime: (channel: WorkspaceChannel) => void, paneVisible = true) {
  const channel = createWorkspaceChannel()
  channel.visible.set(paneVisible)
  prime(channel)
  const result = render(
    <MemoryRouter>
      {/* The shell owns this rail in the product; stood up here so the focus assertion is about
          behaviour, not a missing node. */}
      <div id={WORKSPACE_RAIL_ID}>
        <button type="button">a rail control</button>
      </div>
      <WorkspaceChannelProvider value={channel}>
        <AppPane device="Desktop" reloadNonce={0} />
      </WorkspaceChannelProvider>
    </MemoryRouter>,
  )
  return { ...result, channel }
}

const region = () => screen.getByTestId('app-pane-region')

/** What a mounted surface publishes for the pane's chrome — every field at its resting value. */
const PANE_VIEW = {
  iterating: false, reconnecting: false,
  hasSavedBuild: null,
  previewState: null, occupyingProjectName: null, turnRunning: false,
  compileState: null, workspaceLost: false,
}

afterEach(() => cleanup())

describe('the pane says what it is, and a keyboard can get past it', () => {
  it('is a named region', () => {
    renderPane((c) => c.workspace.set(reportFor(reading())))
    expect(region().getAttribute('aria-label')).toBe('Your app')
  })

  it('★ offers a way past the frame, and it moves focus to the rail', () => {
    // An iframe swallows the tab sequence into a cross-origin document — a way out must exist
    // OUTSIDE it, or someone navigating by keyboard is trapped in the generated app.
    renderPane((c) => c.workspace.set(reportFor(reading())))

    fireEvent.click(screen.getByRole('button', { name: /skip past your app/i }))
    expect(document.activeElement?.id).toBe(WORKSPACE_RAIL_ID)
  })

  it('makes no claim about the framed document itself', () => {
    // The pane says what IT is. What is inside is the generated app's business, and a label
    // promising otherwise would be a claim nothing here can keep.
    renderPane((c) => c.workspace.set(reportFor(reading({ state: 'alive', alive: true }))))
    expect(region().getAttribute('aria-label')).not.toMatch(/accessible|screen reader/i)
  })
})

describe('★ NOT ORPHANED — every no-frame state still offers a way to start the app', () => {
  // These are the states that used to carry LivePreview's `RelaunchAffordance` — the presence
  // check the file docstring's trap requires.
  const restorable = [
    ['asleep, with a saved copy', reading({ state: 'asleep', restorable: true })],
    ['never built, but restorable', reading({ state: 'never_built', restorable: true })],
  ] as const

  for (const [name, preview] of restorable) {
    it(`offers the one start control: ${name}`, () => {
      renderPane((c) => c.workspace.set(reportFor(preview)))
      expect(screen.getByRole('button', { name: /launch application/i })).toBeTruthy()
    })
  }

  it('offers a retry on the one arm that still has one: nothing has ever been decided', () => {
    // THE RETRY SHRANK FROM FOUR ARMS TO ONE, and the three that lost it are the three cards the
    // ten-to-five collapse deleted. `not-painted`, `timed-out` and `start-failed` all described a
    // FETCH rather than a workspace, and each of them landed the citizen on a card whose Try again
    // asked the same question that had just been answered. The reading decides the card now, and
    // the press's own ending rides along as a note.
    renderPane((c) => c.workspace.set(reportFor(reading({ state: 'unknown' }))))
    expect(screen.getByRole('button', { name: /try again/i })).toBeTruthy()
  })

  const nowASavedCard: [string, StartOutcome][] = [
    ['the start did not paint', { kind: 'not-painted' }],
    ['the start timed out', { kind: 'timed-out' }],
    ['the start failed with a reason', { kind: 'failed', reason: 'no image' }],
  ]

  for (const [name, outcome] of nowASavedCard) {
    it(`★ offers the ordinary Launch, not a retry, when ${name}`, () => {
      // THE TRAP THIS FILE EXISTS FOR, applied to the deletion itself: asserting only that the
      // retry is gone would pass on a card with nothing to press at all. So the affordance that
      // replaced it is asserted for its PRESENCE, and the retry's absence beside it.
      renderPane((c) =>
        c.workspace.set(reportFor(reading({ state: 'asleep', restorable: true }), outcome)),
      )

      expect(screen.getByRole('button', { name: /^Launch Application$/ })).toBeTruthy()
      expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
      expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state')).toBe('not-running')
    })
  }

  it('★ and the server`s own reason is on the card, in the line the copy sweep exempts', () => {
    // The reason used to select a card of its own with the sentence in `detail`. `detail` is inside
    // the negative-copy sweep, so one refusal containing "not running" turned a green suite red on
    // a string this client does not control. It rides in `note` now, which the pane draws as its
    // own line — and the card is the ordinary saved one, because the next step is unchanged.
    renderPane((c) =>
      c.workspace.set(
        reportFor(reading({ state: 'asleep', restorable: true }), {
          kind: 'failed',
          reason: 'The container is not running.',
        }),
      ),
    )

    expect(screen.getByTestId('app-pane-note').textContent).toBe('The container is not running.')
    expect(screen.getByTestId('app-pane-empty').textContent).toContain('Your app is saved.')
  })

  it('offers the REMEDY, not a retry, when another project holds the workspace', () => {
    renderPane((c) =>
      c.workspace.set(
        reportFor(reading({ state: 'slot_taken', occupyingProjectName: 'Roster', occupyingProjectId: 'p-9' })),
      ),
    )
    expect(screen.getByRole('button', { name: /open “Roster”/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
  })

  it('offers NOTHING for the two states where nothing can be pressed', () => {
    for (const preview of [reading({ state: 'never_built', restorable: false }), reading({ state: 'starting' })]) {
      const { unmount } = renderPane((c) => c.workspace.set(reportFor(preview)))
      expect(screen.queryByRole('button', { name: /launch application|try again|open /i })).toBeNull()
      // Liveness: it still SAYS something. An absence assertion alone passes on a blank pane.
      expect(screen.getByTestId('app-pane-empty').textContent?.length).toBeGreaterThan(10)
      unmount()
    }
  })
})

describe('the seam is the resolved address, not a URL that happens to be in hand', () => {
  it('frames the host once an address is resolved, and shows no sentence over it', () => {
    const { container } = renderPane((c) => {
      c.workspace.set(reportFor(reading({ state: 'alive', alive: true })))
      c.address.set({ url: 'https://app.example/', status: 'ready', serving: true, projectId: 'p1' })
      c.project.set('p1')
      c.visible.set(true)
    })

    expect(container.querySelector('iframe')).toBeTruthy()
    expect(screen.queryByTestId('app-pane-empty')).toBeNull()
  })

  it('mounts NO iframe of its own when there is no address', () => {
    // Calling `LivePreview` from here would build a second host — see `AppPaneHost`.
    const { container } = renderPane((c) => c.workspace.set(reportFor(reading())))
    expect(container.querySelector('iframe')).toBeNull()
  })

  it('says nothing at all when nobody has computed a state', () => {
    // A surface mounted outside a workspace, or one still resolving its project. Inventing a
    // sentence here would be a second author for the one thing this design gives a single one.
    const { container } = renderPane(() => {})
    expect(screen.queryByTestId('app-pane-empty')).toBeNull()
    expect(container.querySelector('iframe')).toBeNull()
  })
})

describe('one author for every pane sentence', () => {
  it('renders the map`s headline and detail verbatim', () => {
    renderPane((c) => c.workspace.set(reportFor(reading({ state: 'asleep', restorable: true }))))

    const empty = screen.getByTestId('app-pane-empty')
    expect(empty.textContent).toContain('Your app is saved.')
    expect(empty.textContent).toContain('It stays running while you work, so you only do this once.')
  })

  it('★ draws the board\'s mark above the headline on the three states that have one', () => {
    // `NothingBuilt`, `PreviewOff` and `PreviewStarting` put a 30px #9AA5B1 glyph above the
    // headline — the only thing that reads a blank half-screen as deliberate, not broken.
    const withGlyph: [string, PreviewState][] = [
      ['never-built', reading({ state: 'never_built', restorable: false })],
      ['not-running', reading({ state: 'asleep', restorable: true })],
      ['starting', reading({ state: 'starting' })],
    ]

    for (const [name, preview] of withGlyph) {
      const { unmount } = renderPane((c) => c.workspace.set(reportFor(preview)))
      const glyph = screen.getByTestId('app-pane-glyph')
      expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state'), name).toBe(name)
      expect(glyph.getAttribute('width')).toBe('30')
      // Decorative: the headline underneath already says it in words.
      expect(glyph.getAttribute('aria-hidden')).toBe('true')
      unmount()
    }
  })

  it('★ and EVERY board that draws a card draws a mark — no bare cards left', () => {
    // ★ THIS ASSERTION USED TO BE ITS OPPOSITE, and the inversion is the change. The lookup carried
    // SEVEN nulls, on the reading that the canvas had never drawn those boards — but four of the
    // seven were the start-outcome and second-held arms the collapse deleted outright, and the last
    // two drew a real card with a real headline and real buttons and stood there bare. A blank
    // half-screen with no mark reads as a page that failed to load, which is the one thing the
    // glyph exists to prevent, so the two survivors take the mark THIS PRODUCT ALREADY USES for
    // what they are rather than borrowing one of the three boards' or inventing a vocabulary.
    const everyBoard: [string, PreviewState | null][] = [
      ['never-built', reading({ state: 'never_built', restorable: false })],
      ['not-running', reading({ state: 'asleep', restorable: true })],
      ['starting', reading({ state: 'starting' })],
      ['held-by-another-project', reading({ state: 'slot_taken', occupyingProjectName: 'Roster', occupyingProjectId: 'p-9' })],
      ['could-not-read', reading({ state: 'unknown' })],
    ]

    for (const [name, preview] of everyBoard) {
      const { unmount } = renderPane((c) => c.workspace.set(reportFor(preview)))
      expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state'), name).toBe(name)
      const glyph = screen.getByTestId('app-pane-glyph')
      expect(glyph.getAttribute('width'), name).toBe('30')
      expect(glyph.getAttribute('aria-hidden'), name).toBe('true')
      unmount()
    }
  })

  it('★ and `running` is the one entry with no mark, because it draws no card at all', () => {
    // The null that is an ANSWER rather than a gap. The frame IS the state, so there is nothing
    // for a mark to sit above — asserted as the absence of the whole board, not of the glyph, or
    // it would pass on a card that rendered bare.
    const { container } = renderPane((c) => {
      c.workspace.set(reportFor(reading({ state: 'alive', alive: true })))
      c.address.set({ url: 'https://app.example/', status: 'ready', serving: true, projectId: 'p1' })
      c.project.set('p1')
      c.visible.set(true)
    })

    expect(screen.queryByTestId('app-pane-empty')).toBeNull()
    expect(screen.queryByTestId('app-pane-glyph')).toBeNull()
    // LIVENESS: the pane really did render, and what it rendered is the app.
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('never says what the app is NOT', () => {
    for (const preview of [
      reading({ state: 'asleep', restorable: true }),
      reading({ state: 'never_built', restorable: false }),
      reading({ state: 'starting' }),
      reading({ state: 'unknown' }),
    ]) {
      const { unmount } = renderPane((c) => c.workspace.set(reportFor(preview)))
      const text = region().textContent ?? ''
      expect(text).not.toMatch(/not running/i)
      expect(text).not.toMatch(/\bstopped\b/i)
      expect(text).not.toMatch(/unavailable/i)
      // "preview" is the developer's word for the thing; the person's word is their app.
      expect(text).not.toMatch(/\bpreview\b/i)
      unmount()
    }
  })
})

/**
 * The shared risk across these cases is reading `address.url` as the whole seam. It is not: the
 * resolver also returns a STATUS independent of the URL, and a held address can outlive the
 * container behind it — so a URL alone is neither necessary nor sufficient evidence that
 * something is serving.
 */
describe('the seam is the address AND the state, not the URL alone', () => {
  it('★ frames the LOADING state — a status with no URL yet, which is a first build coming up', () => {
    // See `previewAddress.ts`'s own docblock: a provisioning build has a status and no URL yet,
    // and gating on the URL alone put "We could not check on your app." in front of a citizen
    // watching their first build.
    //
    // Mutation receipt: change the gate back to `address.url !== null` and this goes red.
    const { container } = renderPane((c) => {
      c.workspace.set(reportFor(null))
      c.address.set({ url: null, status: 'provisioning', serving: false, projectId: 'p1' })
      c.project.set('p1')
      c.visible.set(true)
      // A surface mid-build publishes its pane view; the host's own "nothing to host at all" early
      // return is about a project nobody has opened a conversation in, which is not this.
      c.pane.set(PANE_VIEW)
    })

    // The host is mounted — it is what draws the wait — and no sentence is drawn over it.
    expect(screen.queryByTestId('app-pane-empty')).toBeNull()
    expect(container.querySelector('[data-testid="app-pane"]')).toBeTruthy()
  })

  it('★ stops framing a HELD address once the workspace says nothing is serving', () => {
    // A URL stays held after the container behind it has stopped. Framing it regardless meant an
    // app that went to sleep showed a card saying "nothing is lost" with no way to bring it back.
    renderPane((c) => {
      c.workspace.set(reportFor(reading({ state: 'asleep', restorable: true })))
      c.address.set({ url: 'https://app.example/', status: 'ready', serving: true, projectId: 'p1' })
      c.project.set('p1')
      c.visible.set(true)
    })

    expect(document.querySelector('iframe')).toBeNull()
    expect(screen.getByRole('button', { name: /launch application/i })).toBeTruthy()
  })

  it('★ an UNKNOWN never pulls a framed app off the screen', () => {
    // The rule the whole preview reshape exists for: a read that decided nothing must not retire a
    // frame somebody is looking at. `could-not-read` is deliberately absent from the veto set.
    const { container } = renderPane((c) => {
      c.workspace.set(reportFor(reading({ state: 'unknown' })))
      c.address.set({ url: 'https://app.example/', status: 'ready', serving: true, projectId: 'p1' })
      c.project.set('p1')
      c.visible.set(true)
    })

    expect(container.querySelector('iframe')).toBeTruthy()
    expect(screen.queryByTestId('app-pane-empty')).toBeNull()
  })

  it('★ DECISION D3 — a blip over a RUNNING app leaves the frame exactly where it is', () => {
    // The pane-level half of "an unreadable read never changes the pane". The map renders the last
    // settled reading, so this arrives here as `running` and the frame is never even asked to come
    // down — which is the point: the invariant is kept by the value the pane receives not moving,
    // rather than by this component carving an exception into its own frame rule.
    //
    // MUTATION RECEIPT: drop the map's `?? lastDecidedPreview` fallback and this goes red — the
    // report becomes `could-not-read` and the card replaces the app.
    const { container } = renderPane((c) => {
      c.workspace.set(
        reportFor(reading({ state: 'unknown' }), null, false, settled({ state: 'alive', alive: true })),
      )
      c.address.set({ url: 'https://app.example/', status: 'ready', serving: true, projectId: 'p1' })
      c.project.set('p1')
      c.visible.set(true)
    })

    expect(container.querySelector('iframe')).toBeTruthy()
    expect(screen.queryByTestId('app-pane-empty')).toBeNull()
  })

  it('★ and a blip over a STANDING CARD leaves that card exactly where it is', () => {
    // The other direction of the same rule, and the one a name-only assertion would miss: the
    // citizen keeps the sentence AND the button they were looking at, rather than watching
    // "Your app is saved. [Launch Application]" turn into "We could not check on your app.
    // [Try again]" because one poll did not come back.
    renderPane((c) =>
      c.workspace.set(
        reportFor(reading({ state: 'unknown' }), null, false, settled({ state: 'asleep', restorable: true })),
      ),
    )

    expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state')).toBe('not-running')
    expect(screen.getByRole('button', { name: /^Launch Application$/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
  })
})

/**
 * ★ THE FRAME MOUNTS IF AND ONLY IF THE APP HAS BEEN WATCHED TO ANSWER A REQUEST.
 *
 * This block is the whole citizen-visible point of the change, so it is asserted as a RULE over
 * every state rather than as a handful of examples. The wire's `alive` is now gated on a serving
 * stamp written where something watched the app ANSWER; it used to mean only that a container had
 * been SCHEDULED. The eight seconds a citizen spent reading "This app isn't running right now"
 * INSIDE this pane on 2026-09-10 are the distance between those two meanings.
 *
 * The two carve-outs below are NOT exceptions to the rule — they are its precondition. Only a
 * VERDICT moves the frame, so a pane with no verdict in hand does not act.
 */
describe('★ the frame mounts if and only if the state is RUNNING', () => {
  /** An address in hand on every one of these, or the assertions would be about the URL. */
  const withAnAddress = (preview: PreviewState | null) => (c: WorkspaceChannel) => {
    c.workspace.set(reportFor(preview))
    c.address.set({ url: 'https://app.example/', status: 'ready', serving: true, projectId: 'p1' })
    c.project.set('p1')
    c.visible.set(true)
    c.pane.set(PANE_VIEW)
  }

  it('★ mounts it on `running`, and on nothing else a citizen is ever shown', () => {
    const withheld: [string, PreviewState][] = [
      ['never-built', reading({ state: 'never_built', restorable: false })],
      ['not-running', reading({ state: 'asleep', restorable: true })],
      ['starting', reading({ state: 'starting' })],
      ['held-by-another-project', reading({ state: 'slot_taken', occupyingProjectName: 'Roster', occupyingProjectId: 'p-9' })],
    ]

    for (const [name, preview] of withheld) {
      const { container, unmount } = renderPane(withAnAddress(preview))
      expect(container.querySelector('iframe'), `${name} framed the app`).toBeNull()
      // LIVENESS, and it is what stops this passing on a pane that failed to render at all: the
      // board really is up, really says which state it is, and really says something.
      expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state'), name).toBe(name)
      expect(screen.getByTestId('app-pane-empty').textContent?.length ?? 0, name).toBeGreaterThan(10)
      unmount()
    }

    const { container } = renderPane(withAnAddress(reading({ state: 'alive', alive: true })))
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(screen.queryByTestId('app-pane-empty')).toBeNull()
  })

  it('★ `starting` is the arm the measured defect lived in, and it is the one that changed', () => {
    // At 48s on 2026-09-10 the container had been created and the registry said `ready`, so the
    // poll answered `alive`, the frame mounted on the apps router's 404 page, and four seconds
    // later a screen reader was told the preview was live. The same instant now reads `starting`,
    // because nothing has watched the app answer — and this pane draws a wait instead.
    renderPane(withAnAddress(reading({ state: 'starting' })))

    expect(document.querySelector('iframe')).toBeNull()
    const board = screen.getByTestId('app-pane-empty')
    expect(board.getAttribute('data-workspace-state')).toBe('starting')
    expect(board.textContent).toContain('Getting your app ready.')
    // AND THE WAIT SAYS IT IS A WAIT, to a reader who cannot see the glyph.
    expect(board.getAttribute('aria-busy')).toBe('true')
  })

  it('the two carve-outs are the rule`s precondition: no verdict, no move', () => {
    // `could-not-read` is a read that decided nothing at a moment when nothing had ever been
    // decided — every surface's own first render — and a null report is the window every hop
    // between two surfaces has, because the publisher clears on unmount. Withholding on either
    // would unmount the host on every navigation into a running app: a cross-origin `src`
    // re-issued, and the citizen's form entries, scroll position and open tab thrown away.
    for (const preview of [reading({ state: 'unknown' }), null] as const) {
      const { container, unmount } = renderPane((c) => {
        if (preview !== null) c.workspace.set(reportFor(preview))
        c.address.set({ url: 'https://app.example/', status: 'ready', serving: true, projectId: 'p1' })
        c.project.set('p1')
        c.visible.set(true)
        c.pane.set(PANE_VIEW)
      })
      expect(container.querySelector('iframe')).toBeTruthy()
      unmount()
    }
  })
})

/**
 * ★ DECISION D2 — THERE IS NO PATIENCE BUTTON, AND TIME DOES NOT GROW ONE.
 *
 * The design wanted a "Launch Application" to appear after 150 seconds of waiting so the wait was
 * never a dead end. It is not shipped, because of where that press would land: `relaunch_preview`'s
 * cold arm tears the live container down before restoring the last saved bundle, and the situation
 * such a button exists for — a start whose observer was lost — is exactly the situation that takes
 * the cold arm. The button would be most dangerous at the precise moment it appeared.
 *
 * The map's own sweep proves no action for any INPUT. This proves the other half, at the one
 * surface that has a clock: the pane counts elapsed time from the moment the wait begins, so this
 * is where a timed affordance would have to be built.
 */
describe('★ no timed action ever appears in the wait — decision D2', () => {
  let clock = 0
  beforeEach(() => {
    clock = 0
    vi.useFakeTimers()
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('★ five minutes into a build there is still nothing to press', () => {
    renderPane((c) => c.workspace.set(reportFor(reading({ state: 'starting' }))))
    expect(screen.queryByRole('button', { name: /launch application|try again|open |^stop /i })).toBeNull()

    // Well past the 150s the design proposed, and past the 300-second accelerated window too.
    act(() => {
      clock += 300_000
      vi.advanceTimersByTime(300_000)
    })

    expect(screen.queryByRole('button', { name: /launch application|try again|open |^stop /i })).toBeNull()
    // ★ LIVENESS, AND IT IS THE WHOLE VALUE OF THIS TEST. An absence assertion after a clock
    // advance passes just as happily when the clock never moved, when the board unmounted, or when
    // the component crashed inside a boundary. The counter proves all three: it is rendered, it is
    // on the wait's own board, and it really did see five minutes go by.
    expect(screen.getByTestId('app-pane-elapsed').textContent).toBe('5m 00s so far')
    expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state')).toBe('starting')
    expect(screen.getByTestId('app-pane-empty').textContent).toContain('Getting your app ready.')
  })

  it('★ and the wait`s wording never changes either, however long it runs', () => {
    // A patience button would arrive with a sentence beside it. The rule is that the headline and
    // the detail are chosen once and never named a duration, so a wait that starts saying something
    // new is the same defect wearing different clothes.
    renderPane((c) => c.workspace.set(reportFor(reading({ state: 'starting' }))))
    const opening = screen.getByTestId('app-pane-empty').textContent ?? ''

    act(() => {
      clock += 300_000
      vi.advanceTimersByTime(300_000)
    })

    const later = screen.getByTestId('app-pane-empty').textContent ?? ''
    expect(opening).toContain('Getting your app ready.Setting up somewhere for it to run.')
    expect(later).toContain('Getting your app ready.Setting up somewhere for it to run.')
    // The ONLY thing that moved is the elapsed count, which is a measured fact rather than a claim.
    expect(opening).toContain('0s so far')
    expect(later).toContain('5m 00s so far')
  })
})

describe('the column a plan chat does not get', () => {
  // AppPane used to read the report and the address but never the VISIBILITY, so its `flex-1`
  // section claimed half the window on a plan chat nobody had asked to start an app from.

  // WHOLE CLASSES, NOT SUBSTRINGS. `min-w-0` contains `w-0`, so a `toContain` here passes on the
  // very layout this block exists to forbid.
  const paneClasses = (container: HTMLElement) =>
    (container.querySelector('[data-testid="app-pane-region"]')?.className ?? '').split(/\s+/)

  it('★ takes no width when no surface asks for the pane', () => {
    const { container } = renderPane((c) => {
      c.workspace.set(reportFor(reading()))
    }, false)

    expect(paneClasses(container)).toContain('w-0')
    expect(paneClasses(container)).not.toContain('flex-1')
  })

  it('★ zeroes its HEIGHT too, for the stacked layout below the threshold', () => {
    // Above the threshold this column sits in a flex row, where a zero width is enough. Below it
    // the same element is a child of a flex COLUMN, and a width of zero leaves a full-height band
    // of nothing under the rail — the stacked layout's version of the same bug.
    const { container } = renderPane((c) => {
      c.workspace.set(reportFor(reading()))
    }, false)

    expect(paneClasses(container)).toContain('h-0')
  })

  it('★ leaves the accessibility tree, so nothing in it is reachable by keyboard', () => {
    // `visibility:hidden` is the mechanism and jsdom loads no stylesheet, so the `aria-hidden`
    // beside it is what this assertion can see — and it is the half that a screen reader obeys.
    // Without it the skip control and any start button stay announced on a screen that draws none.
    renderPane((c) => c.workspace.set(reportFor(reading())), false)

    expect(screen.getByTestId('app-pane-region').getAttribute('aria-hidden')).toBe('true')
    expect(screen.queryByRole('button', { name: /skip past your app/i })).toBeNull()
  })

  it('is HIDDEN, never unmounted — a running app survives the move to a plan chat', () => {
    // Unmounting would re-issue the frame's `src` on the way back — see `AppPaneHost`.
    const { container } = renderPane((c) => {
      c.workspace.set(reportFor(reading({ state: 'alive', alive: true })))
      c.address.set({ url: 'https://app.example/', status: 'ready', serving: true, projectId: 'p1' })
      c.project.set('p1')
      c.pane.set(PANE_VIEW)
    }, false)

    expect(container.querySelector('iframe')).toBeTruthy()
    expect(screen.getByTestId('app-pane-region').className).toContain('invisible')
  })

  it('takes the width back the moment a surface asks for it', () => {
    const { container } = renderPane((c) => c.workspace.set(reportFor(reading())), true)

    expect(paneClasses(container)).toContain('flex-1')
    expect(paneClasses(container)).not.toContain('w-0')
  })
})

describe('the movement between the two layouts', () => {
  const paneClasses = (container: HTMLElement) =>
    (container.querySelector('[data-testid="app-pane-region"]')?.className ?? '').split(/\s+/)

  /** A build chat with a running app framed: the state a citizen actually leaves FROM. */
  const framed = (c: WorkspaceChannel) => {
    c.workspace.set(reportFor(reading({ state: 'alive', alive: true })))
    c.address.set({ url: 'https://app.example/', status: 'ready', serving: true, projectId: 'p1' })
    c.project.set('p1')
    c.pane.set(PANE_VIEW)
  }

  it('★ slides out at full width and only then collapses', async () => {
    // Applying the leave keyframe to the collapsed arm would change nothing — an element at
    // `w-0 invisible` cannot be watched fading — which is why the column holds its size for the
    // length of the animation.
    const { container, channel } = renderPane(framed, true)
    expect(paneClasses(container)).not.toContain('animate-pane-leave')

    act(() => channel.visible.set(false))

    expect(paneClasses(container)).toContain('animate-pane-leave')
    expect(paneClasses(container)).toContain('flex-1')
    expect(paneClasses(container)).not.toContain('w-0')
    // Gone to a reader immediately, even while it is still on screen for the eye.
    expect(screen.getByTestId('app-pane-region').getAttribute('aria-hidden')).toBe('true')

    await waitFor(() => expect(paneClasses(container)).toContain('w-0'))
    expect(paneClasses(container)).not.toContain('animate-pane-leave')
    // AND THE APP WAS NEVER TOUCHED BY ANY OF IT — "nothing about the app is stopped or reloaded,
    // it is only taken off the screen." This is also the liveness half: every class assertion
    // above would pass just as happily against a host that unmounted the frame.
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('★ stays out of the keyboard’s reach for the WHOLE leave, not only once it has gone', async () => {
    // The column holds its size during the leave, so `visibility:hidden` — which takes a subtree
    // out of the tab order — cannot land yet. Without `inert` covering that gap, the pane reads as
    // gone (aria-hidden) but stays one Tab away: a WCAG 4.1.2 violation on a whole application.
    //
    // Asserted as the attribute, not a focus simulation: jsdom implements no part of `inert` (no
    // reflected property, no refused `focus()`), so the attribute is the only mechanism here that
    // a real browser actually obeys.
    const { container, channel } = renderPane(framed, true)
    // A pane somebody is looking at is reachable, or the assertion below proves nothing.
    expect(region().hasAttribute('inert')).toBe(false)

    act(() => channel.visible.set(false))

    expect(paneClasses(container)).toContain('animate-pane-leave')
    expect(paneClasses(container)).not.toContain('invisible')
    expect(region().hasAttribute('inert')).toBe(true)
    // LIVENESS, and the reason a subtree attribute is the right shape: the two focusable things
    // inside the region are the skip control and the frame itself, and both are covered by one
    // attribute rather than by a list this test would have to keep up with.
    expect(container.querySelector('iframe')?.closest('[inert]')).toBe(region())
    expect(region().querySelector('button')?.textContent).toMatch(/skip past your app/i)

    // AND IT IS STILL UNREACHABLE ONCE THE HOLD ENDS, where `visibility:hidden` takes over.
    await waitFor(() => expect(paneClasses(container)).toContain('w-0'))
    expect(region().hasAttribute('inert')).toBe(true)
  })

  it('★ becomes reachable again the moment the pane is back', async () => {
    // The other direction, and the one that would turn this fix into a worse bug than the one it
    // fixes: an `inert` that never lifted would leave a citizen looking at their app unable to
    // reach anything in it, with nothing on the screen to explain why.
    const { container, channel } = renderPane(framed, true)

    act(() => channel.visible.set(false))
    await waitFor(() => expect(paneClasses(container)).toContain('w-0'))
    act(() => channel.visible.set(true))

    expect(region().hasAttribute('inert')).toBe(false)
    expect(container.querySelector('iframe')?.closest('[inert]')).toBeNull()
  })

  it('★ carries the return half of the pair when the pane comes back', async () => {
    const { container, channel } = renderPane(framed, true)
    expect(container.querySelector('[data-testid="app-pane"]')?.className).toMatch(/animate-pane-return/)

    act(() => channel.visible.set(false))
    await waitFor(() => expect(paneClasses(container)).toContain('w-0'))
    act(() => channel.visible.set(true))

    // The return interrupts a departure rather than queueing behind it.
    expect(paneClasses(container)).not.toContain('animate-pane-leave')
    expect(container.querySelector('[data-testid="app-pane"]')?.className).toMatch(/animate-pane-return/)
  })

  it('★ a pane that was never on screen does not animate its way to nothing', () => {
    // Every plan chat opened cold, and the project screen before anything is built. There is no
    // departure to draw, so there is no hold either — the column is at rest on its first frame.
    const { container } = renderPane(framed, false)

    expect(paneClasses(container)).toContain('w-0')
    expect(paneClasses(container)).not.toContain('animate-pane-leave')
  })

  it('★ both halves are suppressed for a reader who asked for less motion', () => {
    // Asserted against the STYLESHEET because that is where the suppression lives, and jsdom
    // loads no stylesheet: nothing else in the suite would notice the media block being deleted.
    // Resolved from the vitest root (`portal/`), not from `import.meta.url`: under vite the
    // module's own URL is not a `file:` one, so `new URL(…, import.meta.url)` cannot be read.
    const css = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf8')
    const reduced = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'))
    expect(reduced.length).toBeGreaterThan(0)

    // Each utility is looked up in whatever rule carries it, rather than in a rule matching the
    // two of them ADJACENT. The old regex demanded `.animate-pane-leave, .animate-pane-return {`
    // literally, so a later change that suppressed the same way by adding `.animate-spin`,
    // `.animate-pulse` and `.animate-bounce` to this very selector list — turned this guard red
    // while the guarantee it protects was strictly widened. A guard that breaks when the thing it
    // guards gets stronger is a guard that gets deleted.
    for (const utility of ['animate-pane-leave', 'animate-pane-return']) {
      const rule = reduced.match(new RegExp(String.raw`([^{}]*\.${utility}\b[^{}]*)\{([^}]*)\}`))
      expect(rule, `no rule in the reduce-motion block names .${utility}`).not.toBeNull()
      expect(rule?.[2]).toMatch(/animation:\s*none/)
    }
    // LIVENESS: the two utilities the block suppresses are the two the components apply, so the
    // rule cannot go on matching class names nothing renders.
    const column = readFileSync(resolve(process.cwd(), 'src/components/workspace/AppPane.tsx'), 'utf8')
    const host = readFileSync(resolve(process.cwd(), 'src/components/workspace/AppPaneHost.tsx'), 'utf8')
    expect(column).toContain('animate-pane-leave')
    expect(host).toContain('animate-pane-return')
  })
})

/**
 * ★ A BLOCKED PROJECT TAKES ITS WORKSPACE BACK — the pane half.
 *
 * WHAT THIS BLOCK IS WRITTEN AGAINST: the unit's three structural traps, each of which passes
 * review and fails in a browser:
 *
 *  1. A take-back that reports `onStartPending` UNMOUNTS ITS OWN BUTTON. `resolveWorkspaceState`
 *     answers `gettingReady()` on an in-flight press, that arm offers no action, and `NoFrame`
 *     draws a control only where there is one. The pane also stops framing on `starting`. So the
 *     in-flight assertions below assert the ARM as well as the button, because "the buttons are
 *     still there" and "the pane is still held" are two different failures.
 *  2. THE DIALOG OWNS `busy` AND `error` ITSELF, and its `run()` catches every rejection into one
 *     alert while staying mounted. If the take-back's handlers rejected, that alert would be what
 *     a citizen reads on all five of the take-back's endings and none of the pane states below
 *     would be reachable. Every failing ending here asserts the dialog is GONE.
 *  3. THE REOPENED DIALOG IS A NEW MOUNT. It takes focus in a mount-time effect, so a dialog
 *     updated in place would leave a keyboard user parked where the busy state put them while the
 *     copy in front of them started naming a different project.
 */
describe('★ taking the workspace back', () => {
  const HELD: Partial<PreviewState> = {
    state: 'slot_taken', occupyingProjectName: 'Car pool', occupyingProjectId: 'pA', restorable: true,
  }

  /** The refusal `POST /relaunch` raises when another project holds the one workspace. */
  const blocked = (over: Record<string, unknown> = {}) =>
    new ApiError('“Car pool” is still open.', 409, 'sandbox_reclaim_blocked', {
      projectId: 'pA', projectName: 'Car pool', dirty: true, building: false, ...over,
    })

  /** A promise a test opens and closes by hand, for asserting on the middle of a sequence. */
  function deferred<T>() {
    let settle!: (value: T) => void
    let fail!: (err: unknown) => void
    const promise = new Promise<T>((res, rej) => { settle = res; fail = rej })
    return { promise, settle, fail }
  }

  /**
   * The pane, held by another project, with the report's handlers exposed as spies.
   *
   * `onStartPending` IS WIRED TO THE MAP, exactly as both real publishers wire it — the project
   * hook's `reportStartPending` and the chat surface's `setStartPending` both feed
   * `resolveWorkspaceState`. Without that the arm assertion below would be vacuous: a harness
   * holding one frozen state cannot show a take-back unmounting its own button, which is trap 1
   * above.
   */
  function heldPane(startOutcome: StartOutcome | null = null) {
    const channel = createWorkspaceChannel()
    const report: WorkspaceReport = {
      ...reportFor(reading(HELD), startOutcome),
      onStartPending: vi.fn((pending: boolean) => {
        act(() => channel.workspace.set({ ...report, state: reportFor(reading(HELD), startOutcome, pending).state }))
      }),
    }
    channel.visible.set(true)
    channel.workspace.set(report)
    const rendered = render(
      <MemoryRouter>
        <div id={WORKSPACE_RAIL_ID} />
        <WorkspaceChannelProvider value={channel}>
          <AppPane device="Desktop" reloadNonce={0} />
        </WorkspaceChannelProvider>
      </MemoryRouter>,
    )
    return { ...rendered, report, channel }
  }

  const takeBack = () => screen.getByRole('button', { name: /^Stop “Car pool” and open this app instead$/ })
  const openHolder = () => screen.getByRole('button', { name: /^Open “Car pool”$/ })
  const dialog = () => screen.queryByRole('dialog')

  /**
   * Press the take-back and wait for the server's refusal to raise the question.
   *
   * ONLY THE FIRST ANSWER IS SCRIPTED HERE — `mockRejectedValueOnce` queues ahead of whatever base
   * answer the test has set, so the SECOND relaunch (the one that closes the sequence) is the
   * caller's to choose. `beforeEach` makes that a successful start unless a test says otherwise.
   */
  async function askTheQuestion(over: Record<string, unknown> = {}) {
    api.relaunchPreview.mockRejectedValueOnce(blocked(over))
    const pane = heldPane()
    fireEvent.click(takeBack())
    await screen.findByRole('dialog')
    return pane
  }

  it('★ the held arm draws TWO controls, and the first is untouched', async () => {
    heldPane()
    // The take-back was added BESIDE this control, never in place of it: the open-holder button
    // keeps the label and the behaviour it already had.
    expect(openHolder()).toBeTruthy()
    expect(takeBack()).toBeTruthy()
  })

  /**
   * ★ THIS TEST USED TO PIN THE OPPOSITE, and it passed while the product was broken.
   *
   * It read "the unattributed arm still draws neither" and asserted BOTH controls absent — which
   * was true of `held-unattributed`, the state that was a documented dead end: a card that named
   * the problem, named no remedy, and left the citizen nothing to press. Merging it into
   * `held-by-another-project` was supposed to end that, and the map does its half — with no holder
   * to open, the take-back becomes `action` rather than `secondAction`.
   *
   * The pane did NOT do its half. `AppPane` handed the sequence only to the second slot, on the
   * reasonable-looking assumption that a take-back is always the second control, and
   * `StartAppControl` draws nothing at all for a take-back it has no sequence for. So the dead end
   * survived one slot along — and this test went on passing, because it was asserting the very
   * absence the bug produces. An assertion that agrees with the defect is not a guard.
   *
   * MUTATION-CHECKED: drop `takeBack={takeBack}` from the LEADING `StartAppControl` in
   * `AppPane.tsx` and this test goes red. That is the mutant that shipped.
   */
  it('★ the unattributed arm degrades the sentence but KEEPS the remedy', () => {
    renderPane((c) => c.workspace.set(reportFor(reading({ state: 'slot_taken' }))))

    // The remedy is offered, unnamed — a missing holder is a reason to say less, not to do less.
    const remedy = screen.getByRole('button', { name: /^Stop the other project and open this app instead$/ })
    expect(remedy).toBeTruthy()
    // And there is nothing to open, so the go-to is correctly absent rather than empty-quoted.
    expect(screen.queryByRole('button', { name: /^Open /i })).toBeNull()
    // Nor does the sentence invent a name or leave a hollow pair of quotes where one belongs.
    const board = screen.getByTestId('app-pane-empty').textContent ?? ''
    expect(board).toContain('Another project is using your workspace.')
    expect(board).not.toMatch(/[“"]\s*[”"]/)
  })

  /** THE INVARIANT ITSELF, over both arms: a held card is never a dead end. */
  it('★ no held arm, named or not, leaves the citizen with nothing to press', () => {
    for (const held of [
      reading({ state: 'slot_taken', occupyingProjectName: 'Car pool', occupyingProjectId: 'pA' }),
      reading({ state: 'slot_taken' }),
    ]) {
      const view = renderPane((c) => c.workspace.set(reportFor(held)))
      expect(screen.queryAllByRole('button', { name: /^Stop|^Open /i }).length).toBeGreaterThan(0)
      // LIVENESS: the board really rendered, so a crash cannot masquerade as a pass here.
      expect(screen.getByTestId('app-pane-empty').textContent?.length).toBeGreaterThan(10)
      view.unmount()
    }
  })

  it('★ pressing it asks THIS project`s own start, and the server`s refusal is what opens the dialog', async () => {
    const { report } = await askTheQuestion()

    expect(api.relaunchPreview).toHaveBeenCalledWith({ projectId: 'p1' })
    // And the question leads with what the citizen is trying to do, not with the obstacle.
    expect(dialog()?.textContent).toContain('Car pool')
    // ★ TRAP 1. The whole point of not routing this through the in-flight channel.
    expect(report.onStartPending).not.toHaveBeenCalled()
  })

  for (const [dirty, expected, forbidden] of [
    [true, /has changes that are not saved yet/i, null],
    [false, /Everything saved in “Car pool” stays exactly as it is/i, /unsaved|not saved yet/i],
    [null, /may have changes that are not saved yet/i, null],
  ] as const) {
    it(`reaches the dialog's ${String(dirty)} copy arm — from the refusal, which is the only place that answer exists`, async () => {
      await askTheQuestion({ dirty })
      expect(dialog()?.textContent).toMatch(expected)
      if (forbidden) expect(dialog()?.textContent).not.toMatch(forbidden)
      // The clean arm offers no Save button for work that does not exist.
      const save = screen.queryByRole('button', { name: /^Save “Car pool” and stop it$/ })
      expect(save === null).toBe(dirty === false)
    })
  }

  for (const [name, button, save] of [
    ['saving first', /^Save “Car pool” and stop it$/, true],
    ['without saving', /^Stop “Car pool” without saving$/, false],
  ] as const) {
    it(`★ confirming ${name} stops the holder and brings this app up — one press, no turn`, async () => {
      const { report } = await askTheQuestion()

      fireEvent.click(screen.getByRole('button', { name: button }))

      await waitFor(() =>
        expect(api.handOverWorkspace).toHaveBeenCalledWith(
          expect.objectContaining({ projectId: 'pA' }),
          save,
          {},
          expect.any(Function),
        ),
      )
      // The relaunch that closes the sequence, and the URL the pane frames.
      await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledTimes(2))
      expect(report.onStarted).toHaveBeenCalledWith('https://app/')
      expect(report.onStartOutcome).toHaveBeenCalledWith(null)
      // ★ TRAP 1 again, on the path that actually starts an app: the pane must never be told a
      // start is pending, or it un-frames itself and unmounts the control mid-sequence.
      expect(report.onStartPending).not.toHaveBeenCalled()
      await waitFor(() => expect(dialog()).toBeNull())
    })
  }

  // --- the shared-view arm: "closed", never "stopped" (#198 round 3) ------------------------

  it('★ giving up a SHARED view says "closed", never "stopped" — nothing of the colleague`s ever ran here', async () => {
    api.handOverWorkspace.mockResolvedValue(undefined)
    const { report } = await askTheQuestion({ isSharedView: true, dirty: false })

    // The clean-stop copy for `dirty: false` offers only this one button, unsuffixed — see
    // `copyFor`'s own `discard: "Stop ${incumbent}"` on that arm.
    fireEvent.click(screen.getByRole('button', { name: /^Stop “Car pool”$/ }))

    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledTimes(2))
    expect(report.onStarted).toHaveBeenCalledWith('https://app/')
    // Never the ordinary-arm sentence — nothing was stopped or saved, only given up.
    expect(await screen.findByText('You closed your view of “Car pool”. Your app has the workspace now.')).toBeTruthy()
    expect(screen.queryByText(/was stopped/)).toBeNull()
  })

  it('★ a rejected give-up on a SHARED view never claims their project was stopped', async () => {
    // `giveUpSharedView` never stops anything, on either outcome, and `handOverWorkspace`
    // narrates the shared arm only AFTER it succeeds — so a rejection here must reach the pane
    // having emitted no narration step at all, the same as a rejection at `stopping` itself.
    const reason = 'Could not close the other app just now.'
    api.handOverWorkspace.mockImplementation(async () => {
      throw new ApiError(reason, 503, 'shared_release_failed')
    })
    const { report } = await askTheQuestion({ isSharedView: true, dirty: false })

    fireEvent.click(screen.getByRole('button', { name: /^Stop “Car pool”$/ }))

    await waitFor(() =>
      expect(report.onStartOutcome).toHaveBeenCalledWith({
        kind: 'take-back-failed', reason, stoppedHolder: null,
      }),
    )
  })

  it('★ ENDING 1 — a stop that failed dismisses the dialog and hands the pane the server`s sentence', async () => {
    // `buildSessionApi.ts` authors the two-minute ceiling sentence. It arrives here verbatim, and
    // `stoppedHolder` is null because nothing was stopped.
    const ceiling = 'The other app is still saving its work. Nothing has changed — give it a moment and try again.'
    api.handOverWorkspace.mockImplementation(async (_id, _save, _deps, narrate: (s: HandoverStep) => void) => {
      narrate('stopping')
      throw new ApiError(ceiling, 409, 'stop_did_not_settle')
    })
    const { report } = await askTheQuestion()

    fireEvent.click(screen.getByRole('button', { name: /^Stop “Car pool” without saving$/ }))

    await waitFor(() =>
      expect(report.onStartOutcome).toHaveBeenCalledWith({
        kind: 'take-back-failed', reason: ceiling, stoppedHolder: null,
      }),
    )
    // ★ TRAP 2. The pane is the single reporting surface, so the dialog is gone and its own
    // "That did not work. Please try again." alert never appeared.
    await waitFor(() => expect(dialog()).toBeNull())
    expect(screen.queryByRole('alert')).toBeNull()
    // LIVENESS: the pane is still on the held arm with both ways out, which is the outcome this
    // ending specifies.
    expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state')).toBe('held-by-another-project')
    expect(takeBack()).toBeTruthy()
  })

  for (const [ending, at, reason] of [
    ['ENDING 3 — the save failed', 'saving', 'Could not save your work'],
    ['ENDING 4 — the release failed', 'releasing', 'Could not close the other workspace'],
  ] as const) {
    it(`★ ${ending}: the holder is down, and the outcome says so`, async () => {
      api.handOverWorkspace.mockImplementation(async (_id, _save, _deps, narrate: (s: HandoverStep) => void) => {
        narrate('stopping')
        if (at === 'releasing') narrate('saving')
        narrate(at)
        throw new ApiError(reason, 500)
      })
      const { report } = await askTheQuestion()

      fireEvent.click(screen.getByRole('button', { name: /^Save “Car pool” and stop it$/ }))

      await waitFor(() =>
        expect(report.onStartOutcome).toHaveBeenCalledWith({
          kind: 'take-back-failed', reason, stoppedHolder: 'Car pool',
        }),
      )
      await waitFor(() => expect(dialog()).toBeNull())
      expect(screen.queryByRole('alert')).toBeNull()
    })
  }

  it('★ ENDING 2 — the slot was freed and the start failed: the outcome carries the stopped holder', async () => {
    api.relaunchPreview.mockRejectedValue(new ApiError('the image could not be pulled', 503))
    const pane = await askTheQuestion()

    fireEvent.click(screen.getByRole('button', { name: /^Stop “Car pool” without saving$/ }))

    await waitFor(() =>
      expect(pane.report.onStartOutcome).toHaveBeenCalledWith({
        kind: 'take-back-failed', reason: 'the image could not be pulled', stoppedHolder: 'Car pool',
      }),
    )
    // The reading is stale the moment the release lands, so the pane asks again — without which it
    // would sit on a hand-over that is over instead of reaching `start-failed`.
    expect(pane.report.onRefresh).toHaveBeenCalled()
    await waitFor(() => expect(dialog()).toBeNull())

    // And that outcome, over the reading that follows it, is the pane this ending specifies.
    //
    // ★ IT IS THE ORDINARY SAVED CARD NOW, not a `start-failed` board of its own. The situation,
    // the honest headline and the next step were always identical to SAVED's — giving it a
    // differently-shaped screen told the citizen something had changed that had not. What survives
    // is the one thing they could not find out any other way: what we did to the other project,
    // and then the server's own words, both in the line the negative-copy sweep exempts.
    cleanup()
    renderPane((c) =>
      c.workspace.set(
        reportFor(reading({ state: 'asleep', restorable: true }), {
          kind: 'take-back-failed', reason: 'the image could not be pulled', stoppedHolder: 'Car pool',
        }),
      ),
    )
    expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state')).toBe('not-running')
    expect(screen.getByTestId('app-pane-note').textContent).toBe(
      '“Car pool” was stopped. the image could not be pulled',
    )
    expect(screen.getByRole('button', { name: /^Launch Application$/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Try again$/ })).toBeNull()
  })

  it('★ ENDING 5 — another tab takes the freed slot: the question reopens, remounted, and takes focus', async () => {
    api.relaunchPreview.mockRejectedValue(blocked({ projectId: 'pB', projectName: 'Roster' }))
    const { report } = await askTheQuestion()

    fireEvent.click(screen.getByRole('button', { name: /^Stop “Car pool” without saving$/ }))

    // The choice screen again, with new data — never the dialog's generic caught-error alert.
    const reopened = await screen.findByRole('button', { name: /^Save “Roster” and stop it$/ })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(report.onStartOutcome).not.toHaveBeenCalledWith(expect.objectContaining({ kind: 'take-back-failed' }))
    // ★ TRAP 3. A NEW MOUNT, proved by where the focus is: the dialog takes it in a mount-time
    // effect, so an in-place update would have left it on the card the busy state parked it on
    // while the copy silently started naming another project. Drop the `key` and this goes red.
    await waitFor(() => expect(document.activeElement).toBe(reopened))
  })

  it('★ while it runs: both controls inert, the take-back busy and renamed, and the arm still HELD', async () => {
    const hold = deferred<void>()
    api.handOverWorkspace.mockImplementation(async (_id, _s, _d, narrate: (s: HandoverStep) => void) => {
      narrate('stopping')
      await hold.promise
    })
    await askTheQuestion()

    fireEvent.click(screen.getByRole('button', { name: /^Stop “Car pool” without saving$/ }))
    // The dialog is up and narrating; the pane behind it is what these assertions are about.
    await screen.findByTestId('reclaim-step')

    const working = screen.getByRole('button', { name: /^Taking your workspace back…$/ })
    expect(working.getAttribute('aria-busy')).toBe('true')
    expect(working.getAttribute('aria-disabled')).toBe('true')
    // BOTH, which is a fact about a pair of siblings and so cannot live in either of them.
    expect(openHolder().getAttribute('aria-disabled')).toBe('true')
    // ★ TRAP 1, asserted as the ARM rather than as the button:
    // a take-back on the in-flight channel reaches `starting`, which offers no action at all and
    // un-frames the pane. The harness feeds `onStartPending` back through the map (see `heldPane`),
    // so reporting one here really does move this arm.
    expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state')).toBe('held-by-another-project')

    await act(async () => { hold.settle(); await Promise.resolve() })
  })

  it('the pane itself is a polite region, mounted before it has anything to say and on arms with no buttons', () => {
    // ★ THE DEFECT THIS TEST NOW GUARDS. This asserted the region was `takeBack().parentElement`
    // — the ROW THE TWO CONTROLS SIT IN — which was true and was the defect: the region lived
    // inside the block that renders the buttons, so any state with `action: null` had no live
    // region at all. `starting` is exactly such a state, and it is the one wait in the product
    // with nothing to press, so a sandbox start announced NOTHING. What this test now rejects is
    // a region scoped to the controls rather than to the pane.
    //
    // The rule still holds and is why the region exists here at all: `LivePreview` keeps the
    // pane's other permanent region and is not mounted on these arms, so without this one the
    // wait would pass in silence. Never a second `sr-only` copy of a sentence already on screen —
    // the two regions divide the pane, and this one owns the states with no app in them.
    heldPane()
    const region = screen.getByTestId('app-pane-live')
    expect(region.getAttribute('role')).toBe('status')
    expect(region.getAttribute('aria-live')).toBe('polite')
    // It is the pane, not the controls: the take-back row is INSIDE it rather than being it.
    expect(region.contains(takeBack())).toBe(true)
    expect(takeBack().parentElement?.getAttribute('role')).not.toBe('status')

    // ★ THE ARM THE MOVE WAS FOR. `starting` offers no action, so under the old scoping it had no
    // region on any screen. Asserted with liveness — the board really rendered — so a pane that
    // failed to mount cannot pass by having no region either.
    cleanup()
    renderPane((c) => c.workspace.set(reportFor(reading({ state: 'starting' }))))
    expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state')).toBe('starting')
    const starting = screen.getByTestId('app-pane-live')
    expect(starting.getAttribute('role')).toBe('status')
    expect(starting.getAttribute('aria-live')).toBe('polite')
  })

  it('★ unmounting mid-sequence updates nothing and crashes nothing — and the server sequence finishes', async () => {
    // The citizen clicks another project during the up-to-two-minute stop wait. The existing
    // hand-over never needed this: its last act is a navigate that unmounts the surface anyway.
    const hold = deferred<void>()
    api.handOverWorkspace.mockImplementation(async (_id, _s, _d, narrate: (s: HandoverStep) => void) => {
      narrate('stopping')
      await hold.promise
    })
    const errors: unknown[] = []
    const onError = (e: ErrorEvent) => errors.push(e.error)
    window.addEventListener('error', onError)
    const { unmount, report } = await askTheQuestion()

    fireEvent.click(screen.getByRole('button', { name: /^Stop “Car pool” without saving$/ }))
    await screen.findByTestId('reclaim-step')
    unmount()

    await act(async () => { hold.settle(); await Promise.resolve() })
    // The rest of the sequence ran server-side regardless — the relaunch that closes it fired.
    await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledTimes(2))
    expect(errors).toEqual([])
    // The report outlives the pane and is still told, exactly as the start path's own note says.
    expect(report.onStarted).toHaveBeenCalledWith('https://app/')
    window.removeEventListener('error', onError)
  })

  it('cancelling changes nothing anywhere, and leaves both controls live', async () => {
    await askTheQuestion()

    fireEvent.click(screen.getByRole('button', { name: /^Cancel$/ }))

    await waitFor(() => expect(dialog()).toBeNull())
    expect(api.handOverWorkspace).not.toHaveBeenCalled()
    expect(takeBack().getAttribute('aria-disabled')).toBe('false')
    expect(openHolder().getAttribute('aria-disabled')).toBe('false')
  })

  /**
   * ★ THE SEQUENCE IS OWNED BY A PROJECT, BECAUSE THE PANE OUTLIVES ONE.
   *
   * `AppPane` is a SIBLING of the Outlet in `WorkspaceShell`, never a child of it — that is the
   * whole reason leaving a build chat for the project screen does not reload the running app. The
   * cost is that a move from one project to another runs NO cleanup here: the same `useState`s
   * carry straight over, and until this fix nothing in `useTakeBack` named a project. A citizen who
   * opened the take-back on A and then went to B was left reading A's hand-over question over B's
   * pane, or holding B's control in a busy state belonging to A's sequence.
   *
   * BOTH TESTS PUBLISH B ONTO THE SAME CHANNEL AND NEVER RE-RENDER THE TREE, which is exactly what
   * the router does — a re-render with a new report and no unmount. Rendering a second pane would
   * test a remount, which is the one case that was never broken.
   */
  describe('★ moving to another project does not inherit this one`s take-back', () => {
    /** What the router publishes on arriving at another project — itself held, by someone else. */
    const arriveAtTheOtherProject = (channel: WorkspaceChannel) =>
      act(() =>
        channel.workspace.set({
          ...reportFor(
            reading({
              state: 'slot_taken',
              occupyingProjectName: 'Roster',
              occupyingProjectId: 'pB',
              restorable: true,
            }),
          ),
          projectId: 'p2',
        }),
      )

    const othersTakeBack = () => screen.getByRole('button', { name: /^Stop “Roster” and open this app instead$/ })
    const othersOpenHolder = () => screen.getByRole('button', { name: /^Open “Roster”$/ })

    it('★ the question does not follow the citizen — A`s dialog closes and B draws its own arm', async () => {
      const { channel } = await askTheQuestion()
      expect(dialog()).toBeTruthy()

      arriveAtTheOtherProject(channel)

      // A's question named A's holder and asked what to do with A's unsaved work. Standing over B
      // it is a modal about a project nobody is looking at, whose Save and Stop buttons act on a
      // container the citizen did not come here to touch.
      expect(dialog()).toBeNull()
      // LIVENESS. The absence above is a dialog that closed, not a pane that stopped rendering:
      // B really did arrive, on its own held arm, with both of its own ways out.
      expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state')).toBe(
        'held-by-another-project',
      )
      expect(othersTakeBack().getAttribute('aria-disabled')).toBe('false')
      expect(othersOpenHolder().getAttribute('aria-disabled')).toBe('false')
    })

    it('★ nor does the busy flag, and A`s sequence cannot write back into B', async () => {
      const hold = deferred<void>()
      api.handOverWorkspace.mockImplementation(async (_id, _s, _d, narrate: (s: HandoverStep) => void) => {
        narrate('stopping')
        await hold.promise
      })
      // The relaunch that CLOSES A's sequence is refused by a third project. Chosen deliberately:
      // it is the one ending that opens a dialog rather than reporting an outcome, so a sequence
      // that could still write would raise a question over B's pane naming a project B has never
      // heard of — the leak's worst shape.
      api.relaunchPreview.mockRejectedValue(blocked({ projectId: 'pC', projectName: 'Gate pass' }))
      const { channel } = await askTheQuestion()
      fireEvent.click(screen.getByRole('button', { name: /^Stop “Car pool” without saving$/ }))
      await screen.findByTestId('reclaim-step')

      arriveAtTheOtherProject(channel)

      // B's control is idle: it has not renamed itself to "Taking your workspace back…", it claims
      // no busy state, and it is pressable — as is its neighbour, which A's sequence had made inert.
      expect(othersTakeBack().getAttribute('aria-busy')).toBe('false')
      expect(othersTakeBack().getAttribute('aria-disabled')).toBe('false')
      expect(othersOpenHolder().getAttribute('aria-disabled')).toBe('false')
      expect(dialog()).toBeNull()

      // AND A'S SEQUENCE FINISHING CHANGES NONE OF IT. It still runs to the end server-side — the
      // relaunch that closes it fires, exactly as the unmount case above — but every state write
      // names the project it began under, so none of them lands on B.
      await act(async () => {
        hold.settle()
        await Promise.resolve()
      })
      await waitFor(() => expect(api.relaunchPreview).toHaveBeenCalledTimes(2))
      expect(dialog()).toBeNull()
      expect(othersTakeBack().getAttribute('aria-busy')).toBe('false')
      expect(othersTakeBack().getAttribute('aria-disabled')).toBe('false')
      // LIVENESS again, after the settle: still B's own held arm, still both of B's controls.
      expect(screen.getByTestId('app-pane-empty').getAttribute('data-workspace-state')).toBe(
        'held-by-another-project',
      )
    })
  })
})

describe('★ the wait counter measures the wait, it does not count its own ticks', () => {
  // This line is the ONLY thing the pane can say truthfully about how long a start has taken —
  // there is no progress bar, so honesty is the whole feature. `setSeconds(was => was + 1)` counted
  // how many times the interval FIRED, and a browser throttles a hidden tab's timers (to 1/s,
  // and to 1/MINUTE after ~5 minutes hidden). A citizen who switches tabs during a two-minute
  // start and comes back was told a number minutes short of the truth.
  const starting = () => reportFor(reading(), null, true)

  // THE CLOCK IS DRIVEN BY HAND, because the counter reads `performance.now()` — monotonic, so
  // an NTP correction cannot make the wait count backwards — and `vi.setSystemTime` moves only
  // the Date clock. Holding the reading here is also what lets the throttled-tab case exist at
  // all: real time has to advance while the interval does NOT fire, which no timer API models.
  let clock = 0
  beforeEach(() => {
    clock = 0
    vi.useFakeTimers()
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  /** Advance real time AND fire the interval, the ordinary case. */
  const tick = (ms: number) => {
    clock += ms
    vi.advanceTimersByTime(ms)
  }

  it('starts at zero and advances with the clock', () => {
    renderPane((c) => c.workspace.set(starting()))
    expect(screen.getByTestId('app-pane-elapsed').textContent).toBe('0s so far')

    act(() => { tick(3_000) })
    expect(screen.getByTestId('app-pane-elapsed').textContent).toBe('3s so far')
  })

  it('★ tells the truth after a throttled tab has swallowed most of the ticks', () => {
    renderPane((c) => c.workspace.set(starting()))
    act(() => { tick(3_000) })

    // The tab goes to the background: real time keeps passing, the interval does not fire.
    // Then it comes forward and gets ONE tick.
    act(() => { clock += 117_000 })
    act(() => { tick(1_000) })

    // 121 seconds of wall clock, 5 firings. The number is the wait, not the firings.
    expect(screen.getByTestId('app-pane-elapsed').textContent).toBe('2m 01s so far')
  })

  it('is not announced — it sits inside the pane`s polite region', () => {
    // A counter that ticks inside a live region is a screen reader reading a number every
    // second for two minutes.
    renderPane((c) => c.workspace.set(starting()))
    expect(screen.getByTestId('app-pane-elapsed').getAttribute('aria-live')).toBe('off')
  })
})
