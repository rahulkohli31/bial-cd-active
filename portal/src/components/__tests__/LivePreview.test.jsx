import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, fireEvent, screen, act } from '@testing-library/react'
import LivePreview from '../LivePreview.jsx'

afterEach(cleanup)

// The Phase-2 preview is a genuinely CROSS-ORIGIN sandbox frame (C8). `previewUrl` is the
// sandbox FQDN root; `previewOrigin` is what the inbound origin guard validates.
const SANDBOX_URL = 'https://app-xyz.example.azurecontainerapps.io/'
const SANDBOX_ORIGIN = 'https://app-xyz.example.azurecontainerapps.io'
const SANDBOX_URL_2 = 'https://app-abc.example.azurecontainerapps.io/'

function setup(props = {}) {
  const view = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" {...props} />)
  const iframe = view.container.querySelector('iframe')
  return { ...view, iframe }
}

/** The device card that carries the reveal's opacity — the handle every reveal assertion uses. */
function card(container) {
  return container.querySelector('[data-testid="device-card"]')
}

// A message that passes BOTH halves of the C8 §3 guard: the sandbox origin AND the window of the
// frame this pane actually rendered. Origin alone stopped being sufficient once every generated
// app began sharing one hostname, so `source` is no longer optional decoration on these events.
function fromSandbox(data, source) {
  return new MessageEvent('message', { data, origin: SANDBOX_ORIGIN, source })
}

describe('LivePreview — cross-origin sandbox preview frame (C8)', () => {
  it('frames the cross-origin previewUrl (not the retired same-origin /preview)', () => {
    const { iframe } = setup()
    expect(iframe).toBeTruthy()
    expect(iframe.getAttribute('src')).toBe(SANDBOX_URL)
  })

  it('uses the C8 sandbox token list — allow-same-origin for the cross-origin next dev app, top-nav/popups withheld', () => {
    const { iframe } = setup()
    const sandbox = iframe.getAttribute('sandbox')
    expect(sandbox).toBe('allow-scripts allow-same-origin allow-forms allow-downloads')
    expect(sandbox).not.toContain('allow-top-navigation') // withheld: no top-nav hijack of the portal tab
    expect(sandbox).not.toContain('allow-popups') // withheld: no popup-phishing of the portal tab
  })

  it('REJECTS a message from a wrong origin — forwards nothing (C8 §3 origin guard, pinned)', () => {
    const onFrameMessage = vi.fn()
    setup({ onFrameMessage })
    window.dispatchEvent(new MessageEvent('message', { data: { hello: true }, origin: 'https://evil.example' }))
    expect(onFrameMessage).not.toHaveBeenCalled()
  })

  it('forwards a message from the framed app’s OWN window to the Wave-1 receiver seam', () => {
    const onFrameMessage = vi.fn()
    const { iframe } = setup({ onFrameMessage })
    window.dispatchEvent(fromSandbox({ kind: 'client_error' }, iframe.contentWindow))
    expect(onFrameMessage).toHaveBeenCalledWith({ kind: 'client_error' })
  })

  // THE ASSERTION THAT SURVIVES THE SHARED HOSTNAME. Every generated app is served from one name
  // now (BIAL refused a wildcard certificate), so `e.origin` is identical for all of them and can
  // no longer say WHICH app spoke. The reachable impostor is not a separate tab — the portal opens
  // every app link with rel="noopener", so no tab it opens holds a handle back — it is another
  // frame inside this same portal document, which is exactly what the second iframe below stands
  // in for: same origin, different window.
  //
  // ASSERT-ABSENCE, PAIRED WITH LIVENESS. jsdom SWALLOWS a throw inside a window listener, so
  // `dispatchEvent` returns normally and a bare `.not.toHaveBeenCalled()` is equally green over a
  // handler that crashed, or one that rejects everything. The genuine message afterwards is what
  // makes the rejection mean "rejected THIS sender" rather than "the gate is dead".
  it('REJECTS a correct-origin message sent from a DIFFERENT window — origin alone no longer authorises', () => {
    const onFrameMessage = vi.fn()
    const { iframe } = setup({ onFrameMessage })
    const impostor = document.body.appendChild(document.createElement('iframe'))
    try {
      window.dispatchEvent(fromSandbox({ kind: 'client_error' }, impostor.contentWindow))
      expect(onFrameMessage).not.toHaveBeenCalled()

      // LIVENESS: the very same payload from the pane's OWN frame still gets through.
      window.dispatchEvent(fromSandbox({ kind: 'client_error' }, iframe.contentWindow))
      expect(onFrameMessage).toHaveBeenCalledWith({ kind: 'client_error' })
    } finally {
      impostor.remove()
    }
  })

  it('REJECTS a correct-origin message carrying NO source at all (fails closed, not open)', () => {
    // The mutant this exists for: written as `e.source !== ref.current?.contentWindow`, flipping
    // `!==` to `!=` makes `null == undefined` true for an unmounted pane and accepts every
    // source-less message. Liveness paired for the same jsdom reason as above.
    const onFrameMessage = vi.fn()
    const { iframe } = setup({ onFrameMessage })
    window.dispatchEvent(fromSandbox({ kind: 'client_error' }))
    expect(onFrameMessage).not.toHaveBeenCalled()
    window.dispatchEvent(fromSandbox({ kind: 'client_error' }, iframe.contentWindow))
    expect(onFrameMessage).toHaveBeenCalledTimes(1)
  })

  it('rejects ALL inbound messages when previewUrl is null (preview dark, origin unknowable)', () => {
    const onFrameMessage = vi.fn()
    render(<LivePreview previewUrl={null} status="provisioning" onFrameMessage={onFrameMessage} />)
    window.dispatchEvent(new MessageEvent('message', { data: { x: 1 }, origin: 'https://anything.example' }))
    expect(onFrameMessage).not.toHaveBeenCalled()
  })

  // PR #93 review, security finding 4: new URL(url).origin is the STRING "null" for an
  // opaque-origin URL (a data: URL, about:blank, a sandboxed iframe without
  // allow-same-origin) — not the actual value null. That string is truthy, so without
  // originOf() specifically folding it to null, it would pass the `!previewOriginRef.current`
  // guard, and every opaque-origin document's postMessage (whose real e.origin is also the
  // string "null") would be trusted as if it were the sandbox. Not reachable via the real
  // control-plane today (it only ever returns an https sandbox FQDN) — pinned as a contract,
  // not a currently-exploitable path.
  it('REJECTS messages even from the literal origin "null" — an opaque previewUrl must not trust opaque senders', () => {
    const onFrameMessage = vi.fn()
    // data: is a genuinely opaque-origin URL; new URL(...).origin for it is the string "null".
    render(<LivePreview previewUrl="data:text/html,x" status="ready" onFrameMessage={onFrameMessage} />)
    window.dispatchEvent(new MessageEvent('message', { data: { hello: true }, origin: 'null' }))
    expect(onFrameMessage).not.toHaveBeenCalled()
  })

  it('the single-file relay is INERT — no outbound postMessage of code/config/token occurs (ORIG-§3-a)', () => {
    const { iframe } = setup({ status: 'ready' })
    const post = vi.spyOn(iframe.contentWindow, 'postMessage')
    // A previewReady-style message that USED to round-trip code back must now do nothing. Sent
    // from the frame's own window on purpose: source-less, it would be dropped by the C8 §3 gate
    // before reaching any code, and this test would assert inertness over a message nothing read.
    window.dispatchEvent(fromSandbox({ previewReady: true }, iframe.contentWindow))
    expect(post).not.toHaveBeenCalled()
  })
})

describe('LivePreview — reload semantics (ORIG-§3-f, no HMR-socket leak)', () => {
  it('a NEW previewUrl (a fresh preview_ready) remounts and reloads the frame', () => {
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    const first = container.querySelector('iframe')
    rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" />)
    const second = container.querySelector('iframe')
    expect(second).not.toBe(first) // key changed → remounted
    expect(second.getAttribute('src')).toBe(SANDBOX_URL_2)
  })

  it('re-rendering with the SAME previewUrl but a changed prop keeps the SAME DOM node (no reload, no socket leak)', () => {
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    const first = container.querySelector('iframe')
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" iterating />)
    const second = container.querySelector('iframe')
    // Same node. The key is now `previewUrl` + a reload nonce, and the nonce is bumped only when
    // a turn ENDS over a live preview — `iterating` going false→true is a turn starting, so it
    // must not reload and leak the framed app's HMR socket.
    expect(second).toBe(first)
    expect(second.getAttribute('src')).toBe(SANDBOX_URL)
  })
})

