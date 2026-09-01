/**
 * THE APP PANE (Plan F, U4) — what it is called, how to get past it, and what it says instead.
 *
 * ═══ THE TRAP THIS FILE EXISTS FOR ═══
 *
 * U4 removes the four start affordances that lived inside `LivePreview`'s no-frame placeholders.
 * Those were, until this plan, the ONLY way to bring a stopped app back. An inertness-only
 * assertion — "the old strings are gone" — passes just as happily on a screen with no start control
 * at all, which would satisfy R3's "exactly one control starts it" with zero. So every no-frame
 * state that used to carry one is asserted here for the affordance's PRESENCE, not its absence.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import AppPane from '../AppPane'
import { WORKSPACE_RAIL_ID } from '../WorkspaceShell'
import {
  WorkspaceChannelProvider,
  createWorkspaceChannel,
  type WorkspaceChannel,
  type WorkspaceReport,
} from '../workspaceChannel'
import { resolveWorkspaceState, type StartOutcome } from '../workspaceState'
import type { PreviewState } from '../../../utils/buildSessionApi'

vi.mock('../../../utils/buildSessionApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../utils/buildSessionApi')>()),
  relaunchPreview: vi.fn(async () => ({
    appId: 'a1', previewUrl: 'https://app/', status: 'ready', restoredFromFailedBuild: false, ready: true,
  })),
}))

const reading = (over: Partial<PreviewState> = {}): PreviewState => ({
  state: 'asleep',
  alive: false,
  previewUrl: null,
  occupyingProjectName: null,
  occupyingProjectId: null,
  restorable: null,
  ...over,
})

function reportFor(preview: PreviewState | null, startOutcome: StartOutcome | null = null): WorkspaceReport {
  return {
    state: resolveWorkspaceState({ preview, projectHasSavedBuild: null, startOutcome }),
    projectId: 'p1',
    onStarted: vi.fn(),
    onStartOutcome: vi.fn(),
    onRefresh: vi.fn(),
    onReclaimRefusal: vi.fn(),
  }
}

/** The pane under a channel primed exactly as a mounted surface would have left it. */
function renderPane(prime: (channel: WorkspaceChannel) => void) {
  const channel = createWorkspaceChannel()
  prime(channel)
  const result = render(
    <MemoryRouter>
      {/* The rail the skip control moves focus to — the shell owns it in the product; here it is
          stood up so the focus assertion is about the behaviour rather than about a missing node. */}
      <div id={WORKSPACE_RAIL_ID}>
        <button type="button">a rail control</button>
      </div>
      <WorkspaceChannelProvider value={channel}>
        <AppPane collapsed={false} onToggleCollapsed={() => {}} />
      </WorkspaceChannelProvider>
    </MemoryRouter>,
  )
  return { ...result, channel }
}

const region = () => screen.getByTestId('app-pane-region')

/** What a mounted surface publishes for the pane's chrome — every field at its resting value. */
const PANE_VIEW = {
  toolbarLeading: null, toolbarTrailing: null,
  iterating: false, reconnecting: false,
  relaunching: false, relaunchError: null, lastBuildFailed: false,
  restoredFromFailedBuild: false, completedLive: true, hasSavedBuild: null,
  previewState: null, occupyingProjectName: null, turnRunning: false,
  compileState: null, workspaceLost: false,
  saveDirty: null, saving: false, saveError: null,
}

afterEach(() => cleanup())

