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
    expect(container.textContent).toMatch(/your work is saved/i)
    // The words that made ordinary housekeeping read as a fault.
    expect(container.textContent).not.toMatch(/preview unavailable/i)
    expect(container.textContent).not.toMatch(/reclaimed/i)
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
  it('offers the way back when the server says it can restore — with NO live container (AE10)', async () => {
    // AE10 exactly: the builder never pressed Save, so there is no saved bundle and no live
    // sandbox — only the platform's recovery copy. `recoveryAt` cannot serve this case at all
    // (it needs a successful attach), which is why `restorable` exists.
    const verdict = await asTheBrowserSeesIt({
      state: 'asleep',
      alive: false,
      previewUrl: null,
      restorable: true,
    })
    expect(verdict.restorable).toBe(true)

    const onRelaunch = vi.fn()
    paneFor(verdict, { onRelaunch })

    fireEvent.click(screen.getByRole('button', { name: /bring it back/i }))
    expect(onRelaunch).toHaveBeenCalledTimes(1)
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