describe('LivePreview — status-driven visuals (all 5 C3 statuses)', () => {
  it('provisioning / building show the loading state (no iframe, no spinner-forever terminal)', () => {
    for (const status of ['provisioning', 'building']) {
      const { container, unmount } = render(<LivePreview previewUrl={null} status={status} />)
      expect(container.querySelector('iframe')).toBeNull()
      expect(container.textContent).toMatch(/setting up|building/i)
      unmount()
    }
  })

  it('ready + previewUrl shows the frame', () => {
    const { iframe } = setup({ status: 'ready' })
    expect(iframe).toBeTruthy()
  })

  it('failed / ended show the terminal placeholder (NOT a lingering spinner) — failed-before-ready', () => {
    for (const status of ['failed', 'ended']) {
      const { container, unmount } = render(<LivePreview previewUrl={null} status={status} />)
      expect(container.querySelector('iframe')).toBeNull()
      expect(container.textContent).toMatch(/no longer running|ended/i)
      unmount()
    }
  })

  it('ended AFTER a framed preview (post-ready teardown) collapses to the terminal placeholder, not the dead URL', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ended" />)
    // Terminal precedence: even with a previewUrl present, the pane must not keep framing a dead sandbox.
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).toMatch(/no longer running|ended/i)
  })

  // U4 (Plan F) — `showEmpty` IS GONE, and the empty-state copy this test used to look for
  // ("...will appear here") went with it: it moved to `AppPane`'s `NoFrame`, which is also the
  // ONLY thing that can put a citizen in this exact state now — `AppPane` mounts this component
  // at all ONLY when the address resolver has a URL, and renders `NoFrame` instead when it does
  // not (`AppPane.tsx`: `address.url ? <AppPaneHost /> : <NoFrame .../>`). So the honest claim
  // left to make here is not "here is the copy" (there is none) but "this component genuinely
  // has nothing left to say for it" — proven below by checking every kind of chrome it knows how
  // to draw, not merely the one sentence that used to live here. `workspaceState.test.ts` covers
  // the state map that now owns this copy.
  it('renders NOTHING for the no-previewUrl/no-status combination — the empty-state copy moved to AppPane', () => {
    const { container } = render(<LivePreview previewUrl={null} status={null} />)
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.querySelector('[data-testid="preview-ended-card"]')).toBeNull()
    expect(container.querySelector('[data-testid="preview-unavailable-card"]')).toBeNull()
    expect(container.querySelector('[data-testid="device-card"]')).toBeNull()
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    // The permanent live region is still mounted (it always is) — just silent.
    expect(container.querySelector('[role="status"]')?.textContent).toBe('')
  })

  it('the "still working" overlay shows only while a LIVE preview keeps receiving activity', () => {
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" iterating />)
    // The copy is "Still working…", not "Still iterating…": "iterate" is a developer's word
        // for a loop, and a citizen reading it beside their app has no way to tell whether it
        // describes progress or a fault. Same overlay, same condition, plain language.
    expect(container.textContent).toMatch(/still working/i)
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" iterating={false} />)
    expect(container.textContent).not.toMatch(/still working/i)
  })
})

describe('LivePreview — the pardoned preview: completed builds stay framed (#13/R2)', () => {
  it('ended + completedLive + previewUrl KEEPS the frame with the build-complete chip — not the placeholder', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ended" completedLive />)
    const iframe = container.querySelector('iframe')
    expect(iframe).toBeTruthy() // the server pardoned the container; the URL is genuinely live
    expect(iframe.getAttribute('src')).toBe(SANDBOX_URL)
    expect(container.textContent).toMatch(/build complete/i)
    expect(container.textContent).not.toMatch(/no longer running/i)
  })

  it('completedLive WITHOUT a previewUrl still shows the terminal placeholder — never a blank pane', () => {
    const { container } = render(<LivePreview previewUrl={null} status="ended" completedLive />)
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).toMatch(/no longer running/i)
  })

  it('ended WITHOUT completedLive still collapses (stop / force-end / failure tore the container down)', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ended" />)
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).toMatch(/no longer running/i)
  })

  it('a relaunch in flight takes precedence over the kept frame (Restoring… busy state)', () => {
    const { container } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ended" completedLive relaunching />,
    )
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).toMatch(/restoring/i)
  })

  // ★ U18 — THE RETRACTION REGRESSION, and the reason it belongs to this unit rather than to
  // plan one's U4. U18 changes what the completion message IS (the harness now renders the
  // agent's `done_summary` instead of its trailing prose) and this pane's chip is the other
  // half of that claim: the frame plus "Build complete — your app is live below". Plan one's
  // retraction is deliberately content-agnostic, so it survives the rendering change on its
  // own — but only if the claim it retracts actually goes quiet, which is a fact about THIS
  // file and nothing tested it.
  //
  // ASSERT-ABSENCE, PAIRED WITH LIVENESS. "The chip is gone" is also true of a pane that threw
  // on render, or of a `completedLive` prop that stopped arriving — so the retraction sentence
  // has to be found on screen in the same breath, in both of its nodes (the visible cover and
  // the live region), or the absence proves nothing.
  //
  // Mutation check: drop the `!showCover` guard on the chip and the first expectation goes red
  // with both sentences on screen at once — which is what a screen reader used to be told.
  it('a retracted claim silences the build-complete chip while the retraction stays readable', () => {
    render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ended"
        completedLive
        previewState="alive"
        compileState="clean"
        workspaceLost
      />,
    )

    // ABSENCE: the completion claim is not made over a workspace that has been wiped.
    expect(screen.queryByText(/build complete/i)).toBeNull()
    // LIVENESS: …because the retraction is standing in its place, in both nodes — the visible
    // cover and the pane's permanent live region.
    expect(screen.getAllByText(/stopped running and needs to be brought back/i)).toHaveLength(2)
    expect(screen.getAllByText(/we\u2019ll restore it/i).length).toBeGreaterThan(0)
  })

  it('a clean, un-retracted completed build still gets its chip (the guard is not a deletion)', () => {
    const { container } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ended"
        completedLive
        previewState="alive"
        compileState="clean"
      />,
    )
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(screen.getByText(/build complete/i)).toBeTruthy()
  })
})