describe('the pane says what it is, and a keyboard can get past it', () => {
  it('is a named region', () => {
    renderPane((c) => c.workspace.set(reportFor(reading())))
    expect(region().getAttribute('aria-label')).toBe('Your app')
  })

  it('★ offers a way past the frame, and it moves focus to the rail', () => {
    // An iframe swallows the tab sequence into a cross-origin document whose length nothing here
    // can know and whose focus behaviour is the generated app's business — so a way out has to
    // exist OUTSIDE it. Without one a person navigating by keyboard is trapped in somebody else's
    // application.
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
  // The four states that used to carry a `RelaunchAffordance` inside `LivePreview`. Asserting the
  // old strings are absent would pass on a pane with no control at all; this asserts PRESENCE.
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

  const retryable: [string, PreviewState | null, StartOutcome | null][] = [
    ['the state could not be read', reading({ state: 'unknown' }), null],
    ['the start did not paint', reading({ state: 'asleep' }), { kind: 'not-painted' }],
    ['the start timed out', reading({ state: 'asleep' }), { kind: 'timed-out' }],
    ['the start failed with a reason', reading({ state: 'asleep' }), { kind: 'failed', reason: 'no image' }],
  ]

  for (const [name, preview, outcome] of retryable) {
    it(`offers a retry: ${name}`, () => {
      renderPane((c) => c.workspace.set(reportFor(preview, outcome)))
      expect(screen.getByRole('button', { name: /try again/i })).toBeTruthy()
    })
  }

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
      c.address.set({ url: 'https://app.example/', status: 'ready', projectId: 'p1' })
      c.project.set('p1')
      c.visible.set(true)
    })

    expect(container.querySelector('iframe')).toBeTruthy()
    expect(screen.queryByTestId('app-pane-empty')).toBeNull()
  })

  it('mounts NO iframe of its own when there is no address', () => {
    // A second host is the remount AE4 and AE37 exist to forbid: the app would reload on every
    // navigation and every crossing of the layout threshold, with nothing red anywhere.
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

  it('never says what the app is NOT (R-16)', () => {
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
 * ★ THE THREE DEFECTS AN EARLIER CUT OF THIS FILE SHIPPED, all caught by the suites that pin the
 * surfaces around this one rather than by review.
 *
 * The shared cause was reading `address.url` as the whole seam. It is not: the resolver also
 * returns a STATUS, deliberately independent of the URL, and an address deliberately OUTLIVES its
 * publisher — so a URL alone is neither necessary nor sufficient evidence that something is
 * serving.
 */
describe('the seam is the address AND the state, not the URL alone', () => {
  it('★ frames the LOADING state — a status with no URL yet, which is a first build coming up', () => {
    // `previewAddress.ts` says it in its own docblock: "a build that is provisioning has a status
    // and no URL yet, and that pair is what renders the loading state instead of an empty pane."
    // Gating on the URL alone put "We could not check on your app." in front of a citizen watching
    // their first build.
    //
    // Mutation receipt: change the gate back to `address.url !== null` and this goes red.
    const { container } = renderPane((c) => {
      c.workspace.set(reportFor(null))
      c.address.set({ url: null, status: 'provisioning', projectId: 'p1' })
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
    // The address outlives its publisher — that is R8's mechanism — so a URL stays held after the
    // container behind it has stopped. Framing it regardless meant an app that went to sleep showed
    // a card saying "nothing is lost" with NO way to bring it back: R3's "exactly one control
    // starts it", satisfied by zero, in an entirely ordinary state.
    renderPane((c) => {
      c.workspace.set(reportFor(reading({ state: 'asleep', restorable: true })))
      c.address.set({ url: 'https://app.example/', status: 'ready', projectId: 'p1' })
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
      c.address.set({ url: 'https://app.example/', status: 'ready', projectId: 'p1' })
      c.project.set('p1')
      c.visible.set(true)
    })

    expect(container.querySelector('iframe')).toBeTruthy()
    expect(screen.queryByTestId('app-pane-empty')).toBeNull()
  })

  it('keeps framing while a start outcome describes a press, not a container', () => {
    // `not-painted` / `timed-out` / `start-failed` say a press did not land. If a frame is already
    // up, that frame is better evidence than the press was.
    const { container } = renderPane((c) => {
      c.workspace.set(reportFor(reading({ state: 'asleep' }), { kind: 'timed-out' }))
      c.address.set({ url: 'https://app.example/', status: 'ready', projectId: 'p1' })
      c.project.set('p1')
      c.visible.set(true)
    })

    expect(container.querySelector('iframe')).toBeTruthy()
  })
})
