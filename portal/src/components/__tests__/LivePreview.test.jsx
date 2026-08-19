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

// A message that passes the C8 origin guard (comes from the sandbox origin).
function fromSandbox(data) {
  return new MessageEvent('message', { data, origin: SANDBOX_ORIGIN })
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

  it('forwards ONLY an origin-valid inbound message to the Wave-1 receiver seam', () => {
    const onFrameMessage = vi.fn()
    setup({ onFrameMessage })
    window.dispatchEvent(fromSandbox({ kind: 'client_error' }))
    expect(onFrameMessage).toHaveBeenCalledWith({ kind: 'client_error' })
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
    // A previewReady-style message that USED to round-trip code back must now do nothing.
    window.dispatchEvent(fromSandbox({ previewReady: true }))
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

  it('empty state when there is no status and no previewUrl', () => {
    const { container } = render(<LivePreview previewUrl={null} status={null} />)
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).toMatch(/preview will appear here/i)
  })

  it('the "still iterating" overlay shows only while a LIVE preview keeps receiving activity', () => {
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" iterating />)
    expect(container.textContent).toMatch(/still iterating/i)
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" iterating={false} />)
    expect(container.textContent).not.toMatch(/still iterating/i)
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
})

describe('LivePreview — relaunch a torn-down preview (#43)', () => {
  it('offers a Relaunch button on the terminal placeholder when the project HAS a saved build', () => {
    // R5: the affordance needs the server-confirmed claim now — onRelaunch alone no longer
    // conjures a button for a build that may not exist.
    const onRelaunch = vi.fn()
    render(<LivePreview previewUrl={null} status="ended" onRelaunch={onRelaunch} hasSavedBuild />)
    const button = screen.getByRole('button', { name: /relaunch preview/i })
    fireEvent.click(button)
    expect(onRelaunch).toHaveBeenCalledTimes(1)
  })

  it('offers Relaunch on a FAILED build too (its saved snapshot may still be restorable)', () => {
    const onRelaunch = vi.fn()
    render(<LivePreview previewUrl={null} status="failed" onRelaunch={onRelaunch} hasSavedBuild />)
    expect(screen.getByRole('button', { name: /relaunch preview/i })).toBeTruthy()
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

describe('LivePreview — relaunch from PROJECT state, not this transcript (finding #1 + N7)', () => {
  it('a project the server CONFIRMS has a saved build offers Relaunch from a fresh chat', () => {
    // status null + no previewUrl = the empty state a fresh chat lands in.
    const onRelaunch = vi.fn()
    const { container } = render(<LivePreview hasSavedBuild onRelaunch={onRelaunch} />)
    expect(container.textContent).toMatch(/already has a saved build/i)
    fireEvent.click(screen.getByRole('button', { name: /relaunch preview/i }))
    expect(onRelaunch).toHaveBeenCalledTimes(1)
  })

  it('a project WITHOUT a saved build keeps the plain empty copy — no phantom relaunch', () => {
    const { container } = render(<LivePreview onRelaunch={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(container.textContent).toMatch(/submit a prompt to start a build/i)
  })

  it('N7: an UNKNOWN answer (null) makes no claim in either direction', () => {
    // The server could not reach the object store. Reading `null` as "yes" offers a button
    // that 404s; reading it as "no" hides a Relaunch that would have worked. Say nothing.
    const { container } = render(<LivePreview hasSavedBuild={null} onRelaunch={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(container.textContent).not.toMatch(/already has a saved build/i)
    expect(container.textContent).toMatch(/submit a prompt to start a build/i)
  })

  it('N7: a 404 after the click is SHOWN, not silently swallowed', () => {
    // The old behaviour hid the button with no message at all: the user pressed Relaunch and
    // the affordance simply vanished. That silence covered our own false claim. With a
    // truthful predicate a 404 is genuinely exceptional, so it gets said out loud.
    const { container } = render(
      <LivePreview
        hasSavedBuild
        onRelaunch={vi.fn()}
        relaunchError={{ kind: 'not_found', message: 'No saved build to relaunch. Build the app first.' }}
      />,
    )
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(screen.getByRole('alert').textContent).toMatch(/no longer available/i)
    expect(container.textContent).toMatch(/submit a prompt to start a build/i)
  })

  it('a retryable relaunch failure from the empty state keeps the button (U6 matrix holds here too)', () => {
    render(
      <LivePreview
        hasSavedBuild
        onRelaunch={vi.fn()}
        relaunchError={{ kind: 'unavailable', message: 'Sandbox unavailable. Please try again later or contact the admin' }}
      />,
    )
    expect(screen.getByRole('alert').textContent).toMatch(/try again later/i)
    expect(screen.getByRole('button', { name: /relaunch preview/i })).toBeTruthy()
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

  it('503 unavailable shows the transient copy WITH the button restored for a retry', () => {
    const onRelaunch = vi.fn()
    render(
      <LivePreview
        previewUrl={null}
        status="ended"
        onRelaunch={onRelaunch}
        hasSavedBuild
        relaunchError={{ kind: 'unavailable', message: 'Sandbox unavailable. Please try again later or contact the admin' }}
      />,
    )
    expect(screen.getByRole('alert').textContent).toMatch(/try again later/i)
    const button = screen.getByRole('button', { name: /relaunch preview/i })
    fireEvent.click(button)
    expect(onRelaunch).toHaveBeenCalledTimes(1)
  })

  it('5xx failed shows the failure copy with the button restored', () => {
    render(
      <LivePreview
        previewUrl={null}
        status="ended"
        onRelaunch={vi.fn()}
        hasSavedBuild
        relaunchError={{ kind: 'failed', message: 'Failed to relaunch the preview' }}
      />,
    )
    expect(screen.getByRole('alert').textContent).toMatch(/failed to relaunch/i)
    expect(screen.getByRole('button', { name: /relaunch preview/i })).toBeTruthy()
  })

  it('labels the button "Relaunch last saved version" when the newest build failed (U6/F1)', () => {
    render(<LivePreview previewUrl={null} status="failed" onRelaunch={vi.fn()} hasSavedBuild lastBuildFailed />)
    expect(screen.getByRole('button', { name: /relaunch last saved version/i })).toBeTruthy()
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

describe('LivePreview — the frame is revealed on load, never on a timer (U5/R3)', () => {
  function card(container) {
    return container.querySelector('[data-testid="device-card"]')
  }

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

  it('a frame that never loads degrades to a LABELLED state with Relaunch — never a bare white card', () => {
    vi.useFakeTimers()
    try {
      const onRelaunch = vi.fn()
      const { container } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ready" onRelaunch={onRelaunch} hasSavedBuild />,
      )
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS + 1))
      expect(card(container).className).toMatch(/opacity-0/) // still nothing painted, so still not revealed
      expect(container.textContent).toMatch(/taking longer than usual/i)
      fireEvent.click(screen.getByRole('button', { name: /relaunch preview/i }))
      expect(onRelaunch).toHaveBeenCalledTimes(1)
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
  it('after the cap with no recovery, collapses to "preview unavailable" + Relaunch (no forever spinner)', () => {
    vi.useFakeTimers()
    try {
      const onRelaunch = vi.fn()
      const { container } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ended" completedLive reconnecting onRelaunch={onRelaunch} hasSavedBuild />,
      )
      expect(container.textContent).toMatch(/reconnecting/i) // before the cap
      act(() => vi.advanceTimersByTime(20001))
      expect(container.textContent).toMatch(/preview unavailable/i) // the bounded terminal
      expect(container.textContent).not.toMatch(/reconnecting to your preview/i)
      fireEvent.click(screen.getByRole('button', { name: /relaunch preview/i }))
      expect(onRelaunch).toHaveBeenCalledTimes(1)
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

  it('both branches with hasSavedBuild=true still offer Relaunch (the built-but-unsubmitted draft)', () => {
    // Terminal:
    const first = render(
      <LivePreview previewUrl={null} status="ended" onRelaunch={vi.fn()} hasSavedBuild />,
    )
    expect(screen.getByRole('button', { name: /relaunch preview/i })).toBeTruthy()
    first.unmount()
    // Unavailable:
    vi.useFakeTimers()
    try {
      render(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ended"
          completedLive
          reconnecting
          onRelaunch={vi.fn()}
          hasSavedBuild
        />,
      )
      act(() => vi.advanceTimersByTime(20001))
      expect(screen.getByRole('button', { name: /relaunch preview/i })).toBeTruthy()
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

  it('the relaunch button still works from inside the compact card', () => {
    const onRelaunch = vi.fn()
    const { container } = render(<LivePreview previewUrl={null} status="ended" onRelaunch={onRelaunch} hasSavedBuild />)
    const card = container.querySelector('[data-testid="preview-ended-card"]')
    const button = screen.getByRole('button', { name: /relaunch preview/i })
    expect(card.contains(button)).toBe(true)
    fireEvent.click(button)
    expect(onRelaunch).toHaveBeenCalledTimes(1)
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

  it('the relaunch button still works from inside the compact unavailable card', () => {
    vi.useFakeTimers()
    try {
      const onRelaunch = vi.fn()
      const { container } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ended" completedLive reconnecting onRelaunch={onRelaunch} hasSavedBuild />,
      )
      act(() => vi.advanceTimersByTime(20001))
      const card = container.querySelector('[data-testid="preview-unavailable-card"]')
      const button = screen.getByRole('button', { name: /relaunch preview/i })
      expect(card.contains(button)).toBe(true)
      fireEvent.click(button)
      expect(onRelaunch).toHaveBeenCalledTimes(1)
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

describe('LivePreview — issue #92 identity handshake relay (F2, R7, R8)', () => {
  const APP_ID = 'app-123'

  function identityRequest(source) {
    return new MessageEvent('message', {
      data: { type: 'bial:identity:request', appId: APP_ID },
      origin: SANDBOX_ORIGIN,
      source,
    })
  }

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('mints and replies to the EXACT requesting frame at ITS origin (never a broadcast)', async () => {
    const fakeSource = { postMessage: vi.fn() }
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ assertion: 'signed.assertion.jwt' }),
    })
    setup({ appId: APP_ID })

    await act(async () => {
      window.dispatchEvent(identityRequest(fakeSource))
      await Promise.resolve()
    })

    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/auth/app-assertion/preview',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: JSON.stringify({ app_id: APP_ID }),
      }),
    )
    expect(fakeSource.postMessage).toHaveBeenCalledWith(
      { type: 'bial:identity:assertion', assertion: 'signed.assertion.jwt' },
      SANDBOX_ORIGIN,
    )
  })

  it('ignores a request from a wrong origin — never mints for an untrusted sender', async () => {
    const fakeSource = { postMessage: vi.fn() }
    global.fetch = vi.fn()
    setup({ appId: APP_ID })

    await act(async () => {
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'bial:identity:request', appId: APP_ID },
          origin: 'https://evil.example',
          source: fakeSource,
        }),
      )
      await Promise.resolve()
    })

    expect(fetch).not.toHaveBeenCalled()
    expect(fakeSource.postMessage).not.toHaveBeenCalled()
  })

  it('does nothing when no appId is known yet — nothing to mint for', async () => {
    const fakeSource = { postMessage: vi.fn() }
    global.fetch = vi.fn()
    setup({ appId: null })

    await act(async () => {
      window.dispatchEvent(identityRequest(fakeSource))
      await Promise.resolve()
    })

    expect(fetch).not.toHaveBeenCalled()
  })

  it('a failed mint (non-2xx) replies with nothing — fail closed, not a broken assertion', async () => {
    const fakeSource = { postMessage: vi.fn() }
    global.fetch = vi.fn().mockResolvedValue({ ok: false })
    setup({ appId: APP_ID })

    await act(async () => {
      window.dispatchEvent(identityRequest(fakeSource))
      await Promise.resolve()
    })

    expect(fakeSource.postMessage).not.toHaveBeenCalled()
  })
})