describe('LivePreview — relaunch a torn-down preview (#43)', () => {
  // R3/U4 (Plan F) — INERTNESS GUARD. This used to press "Relaunch preview" on the terminal
  // placeholder; that control moved to `components/workspace/StartAppControl.tsx`, rendered by
  // `AppPane` from the one computed workspace state (R3: exactly ONE control starts the app). The
  // copy this placeholder still owns is what LIVENESS checks below — the button is what INERTNESS
  // checks.
  it('INERTNESS GUARD: the terminal placeholder still explains an ended session with a saved build, but offers no button', () => {
    const onRelaunch = vi.fn()
    const { container } = render(
      <LivePreview previewUrl={null} status="ended" onRelaunch={onRelaunch} hasSavedBuild />,
    )
    // LIVENESS: the placeholder still says what happened.
    expect(container.textContent).toMatch(/no longer running/i)
    // INERTNESS: no button under any retired label, and the prop it used to fire is never called.
    expect(screen.queryByRole('button', { name: /relaunch|bring it back/i })).toBeNull()
    expect(onRelaunch).not.toHaveBeenCalled()
  })

  it('INERTNESS GUARD: a FAILED build with a saved snapshot gets the same terminal placeholder, no button', () => {
    const onRelaunch = vi.fn()
    const { container } = render(
      <LivePreview previewUrl={null} status="failed" onRelaunch={onRelaunch} hasSavedBuild />,
    )
    expect(container.textContent).toMatch(/no longer running/i)
    expect(screen.queryByRole('button', { name: /relaunch|bring it back/i })).toBeNull()
    expect(onRelaunch).not.toHaveBeenCalled()
  })

  it('without onRelaunch, the terminal keeps its plain "start a new build" copy (no button)', () => {
    const { container } = render(<LivePreview previewUrl={null} status="ended" />)
    expect(screen.queryByRole('button', { name: /relaunch preview/i })).toBeNull()
    expect(container.textContent).toMatch(/start a new build/i)
  })

  it('while relaunching, shows the "Restoring…" busy state and hides the button (no double-click)', () => {
    const onRelaunch = vi.fn()
    const { container } = render(<LivePreview previewUrl={null} status="ended" onRelaunch={onRelaunch} hasSavedBuild relaunching />)
    expect(container.textContent).toMatch(/restoring your app/i)
    expect(screen.queryByRole('button', { name: /relaunch preview/i })).toBeNull()
    expect(container.querySelector('[aria-busy="true"]')).toBeTruthy()
  })

  it('labels a SLOW relaunch after 20s instead of spinning silently (SL-20)', () => {
    // The frame-load stall cap is armed off `showFrame`, and `frameContext` excludes
    // `relaunching` — so the one wait that can legitimately run for minutes was the one wait
    // with no label. SL-20 watched two full minutes of bare "Restoring your app…" end in
    // "Sandbox unavailable". Same 20s cap as the framed wait, deliberately the same sentence.
    vi.useFakeTimers()
    try {
      const { container } = render(
        <LivePreview previewUrl={null} status="ended" onRelaunch={vi.fn()} hasSavedBuild relaunching />,
      )
      expect(container.textContent).toMatch(/restoring your app/i)
      expect(container.textContent).not.toMatch(/taking longer than usual/i)

      act(() => vi.advanceTimersByTime(20_000))

      expect(container.textContent).toMatch(/taking longer than usual/i)
      // …and it says the thing a citizen mid-relaunch actually needs to hear.
      expect(container.textContent).toMatch(/your work is safe/i)
      // The busy state is still a busy state — this labels the wait, it does not end it.
      expect(container.querySelector('[aria-busy="true"]')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('drops the slow-relaunch label as soon as the relaunch settles', () => {
    // Guards the cleanup arm: a stale "taking longer than usual" outliving its relaunch is the
    // same class of lie as a stale reveal verdict.
    vi.useFakeTimers()
    try {
      const view = render(
        <LivePreview previewUrl={null} status="ended" onRelaunch={vi.fn()} hasSavedBuild relaunching />,
      )
      act(() => vi.advanceTimersByTime(20_000))
      expect(view.container.textContent).toMatch(/taking longer than usual/i)

      view.rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" onRelaunch={vi.fn()} hasSavedBuild />)

      expect(view.container.textContent).not.toMatch(/taking longer than usual/i)
    } finally {
      vi.useRealTimers()
    }
  })

  it('re-requests the SAME url after a repair turn ends (review #5)', () => {
    // U1's attach arm makes "same container, same url" the common case, so a repair turn ends
    // with previewUrl byte-identical. Keyed on the url alone React kept the same DOM node, the
    // browser never re-requested, and the citizen kept staring at the broken render of an app
    // the server had already fixed. SL-16 measured it as `iframe loads 1 -> 1`.
    const view = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" iterating />)
    const before = view.container.querySelector('iframe')

    // The repair turn ends: `iterating` falls while the preview stays framed.
    view.rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" iterating={false} />)

    const after = view.container.querySelector('iframe')
    expect(after?.getAttribute('src')).toBe(SANDBOX_URL)
    // A DIFFERENT element is the remount, and the remount is what re-requests. Node identity
    // rather than a test-only attribute: this is the same thing the browser reacts to.
    expect(after).not.toBe(before)
  })

  it('offers a manual Reload that remounts the frame', () => {
    // "What I see is out of date" is a judgement only the person looking can make — a dev-server
    // restart, an HMR socket that died quietly. Without this the only recourse was reloading the
    // whole portal.
    const view = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    const before = view.container.querySelector('iframe')

    fireEvent.click(screen.getByRole('button', { name: /reload/i }))

    expect(view.container.querySelector('iframe')).not.toBe(before)
  })

  it('frames the restored preview once relaunch resolves (a fresh ready URL)', () => {
    // BuilderPage feeds the relaunched URL back with status "ready" → the pane frames it.
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" onRelaunch={vi.fn()} />)
    expect(container.querySelector('iframe')?.getAttribute('src')).toBe(SANDBOX_URL_2)
  })
})

// U4 (Plan F) — THIS WHOLE DESCRIBE BLOCK TESTED THE `showEmpty` ARM, and that arm is gone. Every
// test here rendered `<LivePreview hasSavedBuild=... onRelaunch=... relaunchError=... />` with
// NEITHER a previewUrl NOR a status — the exact no-frame, nothing-built condition `AppPane` now
// owns outright. Two things moved together, not just the button:
//
//   1. THE COPY. "This project already has a saved build" / "Submit a prompt to start a build" /
//      the N7 tri-state wording (a `hasSavedBuild === null` answer claiming nothing) all lived in
//      this component's empty-state placeholder. That placeholder, and the state map that drives
//      it, moved to `AppPane`'s `NoFrame` — and the map itself is `resolveWorkspaceState` in
//      `workspaceState.ts`: its `atRest()` resolves the identical `restorable ?? projectHasSavedBuild`
//      tri-state this block exercised (see `workspaceState.test.ts`).
//   2. THE 404-SAID-AND-NOT-SWALLOWED DISCIPLINE (N7's other half). A failed start now surfaces
//      through `StartAppControl`'s own outcome handling (`StartAppControl.test.tsx`), not through
//      this component's old `relaunchError` prop — no `AppPane`-driven pane populates that prop
//      for this arm any more.
//
// `AppPane` also structurally forecloses this exact prop combination from ever reaching
// `LivePreview` in the product: it mounts `AppPaneHost` (and hence this component) only when the
// address resolver has a URL, and renders `NoFrame` instead when it does not — so a real citizen
// can no longer land on the state this whole block constructed by hand.
//
// ONE test replaces the five that were here, because all five failed for the identical reason
// (asserting a button/copy pair that no longer exists on this component) and a second, third and
// fourth copy of the same "this is gone, and moved to X" finding would not prove anything the
// first did not.
describe('LivePreview — the no-previewUrl/no-status combination (formerly "relaunch from PROJECT state")', () => {
  it('stays inert across the whole former N7/U6 matrix — hasSavedBuild and relaunchError no longer reach any render here', () => {
    for (const props of [
      { hasSavedBuild: true },
      { hasSavedBuild: false },
      { hasSavedBuild: null },
      { hasSavedBuild: true, relaunchError: { kind: 'not_found', message: 'gone' } },
      { hasSavedBuild: true, relaunchError: { kind: 'unavailable', message: 'try later' } },
    ]) {
      const { container, unmount } = render(<LivePreview onRelaunch={vi.fn()} {...props} />)
      expect(container.querySelector('iframe')).toBeNull()
      expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
      expect(screen.queryByRole('alert')).toBeNull()
      expect(container.querySelector('[role="status"]')?.textContent).toBe('')
      unmount()
    }
  })
})

describe('LivePreview — the U6 relaunch response matrix (#43)', () => {
  it('404 not_found HIDES the affordance and says there is nothing to relaunch', () => {
    const { container } = render(
      <LivePreview
        previewUrl={null}
        status="ended"
        onRelaunch={vi.fn()}
        relaunchError={{ kind: 'not_found', message: 'No saved build to relaunch. Build the app first.' }}
      />,
    )
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(container.textContent).toMatch(/nothing to relaunch/i)
  })

  // U4 (Plan F) — INERTNESS GUARD, AND A DOCUMENTED FINDING, NOT JUST A RE-POINT. This test used
  // to assert the transient `unavailable` copy inside `screen.getByRole('alert')` — but tracing
  // the current render arms shows `relaunchError` is read in exactly THREE places in
  // `LivePreview.tsx`, and every one of them special-cases ONLY `kind === 'not_found'`
  // (`grep -n 'relaunchError' src/components/LivePreview.tsx`). The `unavailable`/`failed` kinds
  // fall through to the generic hasSavedBuild-only sentence below, and their own `.message` is
  // never read anywhere — no `role="alert"`, no "try again later". The component's own docblock
  // (`LivePreviewProps.relaunchError`) still SAYS "`unavailable`/`failed` show their copy with the
  // button restored for a retry", which no longer matches what renders: this looks like the U4
  // sweep took the message along with the button for these two kinds, not just the button, and
  // the docblock was never updated to match. Filed as a finding rather than silently reasserted as
  // correct — see the session report. What is left to pin honestly is that the generic
  // saved-build sentence still renders and still makes no button.
  it('INERTNESS GUARD: an `unavailable` relaunch error no longer gets its own alert copy — only the generic saved-build sentence survives', () => {
    const { container } = render(
      <LivePreview
        previewUrl={null}
        status="ended"
        onRelaunch={vi.fn()}
        hasSavedBuild
        relaunchError={{ kind: 'unavailable', message: 'Sandbox unavailable. Please try again later or contact the admin' }}
      />,
    )
    // LIVENESS: the terminal placeholder still renders and still makes the saved-build claim.
    expect(container.textContent).toMatch(/restore your saved app/i)
    // The kind-specific copy is gone, not merely un-alerted — pinned so a reader does not assume
    // it survives elsewhere on the pane.
    expect(container.textContent).not.toMatch(/try again later/i)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
  })

  it('INERTNESS GUARD: a `failed` relaunch error no longer gets its own alert copy — only the generic saved-build sentence survives', () => {
    const { container } = render(
      <LivePreview
        previewUrl={null}
        status="ended"
        onRelaunch={vi.fn()}
        hasSavedBuild
        relaunchError={{ kind: 'failed', message: 'Failed to relaunch the preview' }}
      />,
    )
    expect(container.textContent).toMatch(/restore your saved app/i)
    expect(container.textContent).not.toMatch(/failed to relaunch/i)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
  })

  // R3/U4 — INERTNESS GUARD. `lastBuildFailed` used to pick between two button labels ("Relaunch
  // preview" vs "Relaunch last saved version"); `LivePreview`'s own docblock records that the prop
  // is now accepted and DELIBERATELY UNREAD — the distinction it drew belongs to
  // `restoredFromFailedBuild` now, which says the same thing on a FRAMED pane where a citizen can
  // actually see it (see "overlays the last-saved-version notice..." below, unaffected by this).
  it('INERTNESS GUARD: `lastBuildFailed` no longer labels a button — there is no button left to label', () => {
    const { container } = render(
      <LivePreview previewUrl={null} status="failed" onRelaunch={vi.fn()} hasSavedBuild lastBuildFailed />,
    )
    expect(container.textContent).toMatch(/no longer running/i)
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
  })

  it('overlays the last-saved-version notice on a frame restored from a failed build', () => {
    const { container, rerender } = render(
      <LivePreview previewUrl={SANDBOX_URL_2} status="ready" restoredFromFailedBuild />,
    )
    expect(container.textContent).toMatch(/last saved version/i)
    expect(container.querySelector('iframe')).toBeTruthy() // the frame still shows
    rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" />)
    expect(container.textContent).not.toMatch(/last saved version/i)
  })
})

describe('LivePreview — dev-server crash: reconnecting is distinct from building (F8/U5)', () => {
  it('shows a distinct "Reconnecting…" state (NOT the "Building…" loading copy, NOT the live frame)', () => {
    // A dev-process crash after framing: the port is dead, so the pane must not keep framing a
    // now-broken URL, and must not read as "building" (a different, in-progress meaning).
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" reconnecting />)
    expect(container.textContent).toMatch(/reconnecting to your preview/i)
    expect(container.textContent).not.toMatch(/building your app/i)
    expect(container.querySelector('iframe')).toBeNull()
  })

  it('reconnecting is visually distinct from the "Building your app…" loading bounce', () => {
    const building = render(<LivePreview previewUrl={null} status="building" />)
    expect(building.container.textContent).toMatch(/building your app/i)
    expect(building.container.textContent).not.toMatch(/reconnecting/i)
    building.unmount()
    const reconnecting = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" reconnecting />)
    expect(reconnecting.container.textContent).toMatch(/reconnecting/i)
    expect(reconnecting.container.textContent).not.toMatch(/building your app/i)
  })

  it('a fresh preview_ready (reconnecting=false) clears the reconnecting state and re-frames', () => {
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" reconnecting />)
    expect(container.textContent).toMatch(/reconnecting/i)
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" reconnecting={false} />)
    expect(container.textContent).not.toMatch(/reconnecting/i)
    expect(container.querySelector('iframe')).toBeTruthy() // re-framed
  })
})

// --- U5: the reveal is gated on the framed document's own `load` ---------------------------
//
// What this replaces: `FRAME_GRACE_MS = 400` revealed the iframe on a TIMER, and `showLoading`
// was destroyed the instant `previewUrl` arrived. A timer can only prove that time passed, so
// the citizen got an UNLABELLED BLANK WHITE CARD for the 5-7s the sandbox spent compiling its
// first Turbopack route — at exactly the moment they had been told their app was ready (R3).
//
// The device card is queried by data-testid rather than `iframe.parentElement` for the reason
// the device-toggle block gives below: an element inserted between the card and the iframe
// later must not silently retarget these assertions at the wrong node.

const FRAME_LOAD_CAP_MS = 20000 // mirrors LivePreview's own cap; the tests step over it deliberately

describe('LivePreview — R104\u2019s stop-clock: `onRevealed` (U4)', () => {
  it('\u2605 fires when the citizen is actually LOOKING at the app, and not a moment before', () => {
    // The mark has to mean "the app is on screen". A `load` alone does not: it fires for a 500,
    // and it fires under a raised cover. Only `revealed` \u2014 frame loaded AND cover down \u2014 is the
    // honest instant, which is exactly why the effect hangs off that value and nothing else.
    const onRevealed = vi.fn()
    const { container } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" onRevealed={onRevealed} />,
    )

    expect(onRevealed).not.toHaveBeenCalled()
    fireEvent.load(container.querySelector('iframe'))

    expect(card(container).className).toMatch(/opacity-100/)
    expect(onRevealed).toHaveBeenCalledTimes(1)
  })

  it('\u2605 does NOT fire while the cover is up over a broken app', () => {
    // A failed compile keeps the cover down over an error screen. The document loaded; the
    // citizen is looking at a cover, not at their app. Mutation check: hang the effect off
    // `frameLoaded` instead of `revealed` and this goes red.
    const onRevealed = vi.fn()
    const { container, rerender } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="failed" onRevealed={onRevealed} />,
    )
    fireEvent.load(container.querySelector('iframe'))

    expect(card(container).className).toMatch(/opacity-0/)
    expect(onRevealed).not.toHaveBeenCalled()

    // \u2026and it fires the moment the app actually comes up clean.
    rerender(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="clean" onRevealed={onRevealed} />,
    )
    expect(onRevealed).toHaveBeenCalledTimes(1)
  })

  it('\u2605 fires ONCE for one document, even when the reveal is retracted and re-earned', () => {
    // The reveal is not monotonic: a verdict that flips to failed RETRACTS it (R4), and a later
    // clean verdict earns it back on the SAME document. That is one first-view, not two \u2014 and it
    // is the only path that re-enters this effect with the same frame key, so it is the one that
    // pins the guard. Mutation check: drop the per-frame-key guard and this goes red.
    const onRevealed = vi.fn()
    const { container, rerender } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="clean" onRevealed={onRevealed} />,
    )
    fireEvent.load(container.querySelector('iframe'))
    expect(onRevealed).toHaveBeenCalledTimes(1)

    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="failed" onRevealed={onRevealed} />)
    expect(card(container).className).toMatch(/opacity-0/) // retracted
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="clean" onRevealed={onRevealed} />)
    expect(card(container).className).toMatch(/opacity-100/) // and back

    expect(onRevealed).toHaveBeenCalledTimes(1)
  })

  it('\u2605 does NOT fire when the workspace-lost cover is up over the frame', () => {
    // `revealed` is NOT "the cover is down". `showCover` is `covered || workspaceLost` while
    // `revealed` reads only `covered`, so a confirmed reversion leaves the frame at full opacity
    // UNDERNEATH a cover that says the app stopped running. Firing here reports a first view of
    // an app the citizen cannot see \u2014 and reports it as FAST, since the frame loaded fine.
    //
    // Mutation check: drop `workspaceLost` from the effect's guard and this goes red.
    const onRevealed = vi.fn()
    const { container } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" workspaceLost onRevealed={onRevealed} />,
    )
    fireEvent.load(container.querySelector('iframe'))

    expect(container.textContent).toMatch(/stopped running/i) // the cover really is up
    expect(onRevealed).not.toHaveBeenCalled()
  })

  it('\u2605 a callback that throws does not take the preview pane down with it', () => {
    // There is no ErrorBoundary anywhere in this portal, so an unguarded throw out of this effect
    // white-screens the builder \u2014 a measurement failing the thing it measures, which is the one
    // outcome this surface exists to avoid.
    const { container } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ready"
        onRevealed={() => {
          throw new Error('the beacon module blew up')
        }}
      />,
    )

    expect(() => fireEvent.load(container.querySelector('iframe'))).not.toThrow()
    expect(card(container).className).toMatch(/opacity-100/) // and the app is still shown
  })

  it('is optional \u2014 a caller that does not measure anything still reveals normally', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    fireEvent.load(container.querySelector('iframe'))
    expect(card(container).className).toMatch(/opacity-100/)
  })
})

describe('LivePreview — the frame is revealed on load, never on a timer (U5/R3)', () => {
  it('keeps the labelled wait up when previewUrl arrives, and swaps it for the frame on load', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    const iframe = container.querySelector('iframe')

    // Mounted at once — a frame that never starts loading can never fire `load` — but NOT revealed…
    expect(iframe).toBeTruthy()
    expect(card(container).className).toMatch(/opacity-0/)
    // …and the wait is LABELLED. This is the whole requirement.
    expect(container.textContent).toMatch(/starting your app/i)

    fireEvent.load(iframe)

    expect(card(container).className).toMatch(/opacity-100/)
    expect(container.textContent).not.toMatch(/starting your app/i)
  })

  it('time passing NEVER reveals the frame (mutation: put the 400ms grace back and this goes red)', () => {
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS - 1))
      expect(card(container).className).toMatch(/opacity-0/) // nothing painted, nothing revealed
      expect(container.textContent).toMatch(/starting your app/i)
    } finally {
      vi.useRealTimers()
    }
  })

  // R3/U4 — INERTNESS GUARD. The stall card still degrades to a LABELLED state (never a bare
  // white card — that half of the unit is untouched); what it no longer does is offer its own
  // Relaunch button, because R3 says exactly one control starts the app and this is not it.
  it('INERTNESS GUARD: a frame that never loads still degrades to a LABELLED state — never a bare white card, and never a button', () => {
    vi.useFakeTimers()
    try {
      const onRelaunch = vi.fn()
      const { container } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ready" onRelaunch={onRelaunch} hasSavedBuild />,
      )
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS + 1))
      // LIVENESS: still nothing painted, so still not revealed, and still labelled.
      expect(card(container).className).toMatch(/opacity-0/)
      expect(container.textContent).toMatch(/taking longer than usual/i)
      // INERTNESS: no button, under any label, and the prop it used to fire is never called.
      expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
      expect(onRelaunch).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('the capped state keeps the frame MOUNTED, so a late load still reveals', () => {
    // Unmounting the iframe at the cap would make the timeout permanent BY CONSTRUCTION: the
    // load it is waiting for could never arrive. The cap changes the copy, not the frame.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS + 1))
      expect(container.textContent).toMatch(/taking longer than usual/i)
      const iframe = container.querySelector('iframe')
      expect(iframe).toBeTruthy()
      fireEvent.load(iframe)
      expect(card(container).className).toMatch(/opacity-100/)
      expect(container.textContent).not.toMatch(/taking longer than usual/i)
    } finally {
      vi.useRealTimers()
    }
  })

  it('the capped state inherits the R5/N7 discipline: no relaunch offered, and none PROMISED, without a confirmed build', () => {
    // The same trap n7-terminal-branch-20260730.png captured on the terminal branch: copy that
    // says "relaunch it" is a claim about a saved build, so it is gated exactly like the button.
    vi.useFakeTimers()
    try {
      for (const hasSavedBuild of [false, null]) {
        const { container, unmount } = render(
          <LivePreview previewUrl={SANDBOX_URL} status="ready" onRelaunch={vi.fn()} hasSavedBuild={hasSavedBuild} />,
        )
        act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS + 1))
        expect(container.textContent).toMatch(/taking longer than usual/i) // still labelled…
        expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull() // …but claims nothing
        expect(container.textContent).not.toMatch(/relaunch the preview/i)
        unmount()
      }
    } finally {
      vi.useRealTimers()
    }
  })

  it('a NEW previewUrl re-gates the reveal on the new frame’s own load (relaunch mid-session)', () => {
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    fireEvent.load(container.querySelector('iframe'))
    expect(card(container).className).toMatch(/opacity-100/)

    rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" />)
    // The fresh frame must not inherit the previous one's verdict — that is the blank card again.
    expect(card(container).className).toMatch(/opacity-0/)
    expect(container.textContent).toMatch(/starting your app/i)

    fireEvent.load(container.querySelector('iframe'))
    expect(card(container).className).toMatch(/opacity-100/)
  })

  it('a relaunch AFTER the cap returns to the honest wait — the stalled verdict does not outlive its frame', () => {
    // Caught in a real browser, not here: with the stall held as a bare boolean instead of
    // per-src, the fresh frame opened straight into "taking longer than usual" — a 20-second-old
    // complaint about a frame that no longer exists.
    vi.useFakeTimers()
    try {
      const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS + 1))
      expect(container.textContent).toMatch(/taking longer than usual/i)

      rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" />)
      expect(container.textContent).not.toMatch(/taking longer than usual/i)
      expect(container.textContent).toMatch(/starting your app/i)

      // …and the new frame gets its own full cap, not the remains of the old one.
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS - 1))
      expect(container.textContent).toMatch(/starting your app/i)
      act(() => vi.advanceTimersByTime(2))
      expect(container.textContent).toMatch(/taking longer than usual/i)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a re-frame of the SAME url after a reconnect re-gates too (the frame was torn down and rebuilt)', () => {
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    fireEvent.load(container.querySelector('iframe'))
    expect(card(container).className).toMatch(/opacity-100/)

    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" reconnecting />) // dev process died
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" reconnecting={false} />) // fresh preview_ready
    expect(card(container).className).toMatch(/opacity-0/)
    expect(container.textContent).toMatch(/starting your app/i)
  })

  it('reveals on the load of an ERROR response — a broken app must look broken, not pending forever', () => {
    // The frame is genuinely cross-origin (C8): `load` fires for a 500 exactly as it does for a
    // 200 and the portal cannot read the status. Revealing therefore claims "a document arrived",
    // never "the app is healthy" — the seam a future previewHealth prop would hang off.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      fireEvent.load(container.querySelector('iframe')) // whatever the sandbox served, 500 included
      expect(card(container).className).toMatch(/opacity-100/)
      // And the cap is cancelled: a revealed frame must never later collapse into "taking longer".
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS * 2))
      expect(card(container).className).toMatch(/opacity-100/)
      expect(container.textContent).not.toMatch(/taking longer than usual/i)
    } finally {
      vi.useRealTimers()
    }
  })

  it('no state shows an unlabelled blank pane: while the frame is hidden, the pane always says why', () => {
    // The unit's verification line, made executable. Anything the citizen can be looking at
    // before the document paints must carry one of these labels.
    const LABELLED = /setting up your sandbox|building your app|starting your app|taking longer than usual/i
    vi.useFakeTimers()
    try {
      // Every pre-reveal state, in the order a citizen actually meets them.
      const waits = [
        () => render(<LivePreview previewUrl={null} status="provisioning" />),
        () => render(<LivePreview previewUrl={null} status="building" />),
        () => render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />),
        () => {
          const view = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
          act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS + 1))
          return view
        },
      ]
      for (const mount of waits) {
        const { container, unmount } = mount()
        expect(container.querySelector('[data-testid="device-card"].opacity-100')).toBeNull()
        expect(container.textContent).toMatch(LABELLED)
        unmount()
      }
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('LivePreview — the reconnecting state is BOUNDED after a completed build (F8/U5)', () => {
  // R3/U4 — INERTNESS GUARD. The bound itself (never a forever spinner) is untouched and stays
  // asserted; only the button half — this pane's own way to act on the collapse — moved off it.
  it('INERTNESS GUARD: after the cap with no recovery, still collapses to "preview unavailable" (no forever spinner), with no button of its own', () => {
    vi.useFakeTimers()
    try {
      const onRelaunch = vi.fn()
      const { container } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ended" completedLive reconnecting onRelaunch={onRelaunch} hasSavedBuild />,
      )
      expect(container.textContent).toMatch(/reconnecting/i) // before the cap
      act(() => vi.advanceTimersByTime(20001))
      // LIVENESS: the bounded terminal still fires.
      expect(container.textContent).toMatch(/preview unavailable/i)
      expect(container.textContent).not.toMatch(/reconnecting to your preview/i)
      // INERTNESS: no button, and the prop it used to fire is never called.
      expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
      expect(onRelaunch).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('does NOT bound while the build is still ACTIVE (no completedLive) — the loop owns recovery', () => {
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" reconnecting />)
      act(() => vi.advanceTimersByTime(60000))
      expect(container.textContent).toMatch(/reconnecting/i) // still reconnecting, never "unavailable"
      expect(container.textContent).not.toMatch(/preview unavailable/i)
    } finally {
      vi.useRealTimers()
    }
  })
})

// --- the save control (U5b / KTD-5e) -------------------------------------------------------
//
// Nothing writes the git bundle to storage except this button: the turn terminal used to
// snapshot on every message, which quietly made each message a new saved version, so there was
// no such thing as trying something and walking away from it.
//
// The subtlety worth testing is the TRI-STATE. `saveDirty` is true / false / null, and null
// means UNKNOWN — no live workspace, or a bundle the server could not compare. Rendering
// unknown as "Saved" tells the user their work is safe when nothing actually checked.

describe('LivePreview — the Save control (KTD-5e)', () => {
  it('offers a highlighted Save when there is unsaved work', () => {
    setup({ saveDirty: true, onSave: vi.fn() })
    const save = screen.getByTestId('save-project')
    expect(save.textContent).toContain('Save')
    expect(save.disabled).toBe(false)
    // Highlighted ONLY when there is something to save — a permanently-primary Save button
    // trains the user to ignore it, which is the state the dirty check exists to escape.
    expect(save.className).toMatch(/bg-primary/)
  })

  it('calls onSave once per click', () => {
    const onSave = vi.fn()
    setup({ saveDirty: true, onSave })
    fireEvent.click(screen.getByTestId('save-project'))
    expect(onSave).toHaveBeenCalledTimes(1)
  })

  it('goes quiet and un-clickable once everything is saved', () => {
    setup({ saveDirty: false, onSave: vi.fn() })
    const save = screen.getByTestId('save-project')
    expect(save.textContent).toContain('Saved')
    expect(save.disabled).toBe(true)
    expect(save.className).not.toMatch(/bg-primary/)
    expect(screen.getByText(/all changes saved/i)).toBeTruthy()
  })

  it('UNKNOWN hides the control rather than claiming the work is saved', () => {
    // THE POINT. `null` is not `false`. A button reading "Saved" here would be a claim nobody
    // verified, and the user would act on it.
    setup({ saveDirty: null, onSave: vi.fn() })
    expect(screen.queryByTestId('save-project')).toBeNull()
  })

  it('shows a save failure as an alert instead of letting it look successful', () => {
    setup({
      saveDirty: true,
      onSave: vi.fn(),
      saveError: 'Your workspace is no longer running, so there is nothing to save.',
    })
    expect(screen.getByRole('alert').textContent).toMatch(/no longer running/i)
  })

  it('reports progress while saving, and refuses a second click', () => {
    setup({ saveDirty: true, onSave: vi.fn(), saving: true })
    const save = screen.getByTestId('save-project')
    expect(save.textContent).toContain('Saving')
    expect(save.disabled).toBe(true)
  })
})

describe('LivePreview — the preview only claims a build that exists (R5)', () => {
  // The exact screen n7-terminal-branch-20260730.png captured: a fresh, never-built project
  // opened on the terminal placeholder promised "restore your saved app" and offered a
  // Relaunch that could only 404.
  it('terminal + hasSavedBuild=false: no affordance, no saved-app promise (mutation: ungate the render and this goes red)', () => {
    const { container } = render(
      <LivePreview previewUrl={null} status="ended" onRelaunch={vi.fn()} hasSavedBuild={false} />,
    )
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(container.textContent).not.toMatch(/restore your saved app/i)
    expect(container.textContent).toMatch(/nothing to relaunch yet/i)
  })

  it('unavailable + hasSavedBuild=false: same gate, same honesty', () => {
    vi.useFakeTimers()
    try {
      const { container } = render(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ended"
          completedLive
          reconnecting
          onRelaunch={vi.fn()}
          hasSavedBuild={false}
        />,
      )
      act(() => vi.advanceTimersByTime(20001)) // past the reconnect cap → showUnavailable
      expect(container.textContent).toMatch(/preview unavailable/i)
      expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
      expect(container.textContent).not.toMatch(/restore your saved app/i)
      expect(container.textContent).toMatch(/nothing to relaunch yet/i)
    } finally {
      vi.useRealTimers()
    }
  })

  // R5/R3 — INERTNESS GUARD, and the interesting half is what SURVIVES. R5 was never about the
  // button; it was about not promising a restore where none exists. That promise is still made —
  // in the copy — in both branches; only the button that used to accompany it is gone.
  it('INERTNESS GUARD: both branches with hasSavedBuild=true still MAKE the saved-app claim in copy, but neither offers its own button', () => {
    // Terminal:
    const onRelaunchTerminal = vi.fn()
    const first = render(
      <LivePreview previewUrl={null} status="ended" onRelaunch={onRelaunchTerminal} hasSavedBuild />,
    )
    expect(first.container.textContent).toMatch(/restore your saved app/i)
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(onRelaunchTerminal).not.toHaveBeenCalled()
    first.unmount()
    // Unavailable (reconnect cap expired):
    vi.useFakeTimers()
    try {
      const onRelaunchUnavailable = vi.fn()
      const { container } = render(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ended"
          completedLive
          reconnecting
          onRelaunch={onRelaunchUnavailable}
          hasSavedBuild
        />,
      )
      act(() => vi.advanceTimersByTime(20001))
      expect(container.textContent).toMatch(/restore your saved app/i)
      expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
      expect(onRelaunchUnavailable).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('hasSavedBuild=null (store unreachable) claims NOTHING in either direction, in both branches', () => {
    const first = render(
      <LivePreview previewUrl={null} status="ended" onRelaunch={vi.fn()} hasSavedBuild={null} />,
    )
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(document.body.textContent).not.toMatch(/restore your saved app/i) // no "there is one"
    expect(document.body.textContent).not.toMatch(/no saved build/i) // and no "there is none"
    expect(document.body.textContent).toMatch(/start a new build/i)
    first.unmount()
    vi.useFakeTimers()
    try {
      render(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ended"
          completedLive
          reconnecting
          onRelaunch={vi.fn()}
          hasSavedBuild={null}
        />,
      )
      act(() => vi.advanceTimersByTime(20001))
      expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
      expect(document.body.textContent).not.toMatch(/restore your saved app/i)
      expect(document.body.textContent).not.toMatch(/no saved build/i)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a 404 after the click renders the not-found message inside a role="alert" in the terminal branch', () => {
    render(
      <LivePreview
        previewUrl={null}
        status="ended"
        onRelaunch={vi.fn()}
        hasSavedBuild
        relaunchError={{ kind: 'not_found', message: 'No saved build to relaunch. Build the app first.' }}
      />,
    )
    expect(screen.getByRole('alert').textContent).toMatch(/nothing to relaunch yet/i)
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
  })
})

describe('LivePreview — device viewport toggle (#42)', () => {
  // The iframe itself is plain `w-full` (see LivePreview.jsx's DEVICES comment): it always
  // matches the card's width exactly, with no competing inline value of its own. So the
  // device pixel width lives on the WRAPPER's inline style, and that's what
  // these tests assert — they pin that the inline style got SET, not that the framed
  // document actually reflows against it (jsdom has no layout engine, so a real reflow claim
  // can only be proven in a browser — see the real-sandbox Playwright spec for that half).
  // Queried by data-testid rather than `iframe.parentElement`, so an element inserted between
  // the card and the iframe later can't silently retarget these assertions at the wrong node.
  function deviceCard(container) {
    return container.querySelector('[data-testid="device-card"]')
  }

  it('defaults to Desktop: full width, pressed', () => {
    const { container } = setup()
    expect(deviceCard(container).style.width).toBe('100%')
    expect(screen.getByRole('button', { name: /desktop/i }).getAttribute('aria-pressed')).toBe('true')
  })

  it('Tablet sets the wrapper\'s inline width to 834px (iPad Pro 11" preset) and marks only Tablet pressed', () => {
    const { container } = setup()
    fireEvent.click(screen.getByRole('button', { name: /tablet/i }))
    expect(deviceCard(container).style.width).toBe('834px')
    expect(screen.getByRole('button', { name: /tablet/i }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: /desktop/i }).getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByRole('button', { name: /mobile/i }).getAttribute('aria-pressed')).toBe('false')
  })

  it('Mobile sets the wrapper\'s inline width to 390px (iPhone class) and marks only Mobile pressed', () => {
    const { container } = setup()
    fireEvent.click(screen.getByRole('button', { name: /mobile/i }))
    expect(deviceCard(container).style.width).toBe('390px')
    expect(screen.getByRole('button', { name: /mobile/i }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: /desktop/i }).getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByRole('button', { name: /tablet/i }).getAttribute('aria-pressed')).toBe('false')
  })

  it('switching back to Desktop restores full width', () => {
    const { container } = setup()
    fireEvent.click(screen.getByRole('button', { name: /mobile/i }))
    fireEvent.click(screen.getByRole('button', { name: /desktop/i }))
    expect(deviceCard(container).style.width).toBe('100%')
  })

  it('no per-mode height is imposed on the wrapper — no fixed device aspect ratio', () => {
    const { container } = setup()
    for (const label of ['Desktop', 'Tablet', 'Mobile']) {
      fireEvent.click(screen.getByRole('button', { name: new RegExp(label, 'i') }))
      expect(deviceCard(container).style.height).toBe('')
    }
  })

  it('the card keeps relative + overflow-hidden in every mode — anchors/clips the C8 overlays', () => {
    const { container } = setup()
    for (const label of ['Desktop', 'Tablet', 'Mobile']) {
      fireEvent.click(screen.getByRole('button', { name: new RegExp(label, 'i') }))
      expect(deviceCard(container).className).toMatch(/relative/)
      expect(deviceCard(container).className).toMatch(/overflow-hidden/)
    }
  })
})

describe('LivePreview — compact ended-state card (#42 F3)', () => {
  it('renders the terminal state as a small bounded card, not a full-pane block', () => {
    // Under the new hasSavedBuild gating, onRelaunch alone no longer renders the button —
    // pass hasSavedBuild explicitly so this exercises the button-present path.
    const { container } = render(<LivePreview previewUrl={null} status="ended" onRelaunch={vi.fn()} hasSavedBuild />)
    const card = container.querySelector('[data-testid="preview-ended-card"]')
    expect(card).toBeTruthy()
    expect(card.className).toMatch(/max-w-xs/)
    // Not the old full-pane stretch — the card itself is bounded, its parent centers it.
    expect(card.className).not.toMatch(/flex-1/)
  })

  // R3/U4 — INERTNESS GUARD. The compact card itself (#42 F3) is untouched; what it no longer
  // contains is a button of its own.
  it('INERTNESS GUARD: the compact ended-state card contains no button of its own', () => {
    const onRelaunch = vi.fn()
    const { container } = render(<LivePreview previewUrl={null} status="ended" onRelaunch={onRelaunch} hasSavedBuild />)
    const card = container.querySelector('[data-testid="preview-ended-card"]')
    expect(card).toBeTruthy() // LIVENESS: the compact card still renders
    expect(card.textContent).toMatch(/no longer running/i)
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(onRelaunch).not.toHaveBeenCalled()
  })
})

describe('LivePreview — compact unavailable-state card (#42 F3)', () => {
  it('renders the bounded reconnect-cap-expired state as a small card, distinct from the ended card', () => {
    vi.useFakeTimers()
    try {
      const { container } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ended" completedLive reconnecting onRelaunch={vi.fn()} hasSavedBuild />,
      )
      act(() => vi.advanceTimersByTime(20001))
      const card = container.querySelector('[data-testid="preview-unavailable-card"]')
      expect(card).toBeTruthy()
      expect(card.className).toMatch(/max-w-xs/)
      expect(card.className).not.toMatch(/flex-1/)
    } finally {
      vi.useRealTimers()
    }
  })

  // R3/U4 — INERTNESS GUARD. Same story as the ended-state card above: the compact unavailable
  // card (#42 F3) is untouched, its button is not.
  it('INERTNESS GUARD: the compact unavailable card contains no button of its own', () => {
    vi.useFakeTimers()
    try {
      const onRelaunch = vi.fn()
      const { container } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ended" completedLive reconnecting onRelaunch={onRelaunch} hasSavedBuild />,
      )
      act(() => vi.advanceTimersByTime(20001))
      const card = container.querySelector('[data-testid="preview-unavailable-card"]')
      expect(card).toBeTruthy() // LIVENESS: the compact card still renders
      expect(card.textContent).toMatch(/preview unavailable/i)
      expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
      expect(onRelaunch).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('LivePreview — a live preview is left alone', () => {
  it('with no server verdict the pane keeps framing what it has — absence is not a verdict', () => {
    // The `previewState` prop defaults to null (NOT YET ASKED). The four-state rendering and
    // the reclaimed cases live in LivePreview.test.tsx, next to the wire shape that drives them.
    const { container } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ended" completedLive />,
    )
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(container.textContent).not.toMatch(/preview unavailable/i)
    expect(container.textContent).not.toMatch(/asleep/i)
  })
})

// ---------------------------------------------------------------------------------------
// R16 / R18 — THE COVER (U12)
//
// On 2026-08-18 a full-screen framework compile-error screen filled this pane for ~66 seconds
// in each of three builds, in front of a client. This is the fix, and it is a fix that reaches
// apps ALREADY BUILT: the pane covers its own frame from the outside, so nothing about the
// app — its Next version, its files, its image — is consulted or changed.
//
// Every absence assertion below is paired with a liveness assertion in the same test. A
// `queryBy(...).toBeNull()` also passes when the component threw, and this file is exactly the
// place that would go unnoticed.
// ---------------------------------------------------------------------------------------

const HOLDING = /Putting the latest change together/i
const HOLDING_SLOW = /taking longer than usual — it will appear here/i
// Mirrors `HOLDING_ESCALATE_MS` in the component. Kept as a literal on purpose: a test that
// imports the constant it is pinning asserts only that the code equals itself.
const ESCALATE_MS = 20000

// …and what the cover says when no turn is running, so the holding wording cannot outlive the
// work it describes (U7/R13). TWO sentences, because the cover's two idle causes are opposites:
// `failed` means the app is not usable, `building` means it is compiling a route right now —
// which a perfectly healthy completed app does on demand.
const IDLE_BROKEN = /Your app isn.t running right now/i
const IDLE_BUSY = /Getting your app ready/i

/** The cover is an opaque, full-bleed element over the frame. Identified by what makes it a
 *  cover rather than by a test id, so a refactor that stops covering fails here — and matched
 *  on ANY of its three sentences, because which one it is telling is a separate question from
 *  whether it is covering. Matching on one of them would have made every idle-state test read
 *  as "no cover at all". */
function coverEl(container) {
  return [...container.querySelectorAll('div')].find(
    (el) =>
      el.className.includes('absolute inset-0') &&
      el.textContent &&
      (HOLDING.test(el.textContent) ||
        HOLDING_SLOW.test(el.textContent) ||
        IDLE_BROKEN.test(el.textContent) ||
        IDLE_BUSY.test(el.textContent)),
  )
}

describe('LivePreview — the cover (R16/R18): the framework error screen is never seen', () => {
  it('covers the frame when the app fails to compile, and shows the holding state (AE10)', () => {
    const { container } = setup({ turnRunning: true,  compileState: 'failed' })

    const cover = coverEl(container)
    expect(cover).toBeTruthy()
    expect(cover.textContent).toMatch(HOLDING)
    // The frame stays MOUNTED underneath — covering is not unmounting. Unmounting it would
    // throw away the document that is about to recover, and would make the HMR socket that
    // recovers it reconnect from scratch.
    expect(container.querySelector('iframe')).toBeTruthy()
    // …and nothing from the framework's own screen is reproduced here. This pane renders one
    // sentence; it never renders error text, a file path, or a stack.
    expect(container.textContent).not.toMatch(/unhandled runtime error|module not found|\.tsx/i)
  })

  it('covers an app built before any of this shipped — no version is ever consulted (AE14)', () => {
    // THE FLEET ASSERTION. The cover takes no prop describing the app, its framework version or
    // its image; it is driven purely by a signal about compilation. That is what makes it the
    // only mechanism that reaches the apps already out there, and this test fails the moment
    // someone gates it on something the existing fleet cannot report.
    const { container, rerender } = setup({ turnRunning: true,  compileState: 'failed' })
    expect(coverEl(container)).toBeTruthy()

    rerender(<LivePreview turnRunning previewUrl={SANDBOX_URL} status="ready" compileState="clean" />)
    expect(coverEl(container)).toBeFalsy()
    expect(container.querySelector('iframe')).toBeTruthy() // liveness: the pane still renders
  })

  it('HOLDS the cover when the signal goes unknown — absent is never good news', () => {
    // The fail-closed arm, and the single most important assertion in this file. `unknown` is
    // what a container reports when nothing has connected, when the socket is down, and — for
    // every app provisioned before the signal existed — permanently. Clearing on it would
    // uncover the exact screen this cover exists to hide.
    const { container, rerender } = setup({ turnRunning: true,  compileState: 'failed' })
    expect(coverEl(container)).toBeTruthy()

    rerender(<LivePreview turnRunning previewUrl={SANDBOX_URL} status="ready" compileState="unknown" />)
    expect(coverEl(container)).toBeTruthy()
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('holds the cover DOWN on unknown too — fail-closed means hold, not raise', () => {
    // The other direction, and it matters just as much: an unknown reading must not throw a
    // holding card over a perfectly healthy app the citizen is using.
    const { container, rerender } = setup({ turnRunning: true,  compileState: 'clean' })
    expect(coverEl(container)).toBeFalsy()

    rerender(<LivePreview turnRunning previewUrl={SANDBOX_URL} status="ready" compileState="unknown" />)
    expect(coverEl(container)).toBeFalsy()
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('holds the cover down when nothing has been reported at all', () => {
    // `null` is "no signal on this turn", which is how the pane behaved before the cover
    // existed. It must not raise a cover nobody asked for.
    const { container } = setup({ turnRunning: true,  compileState: null })
    expect(coverEl(container)).toBeFalsy()
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('does not carry a cover across to a DIFFERENT app', () => {
    // The one exception to holding, and it is not a hole in it. Holding is fail-closed because
    // an absent signal says nothing about the app being covered; a new preview url means we are
    // not covering that app any more, and keeping the card up would be a claim about code this
    // container has never seen. Reachable by switching conversations in the same pane.
    const { container, rerender } = setup({ turnRunning: true,  compileState: 'failed' })
    expect(coverEl(container)).toBeTruthy()

    rerender(<LivePreview turnRunning previewUrl={SANDBOX_URL_2} status="ready" compileState={null} />)
    expect(coverEl(container)).toBeFalsy()
    expect(container.querySelector('iframe')).toBeTruthy() // liveness
  })

  it('re-derives the verdict for a new app even when the signal REPEATS the old value', () => {
    // ★ THE FOUR-VALUE TRAP. The signal has only four possible values, so "a new app" and "the
    // same verdict as the previous app" routinely coincide — a relaunch onto a container that is
    // still failing carries `failed` -> `failed` across the url change with no delta at all.
    // Reset-on-url and apply-on-verdict as two effects meant React skipped the verdict effect on
    // that path (its dep did not change), and the reset won uncontested: the pane uncovered
    // itself over a broken app, which is the exact failure this whole mechanism exists to stop.
    const { container, rerender } = setup({ turnRunning: true,  compileState: 'failed' })
    expect(coverEl(container)).toBeTruthy()

    rerender(<LivePreview turnRunning previewUrl={SANDBOX_URL_2} status="ready" compileState="failed" />)
    expect(coverEl(container)).toBeTruthy()
    expect(container.querySelector('iframe')).toBeTruthy() // liveness
  })

  it('starts a new app uncovered even when the signal is byte-identical to the old app’s', () => {
    // Pins `previewUrl` in the effect's dependency list. Reached by holding a cover through an
    // `unknown` (app A broke, then its signal went quiet) and then switching apps while the
    // signal is still `unknown`: app B has reported nothing, so covering it would be a claim
    // about code nothing has looked at. Without the url dep the effect never re-runs here and
    // app B inherits app A's cover.
    const { container, rerender } = setup({ turnRunning: true,  compileState: 'failed' })
    rerender(<LivePreview turnRunning previewUrl={SANDBOX_URL} status="ready" compileState="unknown" />)
    expect(coverEl(container)).toBeTruthy() // held through the unknown, same app

    rerender(<LivePreview turnRunning previewUrl={SANDBOX_URL_2} status="ready" compileState="unknown" />)
    expect(coverEl(container)).toBeFalsy()
    expect(container.querySelector('iframe')).toBeTruthy() // liveness
  })

  it('applies the new app’s own verdict when the url and the signal change together', () => {
    const { container, rerender } = setup({ turnRunning: true,  compileState: 'clean' })
    expect(coverEl(container)).toBeFalsy()

    rerender(<LivePreview turnRunning previewUrl={SANDBOX_URL_2} status="ready" compileState="failed" />)
    expect(coverEl(container)).toBeTruthy()
  })

  it('clears the cover on an affirmative clean, and stops intercepting the frame', () => {
    const { container, rerender } = setup({ turnRunning: true,  compileState: 'building' })
    expect(coverEl(container)).toBeTruthy()

    rerender(<LivePreview turnRunning previewUrl={SANDBOX_URL} status="ready" compileState="clean" />)
    expect(coverEl(container)).toBeFalsy()
    // Cleared means GONE, not transparent: an invisible element over the frame would swallow
    // every click the citizen makes on their own app.
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('escalates the wording exactly once, at the pinned interval, and never again', () => {
    vi.useFakeTimers()
    try {
      const { container } = setup({ turnRunning: true,  compileState: 'building' })
      expect(coverEl(container).textContent).toMatch(HOLDING)

      act(() => vi.advanceTimersByTime(ESCALATE_MS - 1))
      expect(coverEl(container).textContent).toMatch(HOLDING)

      act(() => vi.advanceTimersByTime(1))
      expect(coverEl(container).textContent).toMatch(HOLDING_SLOW)

      // A card that keeps re-narrating itself reads as broken. There is one escalation.
      act(() => vi.advanceTimersByTime(ESCALATE_MS * 3))
      expect(coverEl(container).textContent).toMatch(HOLDING_SLOW)
    } finally {
      vi.useRealTimers()
    }
  })

  it('re-arms the escalation for a NEW cover rather than opening in a stale complaint', () => {
    vi.useFakeTimers()
    try {
      const { container, rerender } = setup({ turnRunning: true,  compileState: 'building' })
      act(() => vi.advanceTimersByTime(ESCALATE_MS))
      expect(coverEl(container).textContent).toMatch(HOLDING_SLOW)

      rerender(<LivePreview turnRunning previewUrl={SANDBOX_URL} status="ready" compileState="clean" />)
      rerender(<LivePreview turnRunning previewUrl={SANDBOX_URL} status="ready" compileState="failed" />)
      expect(coverEl(container).textContent).toMatch(HOLDING)
    } finally {
      vi.useRealTimers()
    }
  })

  it('covers a fresh mount mid-build — a manual page refresh must not land on the error screen', () => {
    // Reloading the tab while a build is running remounts this component from nothing. The
    // cover is derived from the CURRENT signal, not from a transition, so the very first paint
    // after a refresh is already covered.
    const { container } = setup({ turnRunning: true,  compileState: 'failed' })
    expect(coverEl(container)).toBeTruthy()
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('loses to a restore in flight — the restoring card wins and no cover is drawn', () => {
    const { container } = setup({ turnRunning: true,  compileState: 'failed', relaunching: true })
    expect(screen.getAllByText(/Restoring your app/i).length).toBeGreaterThan(0) // liveness for the winner
    expect(coverEl(container)).toBeFalsy()
  })

  it('loses to a terminal session and to a container that is not serving', () => {
    const terminal = render(<LivePreview turnRunning previewUrl={SANDBOX_URL} status="ended" compileState="failed" />)
    expect(coverEl(terminal.container)).toBeFalsy()
    expect(terminal.container.textContent).toMatch(/preview/i) // liveness: the placeholder rendered
    cleanup()

    const asleep = render(
      <LivePreview turnRunning previewUrl={SANDBOX_URL} status="ready" previewState="asleep" compileState="failed" />,
    )
    expect(coverEl(asleep.container)).toBeFalsy()
    expect(screen.getAllByText(/Your workspace is asleep/i).length).toBeGreaterThan(0) // liveness
  })

  it('beats the frame-load wait: two waits are never on screen at once', () => {
    // `showLoading` is true here (the frame is mounted and its `load` has not fired), and so is
    // the cover. The file's existing rule is that the waits share one anchor so they can never
    // co-exist; the cover joins that rule rather than becoming a third card stacked on them.
    const { container } = setup({ turnRunning: true,  compileState: 'building' })
    expect(coverEl(container)).toBeTruthy()
    expect(screen.queryByText(/Starting your app/i)).toBeNull()
    expect(container.querySelector('iframe')).toBeTruthy() // liveness
  })

  it('announces the holding state through the pane’s ONE live region, and gives it back', () => {
    const { container, rerender } = setup({ turnRunning: true,  compileState: 'failed' })
    const regions = container.querySelectorAll('[role="status"]')
    expect(regions).toHaveLength(1) // no second live region is introduced
    expect(regions[0].textContent).toMatch(HOLDING)

    rerender(<LivePreview turnRunning previewUrl={SANDBOX_URL} status="ready" compileState="clean" />)
    expect(container.querySelectorAll('[role="status"]')).toHaveLength(1)
    expect(container.querySelector('[role="status"]').textContent).not.toMatch(HOLDING)
  })

  it('announces the escalated wording too, rather than going quiet as the wait gets longer', () => {
    vi.useFakeTimers()
    try {
      const { container } = setup({ turnRunning: true,  compileState: 'building' })
      act(() => vi.advanceTimersByTime(ESCALATE_MS))
      expect(container.querySelector('[role="status"]').textContent).toMatch(HOLDING_SLOW)
    } finally {
      vi.useRealTimers()
    }
  })
})

// ---------------------------------------------------------------------------------------
// U7 (R13) — the holding state stops when the work does
// U10 (R11) — the frame is revealed on the verdict AND the load, never on the load alone
// ---------------------------------------------------------------------------------------

/** The device card carries the reveal. `opacity-100` is the revealed state; `opacity-0` is
 *  mounted-but-hidden, which is deliberately NOT unmounted — an iframe that never mounts never
 *  loads, and `load` is the only thing that can reveal it. */
function deviceCard(container) {
  return container.querySelector('[data-testid="device-card"]')
}

function loadTheFrame(container) {
  act(() => {
    fireEvent.load(container.querySelector('iframe'))
  })
}

describe('LivePreview — the holding state stops when the turn does (U7/R13)', () => {
  it('says the app is not running once no turn is in flight, and stops claiming progress', () => {
    // THE FAILURE THIS CLOSES. "Putting the latest change together…" is true for exactly as long
    // as a turn is running. Left up after one ends it becomes a progress state that never
    // resolves — the citizen's only way to learn the build was over is to wait long enough to
    // stop believing it, which is the shape of the nine-minute false success inverted.
    const { container } = setup({ compileState: 'failed', turnRunning: false })

    const cover = coverEl(container)
    expect(cover).toBeTruthy() // LIVENESS: still covering; what is behind it is still an error
    expect(cover.textContent).toMatch(IDLE_BROKEN)
    expect(cover.textContent).not.toMatch(HOLDING)
  })

  it('does NOT tell a healthy idle app that it stopped — `building` is not `failed`', () => {
    // ★ The two idle causes are opposites. `building` is published for any on-demand route
    // compile inside a running app, so a single "your app isn't running" sentence would be shown
    // over a working, completed build every time the citizen clicked through to a new page.
    const { container } = setup({ compileState: 'building', turnRunning: false })

    const cover = coverEl(container)
    expect(cover).toBeTruthy() // LIVENESS
    expect(cover.textContent).toMatch(IDLE_BUSY)
    expect(cover.textContent).not.toMatch(IDLE_BROKEN)
  })

  it('switches wording when a running turn ends, without uncovering the error screen', () => {
    const { container, rerender } = setup({ compileState: 'failed', turnRunning: true })
    expect(coverEl(container).textContent).toMatch(HOLDING)

    rerender(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="failed" turnRunning={false} />,
    )

    expect(coverEl(container).textContent).toMatch(IDLE_BROKEN)
    // The cover STAYS. Clearing it would trade a lie about progress for a lie about the app —
    // behind it is the framework's error screen today and a blank page once that is suppressed.
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('does not escalate an idle cover — the escalation is the same claim with more emphasis', () => {
    vi.useFakeTimers()
    try {
      const { container } = setup({ compileState: 'building', turnRunning: false })
      act(() => vi.advanceTimersByTime(ESCALATE_MS * 2))
      expect(coverEl(container).textContent).toMatch(IDLE_BUSY)
      expect(coverEl(container).textContent).not.toMatch(HOLDING_SLOW)
    } finally {
      vi.useRealTimers()
    }
  })

  it('announces the idle wording through the same single live region', () => {
    const { container } = setup({ compileState: 'failed', turnRunning: false })
    const regions = container.querySelectorAll('[role="status"]')
    expect(regions).toHaveLength(1) // still no second live region
    expect(regions[0].textContent).toMatch(IDLE_BROKEN)
  })

  it('re-arms the escalation for a NEW turn rather than opening in a stale complaint', () => {
    // ★ The escalated wording is a claim about how long THIS change has been coming together,
    // and `covered` does not fall between turns: a failed turn leaves the compile state at
    // `failed`. Armed off `covered` alone, a cover raised twenty seconds into turn 1 was still
    // armed when turn 2 began, so the new turn opened by telling the citizen it was already
    // taking longer than usual.
    vi.useFakeTimers()
    try {
      const { container, rerender } = setup({ compileState: 'building', turnRunning: true })
      act(() => vi.advanceTimersByTime(ESCALATE_MS))
      expect(coverEl(container).textContent).toMatch(HOLDING_SLOW)

      // The turn ends, then a new one starts — the compile state never left `building`.
      rerender(
        <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="building" turnRunning={false} />,
      )
      rerender(
        <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="building" turnRunning />,
      )

      expect(coverEl(container).textContent).toMatch(HOLDING)
      expect(coverEl(container).textContent).not.toMatch(HOLDING_SLOW)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('LivePreview — the reveal is earned twice over (U10/R11)', () => {
  it('reveals when the verdict passes AND the frame loads', () => {
    const { container } = setup({ compileState: 'clean' })
    expect(deviceCard(container).className).toMatch(/opacity-0/)

    loadTheFrame(container)

    expect(deviceCard(container).className).toMatch(/opacity-100/)
  })

  it('does NOT reveal on the load event alone when the verdict says the app failed', () => {
    // ★ THE POINT OF THE UNIT. `load` fires for a 500 exactly as it does for a 200, this pane
    // cannot read a cross-origin status, and the in-container proxy emits its handle-block
    // headers even on the 502 it returns when the dev server is down — so `load` fires on that
    // too. On its own it is not evidence of anything.
    const { container } = setup({ compileState: 'failed', turnRunning: true })

    loadTheFrame(container)

    expect(deviceCard(container).className).toMatch(/opacity-0/)
    expect(coverEl(container)).toBeTruthy() // LIVENESS: the pane rendered and is covering
  })

  it('does not reveal on a passing verdict alone — the document still has to load', () => {
    const { container } = setup({ compileState: 'clean' })
    expect(deviceCard(container)).toBeTruthy() // LIVENESS: the card is mounted, just hidden
    expect(deviceCard(container).className).toMatch(/opacity-0/)
  })

  it('RETRACTS a reveal when the verdict flips to failed (R4), and the cover explains', () => {
    const { container, rerender } = setup({ compileState: 'clean' })
    loadTheFrame(container)
    expect(deviceCard(container).className).toMatch(/opacity-100/)

    rerender(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="failed" turnRunning />,
    )

    expect(deviceCard(container).className).toMatch(/opacity-0/)
    expect(coverEl(container).textContent).toMatch(HOLDING)
  })

  it('does NOT retract a reveal on an unanswerable verdict (AE8)', () => {
    // `unknown` HOLDS whatever is showing rather than moving it — the same fail-closed rule that
    // stops an absent signal uncovering a broken app stops it hiding a working one.
    const { container, rerender } = setup({ compileState: 'clean' })
    loadTheFrame(container)
    expect(deviceCard(container).className).toMatch(/opacity-100/)

    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="unknown" />)

    expect(deviceCard(container).className).toMatch(/opacity-100/)
  })

  it('introduces no overlay of its own — the reveal is opacity and nothing else', () => {
    // An inertness guard, paired with liveness. This unit controls the frame's transparency;
    // everything visible ABOVE the frame belongs to the cover, and a second surface here would
    // be one more thing that can contradict it.
    const revealed = setup({ compileState: 'clean' })
    loadTheFrame(revealed.container)
    const overlaysWhenRevealed = revealed.container.querySelectorAll('.absolute.inset-0').length
    cleanup()

    const hidden = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="failed" turnRunning />,
    )
    loadTheFrame(hidden.container)

    expect(deviceCard(hidden.container)).toBeTruthy() // LIVENESS
    // Exactly ONE more full-bleed element than the revealed case: the cover. Not two.
    expect(hidden.container.querySelectorAll('.absolute.inset-0').length).toBe(
      overlaysWhenRevealed + 1,
    )
  })

  it('documents the null/unknown concession rather than leaving it to be discovered', () => {
    // ★ WHAT THIS UNIT DOES NOT CLOSE, pinned so it cannot drift silently. `covered` moves on
    // building/failed/clean and HOLDS on `unknown` and `null` — so with no compile verdict ever
    // reported, the load still reveals on its own, exactly as it did before this unit. That is
    // every container on an image older than the compile endpoint, and the opening moments of
    // every turn.
    //
    // It is a deliberate compatibility concession: gating on a POSITIVE verdict would leave the
    // whole existing fleet's preview permanently blank, which is worse than the failure being
    // fixed. If someone later decides to close it, this test is what tells them they are
    // changing a decision rather than fixing an oversight.
    for (const compileState of [null, 'unknown']) {
      const view = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState={compileState} />,
      )
      loadTheFrame(view.container)
      expect(deviceCard(view.container).className).toMatch(/opacity-100/)
      cleanup()
    }
  })
})

