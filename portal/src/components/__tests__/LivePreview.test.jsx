import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, fireEvent, screen, act } from '@testing-library/react'
import LivePreview from '../LivePreview.jsx'

afterEach(cleanup)

// The Phase-2 preview is a genuinely CROSS-ORIGIN sandbox frame. `previewUrl` is the
// sandbox FQDN root; `previewOrigin` is what the inbound origin guard validates.
const SANDBOX_URL = 'https://app-xyz.example.azurecontainerapps.io/'
const SANDBOX_ORIGIN = 'https://app-xyz.example.azurecontainerapps.io'
const SANDBOX_URL_2 = 'https://app-abc.example.azurecontainerapps.io/'
// …and ITS origin, which is a DIFFERENT one. Named rather than inlined because the second url is
// how this file says "another app", and the trust gate is recomputed from whichever url is framed
// right now — so the beacon that was trusted a moment ago is rejected the instant the pane
// switches, and a test that vouches for the second frame has to speak as the second app.
const SANDBOX_ORIGIN_2 = 'https://app-abc.example.azurecontainerapps.io'
// ★ THE SHARED-HOSTNAME ADDRESS SHAPE, which is the whole reason the beacon carries a path.
// BIAL refused a wildcard certificate, so every generated app is served from ONE name and told
// apart by its PATH — one origin, one certificate, and two apps that a single mis-addressed
// message could confuse. Two of them here, so a beacon can be made to speak for the wrong one.
const APPS_ORIGIN = 'https://apps.example'
const APPS_URL_A = 'https://apps.example/a/sbx-aaaa/'
const APPS_PATH_B = '/a/sbx-bbbb/'

function setup(props = {}) {
  const view = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" {...props} />)
  const iframe = view.container.querySelector('iframe')
  return { ...view, iframe }
}

/** The device card that carries the reveal's opacity — the handle every reveal assertion uses. */
function card(container) {
  return container.querySelector('[data-testid="device-card"]')
}

// A message that passes BOTH halves of the guard: the sandbox origin AND the window of the
// frame this pane actually rendered. Origin alone stopped being sufficient once every generated
// app began sharing one hostname, so `source` is no longer optional decoration on these events.
// The origin is a parameter with a default rather than a constant, for the one case that needs it:
// a pane that has switched to the second app is validating against the second app's origin.
function fromSandbox(data, source, origin = SANDBOX_ORIGIN) {
  return new MessageEvent('message', { data, origin, source })
}

// THE WIRE, matched by value on both sides (`sandbox/template/instrumentation-client.ts`). The
// framed app posts the beacon to its parent once its root layout has rendered, and answers a ping
// with the same message. Written out as literals rather than imported from the component: a test
// that imports the constant it is pinning asserts only that the code equals itself, and this pair
// is a CONTRACT WITH ANOTHER REPOSITORY'S FILE — the value drifting is exactly the failure.
//
// ★ AND THE BEACON NAMES THE PATH IT PAINTED, A FIELD THAT IS REQUIRED RATHER THAN DECORATIVE.
// With one hostname for the whole fleet, `e.source` proves only “the window I framed”; the path is
// the only thing that says WHICH app painted in it. So a message that omits it is not a beacon at
// all — it is an ordinary frame message, and it is treated as one.
const MOUNTED_BEACON = { type: 'bial:app-mounted', path: '/' }
const PING = { type: 'bial:ping' }

/**
 * The framed document vouching for itself — the ONLY thing in this pane that reveals a frame.
 *
 * Built through `fromSandbox` so it carries BOTH halves of the provenance gate (the origin of the
 * url currently framed AND this pane's own frame window); a beacon missing either half is a
 * different scenario, and the tests that need one build it by hand.
 *
 * `path` defaults to the root every sandbox URL in this file frames. The tests that care about the
 * IDENTITY half — a beacon with no path, or one reporting a sibling app on the same host — pass
 * their own, and every hand-built beacon below carries the field for the same reason.
 */
function vouch(container, origin = SANDBOX_ORIGIN, path = '/') {
  const iframe = container.querySelector('iframe')
  act(() => {
    window.dispatchEvent(fromSandbox({ ...MOUNTED_BEACON, path }, iframe.contentWindow, origin))
  })
  return iframe
}

describe('LivePreview — cross-origin sandbox preview frame', () => {
  it('frames the cross-origin previewUrl (not the retired same-origin /preview)', () => {
    const { iframe } = setup()
    expect(iframe).toBeTruthy()
    expect(iframe.getAttribute('src')).toBe(SANDBOX_URL)
  })

  it('uses the sandbox token list — allow-same-origin for the cross-origin next dev app, top-nav/popups withheld', () => {
    const { iframe } = setup()
    const sandbox = iframe.getAttribute('sandbox')
    expect(sandbox).toBe('allow-scripts allow-same-origin allow-forms allow-downloads')
    expect(sandbox).not.toContain('allow-top-navigation') // withheld: no top-nav hijack of the portal tab
    expect(sandbox).not.toContain('allow-popups') // withheld: no popup-phishing of the portal tab
  })

  it('REJECTS a message from a wrong origin — forwards nothing (origin guard, pinned)', () => {
    const onFrameMessage = vi.fn()
    setup({ onFrameMessage })
    window.dispatchEvent(new MessageEvent('message', { data: { hello: true }, origin: 'https://evil.example' }))
    expect(onFrameMessage).not.toHaveBeenCalled()
  })

  it('forwards a message from the framed app’s OWN window to the receiver seam', () => {
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

  // `new URL(url).origin` is the STRING "null" for an opaque-origin URL (a data: URL,
  // about:blank, a sandboxed iframe without allow-same-origin) — not the value null, and that
  // string is truthy. Without `originOf()` folding it to null, it would pass the
  // `!previewOriginRef.current` guard and trust any opaque sender (whose real `e.origin` is also
  // the string "null") as the sandbox. Not reachable via the real control-plane today (it only
  // returns an https FQDN) — pinned as a contract, not a currently-exploitable path.
  it('REJECTS messages even from the literal origin "null" — an opaque previewUrl must not trust opaque senders', () => {
    const onFrameMessage = vi.fn()
    // data: is a genuinely opaque-origin URL; new URL(...).origin for it is the string "null".
    render(<LivePreview previewUrl="data:text/html,x" status="ready" onFrameMessage={onFrameMessage} />)
    window.dispatchEvent(new MessageEvent('message', { data: { hello: true }, origin: 'null' }))
    expect(onFrameMessage).not.toHaveBeenCalled()
  })

  it('the single-file relay is INERT — no outbound postMessage of code/config/token occurs', () => {
    const { iframe } = setup({ status: 'ready' })
    const post = vi.spyOn(iframe.contentWindow, 'postMessage')
    // A previewReady-style message that USED to round-trip code back must now do nothing. Sent
    // from the frame's own window on purpose: source-less, it would be dropped by the gate
    // before reaching any code, and this test would assert inertness over a message nothing read.
    //
    // ★ THE ONE OUTBOUND MESSAGE THIS PANE DOES SEND IS THE PING, and it is not this path: it goes
    // out on the frame's `load`, carries no code, no config and no token, and is pinned in "a load
    // ASKS the document to vouch" below. No `load` is fired here, so the spy staying silent means
    // "an inbound message provokes nothing", never "this pane never posts".
    window.dispatchEvent(fromSandbox({ previewReady: true }, iframe.contentWindow))
    expect(post).not.toHaveBeenCalled()
  })
})

describe('LivePreview — reload semantics (no HMR-socket leak)', () => {
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
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" onFrameMessage={() => {}} />)
    const second = container.querySelector('iframe')
    // Same node. The key is `previewUrl` plus the reload nonces, so a prop that moves nothing about
    // the document must not reload it and leak the framed app's HMR socket.
    expect(second).toBe(first)
    expect(second.getAttribute('src')).toBe(SANDBOX_URL)
  })

  it('★ the app starting to answer re-requests the document, even though the URL never changed', () => {
    // THE FOUR-RELOADS BUG. The address the poll publishes while the container is merely CREATED
    // and the address published once the app is actually serving are the SAME STRING. So across
    // the moment the app comes up, nothing about the frame changes: React keeps the DOM node and
    // the browser never asks again. During a first build the document that loaded first is the
    // router's 502 page — the app was not listening yet — and the citizen keeps looking at "This
    // app isn't running right now" over an app that is now running perfectly. Reported from
    // production as needing four reloads.
    //
    // Mutation check: delete the `status === 'ready'` nonce effect in LivePreview and this goes
    // red with `second` being the same node — the stale document survives the app coming up.
    const { container, rerender } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="building" />,
    )
    const first = container.querySelector('iframe')
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    const second = container.querySelector('iframe')
    expect(second).not.toBe(first)
    // The liveness half: it re-requested the SAME address, which is the whole point — a different
    // address would have remounted anyway and proved nothing about this fix.
    expect(second.getAttribute('src')).toBe(SANDBOX_URL)
  })

  it('a pane that mounts straight into ready does NOT reload the document it just asked for', () => {
    // The guard on the effect above. Without the "there was a previous status" term, the first
    // render of an already-healthy app bumps the nonce and throws away a document the browser had
    // only just fetched — a wasted round trip, and a torn-down HMR socket, on every open of a
    // working app.
    //
    // ASSERTED ON THE KEY, NOT ON NODE IDENTITY, and that distinction is the whole test. The first
    // version compared `container.querySelector('iframe')` before and after an unrelated
    // re-render, and it could not fail: `render()` flushes effects before returning, so a wrongful
    // mount-time bump is already baked into the node you capture, and the second re-render changes
    // neither `status` nor `previewUrl`, so the effect does not even run. It passed with its guard
    // deleted. The nonce is what is being guarded, so the nonce is what to read.
    //
    // Mutation check: widen the effect's condition back to `wasStatus.current !== 'ready'` and
    // this goes red with a `#1.0` key — the bump that should never have happened, now visible.
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
      `${SANDBOX_URL}#0.0`,
    )
  })

  it('★ switching to another app mid-build fetches its document ONCE, not twice', () => {
    // This pane has no `key` on purpose, so it outlives a navigation — which means it can go from
    // one app still building straight to a DIFFERENT app already serving, in one update. Watching
    // the status alone, that reads as "the build finished" and bumps the nonce, so the new app's
    // document is requested once for the address and again for a build that was never its own:
    // two round trips and a visible flash for a single switch.
    //
    // Mutation check: drop `prev.url === previewUrl` from the effect's condition and this goes red
    // with a `#1.0` key — the second, unearned request made visible.
    const OTHER = 'https://app-abc.example.azurecontainerapps.io/'
    const { container, rerender } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="building" />,
    )
    rerender(<LivePreview previewUrl={OTHER} status="ready" />)
    expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(`${OTHER}#0.0`)
  })

  it('★ SENDING A MESSAGE does not tear the running app down and re-fetch it', () => {
    // THE REGRESSION THIS EFFECT NEARLY SHIPPED. On a warm container the transcript's `ended`
    // status is replaced by the live turn's own the moment a send starts, so every follow-up
    // message produces an `ended` → `ready` edge. Written as "the status became ready" the effect
    // fired on it, remounting the iframe: the citizen's running app re-fetched, its scroll
    // position and any form state discarded and its HMR socket cycled, on every message they sent
    // — to re-request a document that was never stale. Only provisioning/building → ready means
    // "the app was not answering and now is".
    //
    // Mutation check: widen the condition to `wasStatus.current !== 'ready'` and this goes red
    // with a `#1.0` key.
    const { container, rerender } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ended" serving />,
    )
    const before = container.querySelector('iframe').getAttribute('data-frame-key')
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" serving />)
    expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(before)
    // LIVENESS: the frame is genuinely still mounted, so the assertion above is about a document
    // that stayed rather than one that was never there.
    expect(container.querySelector('iframe')).toBeTruthy()
  })
})

describe('LivePreview — status-driven visuals, all five statuses', () => {
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

  // ★ THE TERMINAL PLACEHOLDER IS GONE, AND WHAT REPLACES IT IS AN EMPTY PANE — DELIBERATELY.
  //
  // It drew "The preview is no longer running", with a saved-build line under it, and it was this
  // file's fourth workspace verdict: a sentence about the citizen's app, told by the one component
  // that can only see a frame. `workspace/workspaceState.ts` computes that sentence once and
  // `AppPane` draws it, so the placeholder was a second author for it — and the composed product
  // does not even reach this component in those states, because the frame veto mounts it only on a
  // `running` reading.
  //
  // WHAT IS STILL PINNED HERE IS THE HALF THAT IS THIS FILE'S: a terminal status must not keep
  // framing a URL nobody is answering at. Post-ready teardown showing a painted corpse of the app
  // is the failure, and it is still a failure.
  it('★ failed / ended stop framing, and say NOTHING in the placeholder`s place', () => {
    for (const status of ['failed', 'ended']) {
      const { container, unmount } = render(<LivePreview previewUrl={null} status={status} />)
      expect(container.querySelector('iframe')).toBeNull()
      for (const retired of [/no longer running/i, /start a new build/i, /your saved app is still there/i]) {
        expect(container.textContent, `${status} still draws the placeholder`).not.toMatch(retired)
      }
      // ★ LIVENESS, AND IT HAS TO BE STRUCTURAL HERE because the expected screen is empty: an
      // absence sweep over a component that threw would pass every line above. The pane's permanent
      // live region is the one thing that exists in every state, so finding it mounted and silent
      // is what tells "rendered and had nothing to say" from "did not render".
      expect(container.querySelector('[role="status"]')?.getAttribute('aria-live')).toBe('polite')
      expect(container.querySelector('[role="status"]')?.textContent).toBe('')
      unmount()
    }
  })

  it('★ ended AFTER a framed preview collapses the frame — a dead URL is never left painted', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ended" />)
    // Terminal precedence: even with a previewUrl present, the pane must not keep framing a dead
    // sandbox. Without the pardon (`serving`) there is nothing to say the URL still answers.
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).not.toMatch(/no longer running/i)
    // LIVENESS, structural for the same reason as above.
    expect(container.querySelector('[role="status"]')?.textContent).toBe('')
  })

  // `showEmpty` IS GONE, and the empty-state copy this test used to look for ("...will appear
  // here") went with it: it moved to `AppPane`'s `NoFrame`, which is also the ONLY thing that can
  // put a citizen in this exact state now — `AppPane` mounts this component at all ONLY when the
  // address resolver has a URL, and renders `NoFrame` instead when it does not (`AppPane.tsx`:
  // `address.url ? <AppPaneHost /> : <NoFrame .../>`). So the honest claim left to make here is
  // not "here is the copy" (there is none) but "this component genuinely has nothing left to say
  // for it" — proven below by checking every kind of chrome it knows how to draw, not merely a
  // single sentence of copy. `workspaceState.test.ts` covers the state map that now owns it.
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

  // ★ THE THREE OVERLAYS ARE GONE FROM THE APP'S CANVAS, and this is the guard that
  // keeps them from coming back: no platform-drawn chip may sit at `absolute top-3 left-1/2`
  // while a turn keeps refining a live preview, because that is where every generated app draws
  // its own navigation — a chip there writes across the citizen's app.
  //
  // ASSERT-ABSENCE, PAIRED WITH LIVENESS — an empty pane would satisfy the absence on its own, so
  // the frame has to be found in the same breath.
  it('★ draws NOTHING over the framed app', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    expect(container.textContent).not.toMatch(/still working/i)
    // LIVENESS: the app really is framed, so the absence is a deletion rather than a blank pane.
    expect(container.querySelector('iframe')).toBeTruthy()
    // …and no platform-owned overlay is anchored over the frame's top-centre, whatever it says.
    // The geometric form of the same rule, so a NEW chip with different copy is caught too.
    expect(container.querySelectorAll('[data-testid="device-card"] .absolute')).toHaveLength(0)
  })
})

describe('LivePreview — the pardoned preview: a finished turn leaves the app framed', () => {
  it('ended + serving + previewUrl KEEPS the frame — and claims nothing about the build', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ended" serving />)
    const iframe = container.querySelector('iframe')
    expect(iframe).toBeTruthy() // the server pardoned the container; the URL is genuinely live
    expect(iframe.getAttribute('src')).toBe(SANDBOX_URL)
    expect(container.textContent).not.toMatch(/no longer running/i)
    // ★ AND THE CLAIM IS GONE WITH THE CHIP. The pane frames a live app; it does not also state a
    // build outcome — claiming one let a route where no build ever runs publish one.
    expect(container.textContent).not.toMatch(/build complete/i)
  })

  it('★ a serving container WITHOUT a previewUrl frames nothing — this pane draws nothing it cannot point at', () => {
    // Liveness with no address to frame is not a frame. It used to draw the terminal placeholder
    // here; the sentence for a workspace with nothing on screen is the map's now, and `AppPane`
    // does not mount this component without a resolved address at all.
    const { container } = render(<LivePreview previewUrl={null} status="ended" serving />)
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).not.toMatch(/no longer running/i)
    // LIVENESS: the permanent region is mounted and silent, which is what an empty pane looks like
    // as opposed to a component that failed to render.
    expect(container.querySelector('[role="status"]')?.textContent).toBe('')
  })

  it('★ the terminal sentence is UNREACHABLE while the container is serving, in both nodes', () => {
    // The rule, stated as the sentence a citizen actually reads. "The preview is no
    // longer running" is a claim about the container, and it must not be derivable from the turn
    // being over — a turn ending is not an app ending.
    //
    // BOTH NODES, because the pane says everything twice on purpose: the visible card and the
    // permanent live region. A version that stopped drawing the card but kept announcing it would
    // pass a text-only assertion while telling a screen-reader user their app was gone.
    //
    // ASSERT-ABSENCE, PAIRED WITH LIVENESS: the frame has to be found in the same breath, or an
    // empty pane satisfies both absences.
    const { container } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ended" serving previewState="alive" />,
    )

    expect(container.querySelector('iframe')).toBeTruthy()
    expect(screen.queryByTestId('preview-ended-card')).toBeNull()
    expect(screen.getByRole('status').textContent).not.toMatch(/no longer running/i)
    expect(container.textContent).not.toMatch(/no longer running/i)
  })

  it('★ a FAILED build stays framed when its container serves — and is never called complete', () => {
    // Widening liveness must not turn a failed build into a successful-looking one: the frame
    // stays because the container is up, and what the pane SAYS about the build comes from the
    // compile state, which covers the frame and names the failure. Two questions, two answers,
    // neither borrowed from the other.
    render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="failed"
        serving
        previewState="alive"
        compileState="failed"
      />,
    )

    // The build is named as broken, not as finished \u2014 and the sentence names THE PAGE now.
    //
    // \u2605 IT USED TO OPEN "Your app isn't running right now", which is word for word the claim the
    // apps router's own error page makes, told from inside a pane that exists only because the app
    // IS up. Two authors, one sentence; on 2026-09-10 the two of them contradicted each other on
    // screen. What this cover is entitled to describe is the document in front of it.
    expect(screen.getAllByText(/this page can\u2019t open/i).length).toBeGreaterThan(0)
    expect(screen.queryByText(/isn\u2019t running right now/i)).toBeNull()
    expect(screen.queryByText(/build complete/i)).toBeNull()
    // LIVENESS: and the citizen is not staring at a raw framework error screen — the cover is up.
    expect(screen.getByRole('status').textContent).not.toMatch(/preview is live/i)
  })

  it('★ ended with NOTHING SERVING collapses — a reclaimed or torn-down container, not a stop', () => {
    // ★ THE NAME AND ITS PARENTHETICAL WERE CORRECTED. It read
    // "ended WITHOUT completedLive still collapses (stop / force-end / failure tore the container
    // down)", and the parenthetical is FACTUALLY WRONG for a stop: `finish_turn_sandbox` pardons
    // the container with no branch on how the turn ended, and the stopped arm is reached precisely
    // because Stop arrives as a task cancellation into that `finally`. Nothing tears anything down.
    //
    // WHAT THE ASSERTION NOW REJECTS: a pane that keeps framing a URL when nothing is answering
    // there. That is the honest half of the old test and it is worth keeping — a frame pointed at a
    // dead container shows the citizen a painted corpse of their app. What it no longer does is
    // stand as evidence that collapsing on a STOP was intended; a stopped turn now resolves to a
    // serving address (see `turnNarrative`'s phase test) and never reaches this state at all.
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ended" />)
    expect(container.querySelector('iframe')).toBeNull()
    // ★ AND IT COLLAPSES TO NOTHING, not to a sentence. The card that used to fill this space was
    // the last of this file's four workspace verdicts; the map says it once and `AppPane` draws it.
    expect(container.textContent).not.toMatch(/no longer running/i)
    expect(container.querySelector('[role="status"]')?.textContent).toBe('')
  })

  it('RETIREMENT GUARD: nothing pre-empts the kept frame any more — the Restoring state that did is gone', () => {
    // This asserted the opposite: `relaunching` unmounted the pardoned frame in favour of a
    // "Restoring…" card. The flag had no producer — `relaunch()` was reachable only through
    // `LivePreview`'s own `onRelaunch`, which this component accepts and never reads — so the one
    // thing that could take an iframe down over a container the server is still serving could
    // never actually be set. Both are deleted; what is pinned now is the state that ships.
    const { container } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ended" serving relaunching />,
    )
    expect(container.querySelector('iframe')).toBeTruthy() // liveness: still framed, still serving
    expect(container.textContent).not.toMatch(/restoring/i)
    expect(container.textContent).not.toMatch(/no longer running/i)
  })

  // THE RETRACTION REGRESSION. The harness now renders the agent's `done_summary` instead of its
  // trailing prose; this pane's chip was the other half of that claim — the frame plus "Build
  // complete — your app is live below" — which the cover hid visually while leaving it in the
  // DOM, where a screen reader lives, so a pane covered by the retraction announced "your app
  // stopped running" and "Build complete" in the same breath. The retraction is content-agnostic
  // by design, so it survives the rendering change on its own, but only if the claim it retracts
  // actually goes quiet — a fact about THIS file that nothing tested.
  //
  // THE CHIP IS DELETED NOW, so the claim cannot be made from this pane at all and the
  // guard it needed is moot. The scenario survives as the ANNOUNCEMENT test it always really was:
  // the live region must carry the retraction, and must not be carrying a liveness claim instead.
  //
  // ASSERT-ABSENCE, PAIRED WITH LIVENESS. "No completion claim" is also true of a pane that threw
  // on render — so the retraction sentence has to be found on screen in the same breath, in both of
  // its nodes (the visible cover and the live region), or the absence proves nothing.
  it('a retracted claim leaves no live-preview announcement, and the retraction stays readable', () => {
    render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ended"
        serving
        previewState="alive"
        compileState="clean"
        workspaceLost
      />,
    )

    // ABSENCE: no completion claim, and no "your app preview is live" either — both would be
    // wrong over a workspace that has been wiped, and the second one is the announcement a
    // reader would otherwise hear on top of the retraction.
    expect(screen.queryByText(/build complete/i)).toBeNull()
    expect(screen.getByRole('status').textContent).not.toMatch(/preview is live/i)
    // LIVENESS: …because the retraction is standing in its place, in both nodes — the visible
    // cover and the pane's permanent live region.
    expect(screen.getAllByText(/isn’t your app any more/i)).toHaveLength(2)
    expect(screen.getAllByText(/we\u2019ll restore it/i).length).toBeGreaterThan(0)
  })

  it('a clean, un-retracted finished turn still frames the app and says so in the region', () => {
    const { container } = render(
      <LivePreview
        previewUrl={SANDBOX_URL}
        status="ended"
        serving
        previewState="alive"
        compileState="clean"
      />,
    )
    const iframe = container.querySelector('iframe')
    expect(iframe).toBeTruthy()
    // The reveal is earned by the framed document's own BEACON, never by the status and never
    // by a `load`. Mutation check: replace the vouch with a `load` and this goes red at opacity-0.
    vouch(container)
    expect(container.querySelector('[data-testid="device-card"]').className).toMatch(/opacity-100/)
    // ★ THE CHIP IS GONE AND THE SPOKEN CLAIM IS EARNED.
    //
    // The chip said "Build complete — your app is live below" and is DELETED: it was drawn over
    // the citizen's own app, and it rested on a cross-origin `load` that fires for a 500 exactly
    // as for a 200. The region's sentence rested on the same signal and was equally unevidenced.
    //
    // What is different about the region now is not the wording but the WARRANT: it speaks only
    // when a container is answering (`serving`) AND the platform asked the server whether the
    // build compiled and was told `clean`. Both hold here, so the sentence is true and this
    // test's own name — "says so in the region" — is finally what it asserts.
    expect(container.textContent).not.toMatch(/build complete/i)
    expect(screen.getByRole('status').textContent).toMatch(/preview is live/i)
  })
})

describe('LivePreview — relaunch a torn-down preview', () => {
  // INERTNESS GUARD. This used to press "Relaunch preview" on the terminal placeholder; that
  // control moved to `components/workspace/StartAppControl.tsx`, rendered by
  // `AppPane` from the one computed workspace state (exactly ONE control starts the app). The
  // copy this placeholder still owns is what LIVENESS checks below — the button is what INERTNESS
  // checks.
  // ★ AND THE PLACEHOLDER ITSELF IS GONE NOW, so the three tests that pinned its copy collapse
  // into one. They differed only in which terminal status and which `hasSavedBuild` value put the
  // card on screen, and all three now fail for the identical reason: there is no card. A second
  // and third copy of that finding would not prove anything the first does not.
  //
  // WHERE ITS NEWS LIVES, so nothing is merely dropped: "the preview is no longer running" with a
  // saved-build line under it is the map's "Your app is saved." plus the one press that brings it
  // back, drawn by `AppPane` on a board `AppPane.test.tsx` pins.
  it('★ RETIREMENT GUARD: no terminal status draws a placeholder, under any saved-build answer', () => {
    for (const status of ['ended', 'failed']) {
      for (const props of [{}, { hasSavedBuild: true }, { hasSavedBuild: false }, { hasSavedBuild: null }]) {
        const onRelaunch = vi.fn()
        const { container, unmount } = render(
          <LivePreview previewUrl={null} status={status} onRelaunch={onRelaunch} {...props} />,
        )
        const where = `${status} / ${JSON.stringify(props)}`
        for (const retired of [
          /no longer running/i,
          /start a new build/i,
          /your saved app is still there/i,
          /nothing to relaunch yet/i,
        ]) {
          expect(container.textContent, where).not.toMatch(retired)
        }
        // INERTNESS: no button under any retired label, and the dead prop is never called.
        expect(screen.queryByRole('button', { name: /relaunch|bring it back/i })).toBeNull()
        expect(onRelaunch).not.toHaveBeenCalled()
        // ★ LIVENESS, STRUCTURAL, because the expected screen is empty and an absence sweep over a
        // component that threw would pass every line above. The permanent live region is the one
        // element that exists in every state.
        expect(container.querySelector('[role="status"]')?.getAttribute('aria-live'), where).toBe('polite')
        expect(container.querySelector('[role="status"]')?.textContent, where).toBe('')
        unmount()
      }
    }
  })

  // THE "RESTORING…" WAIT AND ITS SLOW LABEL ARE GONE, and the three tests that drove them with
  // it. They rendered `relaunching`, a prop no production caller could set — it came from the
  // session hook's `relaunch()`, reachable only through `onRelaunch`, which is accepted here and
  // never read — so the wait could never appear, and its 20-second label covered nothing.
  //
  // THE FINDING SURVIVES: "the one wait that can legitimately run for minutes must label itself"
  // is enforced on the wait a citizen actually reaches — the frame's own load cap — in "a frame
  // that never loads still degrades to a LABELLED state" and "the capped state keeps the frame
  // MOUNTED" below. Same requirement, same sentence.
  it('RETIREMENT GUARD: no prop this pane accepts renders a Restoring wait any more', () => {
    vi.useFakeTimers()
    try {
      const { container } = render(
        <LivePreview previewUrl={null} status="ended" onRelaunch={vi.fn()} hasSavedBuild relaunching />,
      )
      // LIVENESS: the pane rendered — its permanent region is mounted — and what it drew instead
      // of a Restoring card is nothing at all, which is the terminal placeholder's retirement.
      expect(container.querySelector('[role="status"]')?.getAttribute('aria-live')).toBe('polite')
      expect(container.textContent).not.toMatch(/no longer running/i)
      expect(container.textContent).not.toMatch(/restoring your app/i)
      act(() => vi.advanceTimersByTime(20_000))
      expect(container.textContent).not.toMatch(/taking longer than usual/i)
      expect(container.textContent).not.toMatch(/your work is safe/i)
    } finally {
      vi.useRealTimers()
    }
  })

  it('remounts the frame when the shell asks it to reload', () => {
    // "What I see is out of date" is a judgement only the person looking can make — a dev-server
    // restart, an HMR socket that died quietly. The CONTROL is in the toolbar row now; what this
    // pins is the half this component owns, that a change in the signal produces a genuinely new
    // frame rather than a re-render of the same one.
    const view = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" reloadNonce={0} />)
    const before = view.container.querySelector('iframe')

    view.rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" reloadNonce={1} />)

    expect(view.container.querySelector('iframe')).not.toBe(before)
  })

  it('does NOT remount the frame when the reload signal holds still', () => {
    // The other half, and the one a single-direction test cannot see: a re-render for any other
    // reason must leave the citizen's app exactly where it was.
    const view = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" reloadNonce={3} />)
    const before = view.container.querySelector('iframe')

    view.rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" reloadNonce={3} turnRunning />)

    expect(view.container.querySelector('iframe')).toBe(before)
  })

  it('frames the restored preview once relaunch resolves (a fresh ready URL)', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" onRelaunch={vi.fn()} />)
    expect(container.querySelector('iframe')?.getAttribute('src')).toBe(SANDBOX_URL_2)
  })
})

// THIS WHOLE DESCRIBE BLOCK TESTED THE `showEmpty` ARM, and that arm is gone — the exact
// no-frame, nothing-built condition `AppPane` now owns outright. Two things moved with it, not
// just the button:
//
//   1. THE COPY. The empty-state placeholder and the tri-state wording it exercised
//      (`hasSavedBuild === null` claiming nothing) moved to `AppPane`'s `NoFrame`, driven by
//      `resolveWorkspaceState`'s `atRest()` in `workspaceState.ts` (see `workspaceState.test.ts`).
//   2. THE 404-SAID-AND-NOT-SWALLOWED DISCIPLINE. A failed start now surfaces through
//      `StartAppControl`'s own outcome handling (`StartAppControl.test.tsx`), not this
//      component's old `relaunchError` prop — no `AppPane`-driven pane populates it any more.
//
// `AppPane` also structurally forecloses this prop combination from reaching `LivePreview` in the
// product: it mounts this component only when the address resolver has a URL, and renders
// `NoFrame` otherwise — so a real citizen can no longer land on the state this block hand-built.
//
// ONE test replaces the five that were here: all five failed for the identical reason, and a
// second, third and fourth copy of the same finding would not prove anything the first did not.
describe('LivePreview — the no-previewUrl/no-status combination (formerly "relaunch from PROJECT state")', () => {
  it('stays inert across the whole former relaunch matrix — hasSavedBuild and relaunchError no longer reach any render here', () => {
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

describe('LivePreview — the relaunch response matrix', () => {
  // ★ ALL FOUR OF THESE PINNED A PLACEHOLDER THAT NO LONGER EXISTS, and they collapse into one
  // for the reason the block above them records: they failed for the identical reason, and a
  // second, third and fourth copy of one finding proves nothing the first does not.
  //
  // WHAT EACH OF THEM WAS. `relaunchError` was read in exactly three places, all special-casing
  // `kind === 'not_found'`; the `unavailable` and `failed` kinds fell through to a generic
  // saved-build sentence and their own `.message` was never read anywhere. `lastBuildFailed` used
  // to pick between two button labels. Every one of those reads landed on the terminal
  // placeholder — a workspace verdict this file has stopped authoring — so the props now have
  // nowhere to reach even if a caller sets them.
  //
  // WHERE THE LIVE 404 IS ANSWERED NOW: `StartAppControl`'s own outcome handling, which carries
  // the server's words verbatim onto the map's `note` (`StartAppControl.test.tsx`,
  // `workspaceState.test.ts`).
  it('★ RETIREMENT GUARD: every relaunch-response prop reaches no render at all', () => {
    const everyResponse = [
      { relaunchError: { kind: 'not_found', message: 'No saved build to relaunch. Build the app first.' }, hasSavedBuild: false },
      { relaunchError: { kind: 'unavailable', message: 'Sandbox unavailable. Please try again later or contact the admin' }, hasSavedBuild: true },
      { relaunchError: { kind: 'failed', message: 'Failed to relaunch the preview' }, hasSavedBuild: true },
      { lastBuildFailed: true, hasSavedBuild: true },
    ]

    for (const props of everyResponse) {
      const { container, unmount } = render(
        <LivePreview previewUrl={null} status="ended" onRelaunch={vi.fn()} {...props} />,
      )
      const where = JSON.stringify(props)
      // Neither the kind-specific copy nor the generic sentence it used to fall through to.
      for (const retired of [
        /nothing to relaunch yet/i,
        /your saved app is still there/i,
        /no longer running/i,
        /try again later/i,
        /failed to relaunch/i,
      ]) {
        expect(container.textContent, where).not.toMatch(retired)
      }
      expect(screen.queryByRole('alert')).toBeNull()
      expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
      // ★ LIVENESS, STRUCTURAL: the expected screen is empty, so an absence sweep alone would pass
      // over a component that threw. The permanent region is what proves it rendered.
      expect(container.querySelector('[role="status"]')?.getAttribute('aria-live'), where).toBe('polite')
      expect(container.querySelector('[role="status"]')?.textContent, where).toBe('')
      unmount()
    }
  })

  // ★ THE THIRD OVERLAY. This asserted the "Showing your last saved version — the most
  // recent build failed" notice appeared on a frame restored after a failed build. It shared the
  // exact rectangle the other two did, and it is the one a citizen can least afford to have half
  // covered — or to have covering their app's own nav.
  //
  // ITS PROP IS DELETED, NOT JUST ITS MARKUP, and that is stated rather than left to be inferred:
  // both publishers hardcoded `restoredFromFailedBuild: false`, so nothing could ever produce this
  // notice. Finding it a NEW home (the toolbar row, or a transcript line) stays open knowingly
  // — which is why there is no replacement assertion here to write.
  it('★ says nothing over the app about a restore — the notice has no renderer on this pane', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" />)
    expect(container.textContent).not.toMatch(/last saved version/i)
    // LIVENESS: the frame is up, so the silence is this pane's choice and not a failed render.
    expect(container.querySelector('iframe')).toBeTruthy()
  })
})

describe('LivePreview — dev-server crash: reconnecting is distinct from building', () => {
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

// --- the reveal is gated on the framed document VOUCHING FOR ITSELF ------------------------
//
// What this replaces, twice over. First `FRAME_GRACE_MS = 400` revealed the iframe on a TIMER,
// which can only prove that time passed — so the citizen got an UNLABELLED BLANK WHITE CARD for
// the 5-7s the sandbox spent compiling its first Turbopack route. Then `load` replaced the timer,
// and on 2026-09-10 that failed in the same shape for a harder reason: every signal this pane is
// handed is measured INSIDE the container, while the citizen's browser reaches the app through
// the portal edge and the ACA ingress. A 502 with a ZERO-BYTE BODY lives in that gap, and it
// fires `load` exactly as a page does. The only witness on the citizen's side of the network is
// the document itself, which is what the beacon is.
//
// The device card is queried by data-testid rather than `iframe.parentElement` for the reason
// the device-toggle block gives below: an element inserted between the card and the iframe
// later must not silently retarget these assertions at the wrong node.

// The numbers the vouch machinery runs on, mirrored from the component. Kept as literals on
// purpose: a test that imports the constant it is pinning asserts only that the code equals
// itself.
const FRAME_LOAD_CAP_MS = 20000 // a frame whose `load` never fires is ASKED after this
const VOUCH_AFTER_LOAD_MS = 5000 // …and one that loaded and then said nothing, after this
const PINGS_BEFORE_RELOAD = 2 // asking comes first: this many pings per key before it is fetched
const VOUCH_RETRY_LIMIT = 3 // re-requests per address, after which the wait is LABELLED
const HEARTBEAT_MS = 15000 // …and a frame that HAS vouched is asked again, slowly, for ever

// ★ AND THERE IS NO PING-REPLY TIMER, which is pinned here as an absence rather than left as a
// deletion nobody notices. A second `load` at one key takes the vouch back ON THE SPOT (see “a
// vouched frame that loads a SECOND time” below), so no constant sits between a document being
// replaced and this pane admitting it.

/**
 * Spend the whole re-request budget on a document that never vouches, landing on the stall card.
 *
 * TWELVE expiries, and the arithmetic IS the contract: every key is asked `PINGS_BEFORE_RELOAD`
 * times before it is fetched again, so a key costs three expiries — and there are four keys, the
 * first three ending in a re-request and the last, its budget gone, in the label. One `act` per
 * expiry rather than one long advance, because a React state update made from a timer callback is
 * not flushed until the `act` returns — so a single long `advanceTimersByTime` would fire ONE
 * timer and then find nothing else scheduled.
 */
function exhaustTheVouchBudget() {
  const steps = (VOUCH_RETRY_LIMIT + 1) * (PINGS_BEFORE_RELOAD + 1)
  for (let i = 0; i < steps; i += 1) {
    act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS + 1))
  }
}

describe('LivePreview — the stop-clock instant: `onRevealed`', () => {
  it('\u2605 fires when the citizen is actually LOOKING at the app, and not a moment before', () => {
    // The mark has to mean "the app is on screen". A `load` alone does not: it fires for a 500,
    // it fires for the bodyless 502 measured on 2026-09-10, and it fires under a raised cover.
    // Only `revealed` — the framed document VOUCHED for itself AND the cover is down — is the
    // honest instant, which is exactly why the effect hangs off that value and nothing else.
    const onRevealed = vi.fn()
    const { container } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" onRevealed={onRevealed} />,
    )

    expect(onRevealed).not.toHaveBeenCalled()

    // ★ AND THE LOAD IS NOT THE INSTANT EITHER. This clock is "how long until the citizen saw
    // their app", and a blank white rectangle was stopping it — so the runs where they saw
    // nothing at all were logged as the fastest views of the day. Mutation check: put
    // `frameLoaded` back into `revealed` and the assertion under this comment goes red.
    loadTheFrame(container)
    expect(onRevealed).not.toHaveBeenCalled()

    vouch(container)

    expect(card(container).className).toMatch(/opacity-100/)
    expect(card(container).getAttribute('data-revealed')).toBe('true')
    expect(onRevealed).toHaveBeenCalledTimes(1)
  })

  it('\u2605 does NOT fire while the cover is up over a broken app', () => {
    // A failed compile keeps the cover down over an error screen. The document loaded AND
    // vouched for itself — the beacon says a root layout rendered, never that the app is healthy
    // — and the citizen is still looking at a cover, not at their app. Mutation check: hang the
    // effect off `frameVouched` instead of `revealed` and this goes red.
    const onRevealed = vi.fn()
    const { container, rerender } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="failed" onRevealed={onRevealed} />,
    )
    loadTheFrame(container)
    vouch(container)

    expect(card(container).className).toMatch(/opacity-0/)
    expect(onRevealed).not.toHaveBeenCalled()

    // \u2026and it fires the moment the app actually comes up clean.
    rerender(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="clean" onRevealed={onRevealed} />,
    )
    expect(onRevealed).toHaveBeenCalledTimes(1)
  })

  it('\u2605 fires ONCE for one document, even when the reveal is retracted and re-earned', () => {
    // The reveal is not monotonic: a verdict that flips to failed RETRACTS it, and a later
    // clean verdict earns it back on the SAME document. That is one first-view, not two \u2014 and it
    // is the only path that re-enters this effect with the same frame key, so it is the one that
    // pins the guard. Mutation check: drop the per-frame-key guard and this goes red.
    const onRevealed = vi.fn()
    const { container, rerender } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="clean" onRevealed={onRevealed} />,
    )
    vouch(container)
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
    // UNDERNEATH a cover that says what is in the frame is not the citizen's app. Firing here
    // reports a first view of an app the citizen cannot see \u2014 and reports it as FAST, since the
    // frame loaded fine.
    //
    // Mutation check: drop `workspaceLost` from the effect's guard and this goes red.
    const onRevealed = vi.fn()
    const { container } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" workspaceLost onRevealed={onRevealed} />,
    )
    loadTheFrame(container)
    vouch(container)

    // The cover really is up \u2014 and it names the FRAME, not the workspace. That sentence used to
    // open "Your app stopped running", a verdict on the workspace that this component cannot reach
    // and that contradicts its own mounting condition.
    expect(container.textContent).toMatch(/isn\u2019t your app any more/i)
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

    expect(() => vouch(container)).not.toThrow()
    expect(card(container).className).toMatch(/opacity-100/) // and the app is still shown
  })

  it('is optional \u2014 a caller that does not measure anything still reveals normally', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    vouch(container)
    expect(card(container).className).toMatch(/opacity-100/)
  })
})

describe('LivePreview — the frame is revealed on the beacon, never on load or a timer', () => {
  it('keeps the labelled wait up through the load, and swaps it for the frame on the BEACON', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)

    // Mounted at once — a frame that never mounts can never send a beacon — but NOT revealed…
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(card(container).className).toMatch(/opacity-0/)
    expect(card(container).getAttribute('data-revealed')).toBe('false')
    // …and the wait is LABELLED. This is the whole requirement.
    expect(container.textContent).toMatch(/opening your app/i)

    // ★ AND THE LOAD CHANGES NEITHER OF THEM, which is the 2026-09-10 correction. The public
    // address answered `502` with a ZERO-BYTE BODY and `load` fired for it exactly as for a page;
    // this pane cannot read a cross-origin status, so it faded in the empty document and called it
    // the citizen's app. Mutation check: put `frameLoaded` back into `revealed` and the two
    // assertions under this comment go red — that pane is the defect, reproduced.
    loadTheFrame(container)
    expect(card(container).className).toMatch(/opacity-0/)
    expect(card(container).getAttribute('data-revealed')).toBe('false')
    expect(container.textContent).toMatch(/opening your app/i)

    // The document itself, and only the document, ends the wait.
    vouch(container)

    expect(card(container).className).toMatch(/opacity-100/)
    expect(card(container).getAttribute('data-revealed')).toBe('true')
    expect(container.textContent).not.toMatch(/opening your app/i)
  })

  it('time passing NEVER reveals the frame — and the ping it triggers is not a reveal either', () => {
    // Mutation check: put the retired 400ms grace back, or let the vouch wait's expiry reveal
    // instead of ask, and this goes red. A timer can only prove that time passed.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      const post = vi.spyOn(container.querySelector('iframe').contentWindow, 'postMessage')
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS - 1))
      expect(card(container).className).toMatch(/opacity-0/) // nothing vouched, nothing revealed
      expect(container.textContent).toMatch(/opening your app/i)

      act(() => vi.advanceTimersByTime(2)) // the cap lands: the document is ASKED, never shown
      expect(card(container).className).toMatch(/opacity-0/)
      expect(card(container).getAttribute('data-revealed')).toBe('false')
      // LIVENESS: the pane really did act at the cap — it asked the document to vouch — so the two
      // absences above are a withheld reveal rather than a component that stopped moving. And
      // asking comes before re-requesting, so the element itself is untouched.
      expect(post).toHaveBeenCalledWith(PING, SANDBOX_ORIGIN)
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#0.0`,
      )
    } finally {
      vi.useRealTimers()
    }
  })

  // INERTNESS GUARD. The stall card still degrades to a LABELLED state (never a bare white
  // card — that half of the unit is untouched); what it no longer does is offer its own
  // Relaunch button, because the rule is exactly one control starts the app and this is not it.
  it('INERTNESS GUARD: a frame that never vouches still degrades to a LABELLED state — never a bare white card, and never a button', () => {
    vi.useFakeTimers()
    try {
      const onRelaunch = vi.fn()
      const { container } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ready" onRelaunch={onRelaunch} hasSavedBuild />,
      )
      exhaustTheVouchBudget()
      // LIVENESS: nothing has vouched, so nothing is revealed, and the wait is still labelled.
      expect(card(container).className).toMatch(/opacity-0/)
      expect(container.textContent).toMatch(/taking longer than usual/i)
      // INERTNESS: no button, under any label, and the relaunch prop is never called.
      expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
      expect(onRelaunch).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('the stalled state keeps the frame MOUNTED, so a late beacon still reveals', () => {
    // Unmounting the iframe at the stall would make it permanent BY CONSTRUCTION: the beacon it is
    // waiting for could never arrive. The stall changes the copy, not the frame.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      exhaustTheVouchBudget()
      expect(container.textContent).toMatch(/taking longer than usual/i)
      expect(container.querySelector('iframe')).toBeTruthy()

      vouch(container)

      expect(card(container).className).toMatch(/opacity-100/)
      expect(container.textContent).not.toMatch(/taking longer than usual/i)
    } finally {
      vi.useRealTimers()
    }
  })

  it('the stalled state inherits the relaunch discipline: no relaunch offered, and none PROMISED, without a confirmed build', () => {
    // The same trap the terminal placeholder fell into: copy that says "relaunch it" is a claim
    // about a saved build, so it is gated exactly like the button.
    vi.useFakeTimers()
    try {
      for (const hasSavedBuild of [false, null]) {
        const { container, unmount } = render(
          <LivePreview previewUrl={SANDBOX_URL} status="ready" onRelaunch={vi.fn()} hasSavedBuild={hasSavedBuild} />,
        )
        exhaustTheVouchBudget()
        expect(container.textContent).toMatch(/taking longer than usual/i) // still labelled…
        expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull() // …but claims nothing
        expect(container.textContent).not.toMatch(/relaunch the preview/i)
        unmount()
      }
    } finally {
      vi.useRealTimers()
    }
  })

  it('a NEW previewUrl re-gates the reveal on the new frame’s own BEACON (relaunch mid-session)', () => {
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    vouch(container)
    expect(card(container).className).toMatch(/opacity-100/)

    rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" />)
    // The fresh frame must not inherit the previous one's verdict — that is the blank card again.
    // Mutation check: key the vouch on the URL instead of on the FRAME KEY and this goes red.
    expect(card(container).className).toMatch(/opacity-0/)
    expect(card(container).getAttribute('data-revealed')).toBe('false')
    expect(container.textContent).toMatch(/opening your app/i)

    // …and the second app has to speak as itself: the trust gate is recomputed from the url that
    // is framed now, so the first app's origin no longer authorises anything here.
    vouch(container, SANDBOX_ORIGIN_2)
    expect(card(container).className).toMatch(/opacity-100/)
  })

  it('★ a RETURNING frame key starts unrevealed — app A → app B → app A never inherits A’s vouch', () => {
    // The key is the address plus two counters, so it can RECUR: this pane deliberately carries no
    // `key` of its own (see `AppPaneHost`), so it survives a navigation from app A to app B and
    // back — and A's key comes back BYTE-IDENTICAL while A's iframe is a brand-new element that
    // has fetched nothing. A verdict remembered from the first visit would reveal that empty
    // element on its very first paint: the blank white rectangle again, by the one route a
    // per-key verdict looks like it has already closed.
    //
    // Mutation check: key the verdicts on `previewUrl` instead of on the FRAME KEY and this goes
    // red at full opacity on the return.
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    const firstVisit = container.querySelector('iframe')
    const returningKey = firstVisit.getAttribute('data-frame-key')
    vouch(container)
    expect(card(container).getAttribute('data-revealed')).toBe('true')

    rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" />)
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)

    // The key really did come back byte-identical — without that this test proves nothing — on an
    // element that is NOT the one that vouched. Neither nonce moved: nothing about this return is
    // a reload.
    const secondVisit = container.querySelector('iframe')
    expect(secondVisit.getAttribute('data-frame-key')).toBe(returningKey)
    expect(secondVisit).not.toBe(firstVisit)

    expect(card(container).getAttribute('data-revealed')).toBe('false')
    expect(card(container).className).toMatch(/opacity-0/)
    expect(container.textContent).toMatch(/opening your app/i)

    // LIVENESS: this visit's own beacon reveals it, so the pane is re-gated rather than stuck.
    vouch(container)
    expect(card(container).getAttribute('data-revealed')).toBe('true')
  })

  it('a relaunch AFTER the stall returns to the honest wait — the stalled verdict does not outlive its frame', () => {
    // Caught in a real browser, not here: with the stall held as a bare boolean instead of
    // per-key, the fresh frame opened straight into "taking longer than usual" — a complaint
    // about a frame that no longer exists.
    vi.useFakeTimers()
    try {
      const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      exhaustTheVouchBudget()
      expect(container.textContent).toMatch(/taking longer than usual/i)

      rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" />)
      expect(container.textContent).not.toMatch(/taking longer than usual/i)
      expect(container.textContent).toMatch(/opening your app/i)

      // …and the new frame gets its own full budget, not the remains of the old one: one cap is a
      // re-request here, where on the exhausted frame above it was the label.
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS + 1))
      expect(container.textContent).toMatch(/opening your app/i)
      exhaustTheVouchBudget()
      expect(container.textContent).toMatch(/taking longer than usual/i)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a re-frame of the SAME url after a reconnect re-gates too (the frame was torn down and rebuilt)', () => {
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    vouch(container)
    expect(card(container).className).toMatch(/opacity-100/)

    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" reconnecting />) // dev process died
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" reconnecting={false} />) // fresh preview_ready
    // Mutation check: drop the `!showFrame` reset of the three keys and this goes red — the new
    // element opens at full opacity on the strength of the dead document's word.
    expect(card(container).className).toMatch(/opacity-0/)
    expect(card(container).getAttribute('data-revealed')).toBe('false')
    expect(container.textContent).toMatch(/opening your app/i)
  })

  it('★ NEVER reveals on the load of an ERROR response — a bodyless 502 loads exactly like a page', () => {
    // ★ THIS TEST USED TO ASSERT THE OPPOSITE, and its premise is what failed in front of the
    // owner. It read "reveals on the load of an ERROR response — a broken app must look broken,
    // not pending forever", on the reasoning that revealing claims "a document arrived" and never
    // "the app is healthy". Measured on 2026-09-10: what arrived at the public address was `502`
    // with a ZERO-BYTE body, so "looking broken" was a COMPLETELY BLANK WHITE RECTANGLE presented
    // as the citizen's app, with no words anywhere on it. A cross-origin status code is unreadable
    // from here, so the only witness on the citizen's side of the network is the document itself.
    //
    // Mutation check: restore `revealed = frameLoaded && !covered` and the first two assertions go
    // red — which is the same thing as saying this test now pins the fix rather than the defect.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      loadTheFrame(container) // the 502 arrives and fires `load`, exactly as a real page would

      expect(card(container).className).toMatch(/opacity-0/)
      expect(card(container).getAttribute('data-revealed')).toBe('false')
      // …and it stays hidden through every re-request, landing on WORDS rather than on the blank.
      exhaustTheVouchBudget()
      expect(card(container).className).toMatch(/opacity-0/)
      expect(container.textContent).toMatch(/Your app is taking longer than usual to open/i)

      // LIVENESS: the ingress catches up and the recovered document vouches — the reveal is
      // withheld from a 502, not broken.
      vouch(container)
      expect(card(container).className).toMatch(/opacity-100/)
    } finally {
      vi.useRealTimers()
    }
  })

  it('no state shows an unlabelled blank pane: while the frame is hidden, the pane always says why', () => {
    // The unit's verification line, made executable. Anything the citizen can be looking at
    // before the document vouches must carry one of these labels.
    //
    // ★ "Starting your app…" IS NOW "Opening your app…", AND THE WORD IS THE WHOLE POINT. This
    // pane is mounted only once the platform has watched the app ANSWER a request, so by the time
    // this sentence is on screen the starting is over and the only thing still pending is this
    // frame's own document — which is the one fact this component is entitled to describe.
    // "Starting your app…" also belongs to somebody else now: it is `StartAppControl`'s pending
    // label and `ReclaimWorkspaceDialog`'s step, both of them about a press, and one sentence with
    // two authors is what this whole change exists to stop.
    const LABELLED = /setting up your sandbox|building your app|opening your app|taking longer than usual/i
    vi.useFakeTimers()
    try {
      // Every pre-reveal state, in the order a citizen actually meets them.
      const waits = [
        () => render(<LivePreview previewUrl={null} status="provisioning" />),
        () => render(<LivePreview previewUrl={null} status="building" />),
        () => render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />),
        // ★ THE STATE THE INCIDENT WAS ACTUALLY IN, and it was missing from this sweep: a frame
        // that LOADED and then said nothing. Under the old contract this state did not exist —
        // the load ended every wait on screen — which is exactly why a blank rectangle could be
        // unlabelled without any test in this file noticing.
        () => {
          const view = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
          loadTheFrame(view.container)
          return view
        },
        () => {
          const view = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
          exhaustTheVouchBudget()
          return view
        },
      ]
      for (const mount of waits) {
        const { container, unmount } = mount()
        expect(container.querySelector('[data-testid="device-card"].opacity-100')).toBeNull()
        // ★ THE LABEL HAS TO BE ON SCREEN, NOT MERELY SPOKEN, and this test read
        // `container.textContent` — which INCLUDES the permanent sr-only live region. That region
        // announces in every state, so the assertion was satisfied by the announcement alone and
        // stayed green with the visible card deleted. Caught by mutation: removing the frame-stall
        // card's render left this passing while a citizen watched a blank rectangle. Asserting on
        // the visible half is the fix, and it is the same lesson the `starting` refusal already
        // records one file over — an absence paired with a liveness check that cannot fail is an
        // absence on its own.
        const spoken = container.querySelector('[role="status"]')
        const seen = [...container.querySelectorAll('p')].filter(
          (el) => !spoken.contains(el) && LABELLED.test(el.textContent ?? ''),
        )
        expect(seen.length).toBeGreaterThan(0)
        unmount()
      }
    } finally {
      vi.useRealTimers()
    }
  })
})

// --- the beacon's provenance, the ping that asks for it, and the retraction ----------------
//
// The wire is two messages, matched by value on both sides (`sandbox/template/
// instrumentation-client.ts`): the framed app posts `bial:app-mounted` to its parent once its root
// layout has rendered, and answers a `bial:ping` from the parent with the same message. This block
// is the parent's half of that contract — who is allowed to speak, what is done with what they
// say, and what happens when a document that had spoken goes quiet.

describe('LivePreview — the beacon: who may vouch, what the ping asks, and when a vouch is taken back', () => {
  it('a beacon from a WRONG ORIGIN does not reveal, nor does one from another window — and the pane’s own frame still does', () => {
    // ★ ORIGIN ALONE STOPPED DISCRIMINATING when BIAL refused a wildcard certificate: every
    // generated app is served from ONE name now, so `e.origin` proves "this is an app" and never
    // "this is the app I am framing". The reachable impostor is not a stray tab (the portal opens
    // every app link with rel="noopener") — it is another frame inside this same portal document,
    // which the second iframe below stands in for: same origin, different window.
    //
    // ASSERT-ABSENCE, PAIRED WITH LIVENESS. jsdom SWALLOWS a throw inside a window listener, so a
    // bare "still hidden" is equally green over a gate that has died or that rejects everything.
    // The genuine beacon at the end is what makes each rejection mean "rejected THAT sender".
    //
    // Mutation check: drop the `e.origin` half and the first beacon reveals; drop the `e.source`
    // half and the second one does.
    const { container, iframe } = setup()
    const impostor = document.body.appendChild(document.createElement('iframe'))
    try {
      act(() => {
        window.dispatchEvent(
          new MessageEvent('message', {
            data: MOUNTED_BEACON,
            origin: 'https://evil.example',
            source: iframe.contentWindow,
          }),
        )
      })
      expect(card(container).getAttribute('data-revealed')).toBe('false')

      act(() => {
        window.dispatchEvent(fromSandbox(MOUNTED_BEACON, impostor.contentWindow))
      })
      expect(card(container).getAttribute('data-revealed')).toBe('false')
      expect(card(container).className).toMatch(/opacity-0/)

      // LIVENESS: the very same message, from the window this pane actually rendered, reveals.
      vouch(container)
      expect(card(container).getAttribute('data-revealed')).toBe('true')
      expect(card(container).className).toMatch(/opacity-100/)
    } finally {
      impostor.remove()
    }
  })

  it('the beacon is CONSUMED, not forwarded — and a non-beacon from the same frame still reaches the receiver', () => {
    // It is this pane's own evidence, not a report about the app: forwarded, `bial:app-mounted`
    // would arrive in the client-error relay, where the harness would have to learn to ignore a
    // message the pane invented the meaning of.
    //
    // Mutation check: delete the `return` after `setVouchedKey(...)` and the first assertion goes
    // red. The second is the liveness half — the seam is still open for everything else.
    const onFrameMessage = vi.fn()
    const { container, iframe } = setup({ onFrameMessage })

    vouch(container)
    expect(onFrameMessage).not.toHaveBeenCalled()
    // …and it was READ rather than merely dropped, which is what tells a consumed message from a
    // rejected one: it revealed the frame.
    expect(card(container).getAttribute('data-revealed')).toBe('true')

    window.dispatchEvent(fromSandbox({ kind: 'client_error' }, iframe.contentWindow))
    expect(onFrameMessage).toHaveBeenCalledWith({ kind: 'client_error' })
    expect(onFrameMessage).toHaveBeenCalledTimes(1)
  })

  it('★ a beacon with NO path is not a beacon: it reveals nothing, and is forwarded like any other message', () => {
    // ★ THE FIELD THE REVISED WIRE ADDED, AND WHY IT IS REQUIRED RATHER THAN OPTIONAL. One
    // hostname serves the whole fleet, so `e.source` says “the window I framed” and the path is
    // the only thing that says WHICH app painted in it. A message that omits the field proves
    // nothing about identity, so it is not evidence at all — and, having failed the beacon test,
    // it is an ordinary frame message and goes to the receiver seam rather than being swallowed.
    //
    // Mutation check: drop the `typeof data.path === 'string'` half of `isMountedBeaconFor` and
    // the first two assertions go red — a message naming no app would reveal one, and would be
    // eaten on the way past.
    const onFrameMessage = vi.fn()
    const { container, iframe } = setup({ onFrameMessage })
    const pathless = { type: 'bial:app-mounted' }

    act(() => {
      window.dispatchEvent(fromSandbox(pathless, iframe.contentWindow))
    })
    expect(card(container).getAttribute('data-revealed')).toBe('false')
    expect(onFrameMessage).toHaveBeenCalledWith(pathless)

    // LIVENESS: the same message WITH its path is a beacon — it reveals, and it is consumed rather
    // than forwarded — so the two lines above are a rejected shape, never a dead listener.
    vouch(container)
    expect(card(container).getAttribute('data-revealed')).toBe('true')
    expect(onFrameMessage).toHaveBeenCalledTimes(1)
  })

  it('★ a beacon for ANOTHER app on the same host reveals nothing here — and is forwarded, not eaten', () => {
    // The address shape the path field exists for: one name, one certificate, one browser origin,
    // and the apps told apart by PATH. A frame that navigated itself to a sibling app reports that
    // app's path, and this pane must not present a stranger's app as the one it was asked to frame.
    //
    // Mutation check: delete the `reported === framedPath || reported.startsWith(...)` comparison
    // (accept any string) and the first two assertions go red.
    const onFrameMessage = vi.fn()
    const { container } = render(
      <LivePreview previewUrl={APPS_URL_A} status="ready" onFrameMessage={onFrameMessage} />,
    )
    const strayer = { type: 'bial:app-mounted', path: APPS_PATH_B }

    act(() => {
      window.dispatchEvent(
        fromSandbox(strayer, container.querySelector('iframe').contentWindow, APPS_ORIGIN),
      )
    })
    expect(card(container).getAttribute('data-revealed')).toBe('false')
    expect(onFrameMessage).toHaveBeenCalledWith(strayer)

    // LIVENESS: a path UNDER the framed app's own — a route inside it, not merely its root — IS
    // this app vouching, so it reveals and is consumed.
    vouch(container, APPS_ORIGIN, '/a/sbx-aaaa/dashboard')
    expect(card(container).getAttribute('data-revealed')).toBe('true')
    expect(onFrameMessage).toHaveBeenCalledTimes(1)
  })

  it('a load ASKS the document to vouch — a ping into the frame, at the preview origin and never at "*"', () => {
    // The pane cannot tell a first load from a reload the document performed on itself, and a
    // document that has already mounted will not send its spontaneous beacon a second time. So the
    // load asks. The targetOrigin is explicit for the usual reason: a `'*'` ping hands `bial:ping`
    // to whatever document is in the frame — including the apps router's own error page, and
    // including whatever a mid-navigation frame has become.
    //
    // Mutation check: delete the `postMessage` and the whole re-vouch path dies silently (a
    // dev-server restart would never recover its reveal); widen the targetOrigin to `'*'` and the
    // last assertion goes red.
    const { iframe } = setup()
    const post = vi.spyOn(iframe.contentWindow, 'postMessage')

    fireEvent.load(iframe)

    expect(post).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledWith(PING, SANDBOX_ORIGIN)
    expect(post.mock.calls[0][1]).not.toBe('*')
  })

  it('★ a vouched frame that loads a SECOND time is retracted AT ONCE — no timer stands in between', () => {
    // A dev server restart reloads the framed page, and this side sees only a second `load` on a
    // frame that had already vouched. The document behind it may now be anything — the router's
    // error page while the server comes back, most likely, or the bodyless 502 measured on
    // 2026-09-10 — so the vouch is owed again, and it is owed IMMEDIATELY: a grace period is a
    // window in which the pane knowingly keeps vouching for a document it knows has been replaced.
    //
    // Mutation check: reinstate a ping-reply timer (the retired PING_REPLY_MS) and the two
    // assertions under the second load go red with no timer advanced; delete the `if (reloaded)`
    // branch and they go red for good.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      vouch(container)
      expect(card(container).className).toMatch(/opacity-100/)

      loadTheFrame(container) // the FIRST load: a beacon can beat its own load, so nothing is owed
      expect(card(container).getAttribute('data-revealed')).toBe('true')

      loadTheFrame(container) // …and the SECOND is a different document

      // NOT ONE MILLISECOND ADVANCED between that load and these two lines. That is the assertion.
      expect(card(container).className).toMatch(/opacity-0/)
      expect(card(container).getAttribute('data-revealed')).toBe('false')
      // LIVENESS: the frame is still mounted and the pane is back in its labelled wait, so the
      // retraction returns the citizen to the wait rather than to a blank pane.
      expect(container.querySelector('iframe')).toBeTruthy()
      expect(container.textContent).toMatch(/opening your app/i)

      // …and the replacement document earns the reveal back by answering the ping that load sent.
      vouch(container)
      expect(card(container).getAttribute('data-revealed')).toBe('true')
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ …while the FIRST load at a key never takes a vouch back, however long it is then left alone', () => {
    // The other half, and the reason the retraction is keyed on "a SECOND load" rather than on "a
    // load": a page's beacon routinely beats its own `load` (hydration finishes while an image or
    // a font is still arriving), so the ordinary happy path IS vouch-then-load. Retracting there
    // would flash the citizen's app away at the moment it arrived.
    //
    // Mutation check: retract on every load — drop the `reloaded` read of `loadedKeyRef` — and the
    // two assertions after the single load go red.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      vouch(container)
      loadTheFrame(container)

      expect(card(container).className).toMatch(/opacity-100/)
      expect(card(container).getAttribute('data-revealed')).toBe('true')

      // …and no wait is running underneath it either: a vouched frame is left alone, never asked
      // again and never fetched again.
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS * 4))
      expect(card(container).getAttribute('data-revealed')).toBe('true')
      // LIVENESS: the app is on screen with no wait over it, which is what a kept vouch looks like.
      expect(container.textContent).not.toMatch(/opening your app/i)
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#0.0`,
      )
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('LivePreview — the vouch wait: a silent document is asked again, and then labelled', () => {
  it('a frame that LOADED and then said nothing is ASKED after the short wait, and fetched again only after two', () => {
    // The 502 gap, which closes on its own once the ingress catches up — so the honest answer is
    // to ask again rather than to show the blank or to give up. The short wait is generous against
    // hydration: a page that is going to send a beacon at all sends it within milliseconds of
    // `load`. And ASKING COMES BEFORE RE-REQUESTING, because a fetch tears down the very hydration
    // that produces the beacon.
    //
    // Mutation check: swap the two waits (`frameLoaded ? FRAME_LOAD_CAP_MS : VOUCH_AFTER_LOAD_MS`)
    // and every step of this goes red — nothing happens at 5s at all; drop `PINGS_BEFORE_RELOAD`
    // and the first expiry replaces the element instead of asking it.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      const first = container.querySelector('iframe')
      loadTheFrame(container)
      // Spied AFTER the load, so the load's own ping is out of the count and every call below
      // belongs to the wait.
      const post = vi.spyOn(first.contentWindow, 'postMessage')

      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS - 1))
      expect(post).not.toHaveBeenCalled()
      expect(container.querySelector('iframe')).toBe(first)

      act(() => vi.advanceTimersByTime(2))
      // ASKED, not replaced: the same element still, and a `bial:ping` into it at the preview
      // origin. LIVENESS for the untouched-element assertion beside it.
      expect(post).toHaveBeenCalledWith(PING, SANDBOX_ORIGIN)
      expect(container.querySelector('iframe')).toBe(first)
      expect(first.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#0.0`)

      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1)) // the second ask…
      expect(post).toHaveBeenCalledTimes(PINGS_BEFORE_RELOAD)
      expect(container.querySelector('iframe')).toBe(first)

      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1)) // …and only now the re-request
      // A NEW element is the re-request — the browser reacts to the element, not to the attribute
      // this is read through — and the key names why it happened.
      expect(container.querySelector('iframe')).not.toBe(first)
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#1.0`,
      )
      // LIVENESS: still the same address, so this is a re-request rather than a navigation.
      expect(container.querySelector('iframe').getAttribute('src')).toBe(SANDBOX_URL)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a frame whose `load` never fires waits the FULL cap — a hung connection is not a silent document', () => {
    // The other wait, and it is four times longer for a reason: nothing has arrived at all, so
    // there is nothing to have been silent. Mutation check: collapse the two waits into one
    // constant and one of these two tests goes red whichever constant you pick.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      const post = vi.spyOn(container.querySelector('iframe').contentWindow, 'postMessage')

      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1)) // the SHORT wait must not apply
      expect(post).not.toHaveBeenCalled()

      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS - VOUCH_AFTER_LOAD_MS))
      // LIVENESS: the long wait DID land — the document is asked — so the silence above is the
      // right wait running rather than no wait at all. The element is untouched: asking first.
      expect(post).toHaveBeenCalledWith(PING, SANDBOX_ORIGIN)
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#0.0`,
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ two pings, then a re-request — three times over — and then the labelled stall, where it STOPS', () => {
    // THE WHOLE SEQUENCE, walked one expiry at a time, because each step is a separate decision:
    // ASKING COMES BEFORE RE-REQUESTING (a fetch tears down the hydration that produces the
    // beacon), and the budget exists so a container running an image older than the beacon lands
    // on a labelled card instead of reloading its own app forever — a page that never settles and
    // a dev server asked to serve it on a loop.
    //
    // Mutation check: drop the `pingsRef.current < PINGS_BEFORE_RELOAD` branch and every ping step
    // goes red on the frame key; delete the `vouchRetriesRef.current < VOUCH_RETRY_LIMIT` guard
    // and the key climbs past `#3.0` with no card ever appearing; delete the trailing
    // `setStalledKey(...)` and the card never appears at all.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)

      for (let attempt = 0; attempt <= VOUCH_RETRY_LIMIT; attempt += 1) {
        const iframe = container.querySelector('iframe')
        expect(iframe.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#${attempt}.0`)
        const post = vi.spyOn(iframe.contentWindow, 'postMessage')
        // The document arrives — a bodyless 502 fires `load` exactly as a page does — and the load
        // asks it once. Every wait below is the short one from here on.
        loadTheFrame(container)

        for (let ping = 0; ping < PINGS_BEFORE_RELOAD; ping += 1) {
          act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
          // A PING STEP TOUCHES NOTHING ELSE: same element, same key, and the citizen is still
          // being told the honest wait rather than the stall.
          expect(container.querySelector('iframe')).toBe(iframe)
          expect(iframe.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#${attempt}.0`)
          expect(container.textContent).toMatch(/opening your app/i)
        }
        // The load's own ping plus one per wait — every one of them `bial:ping`, every one of them
        // at the preview origin and never at `'*'`.
        expect(post).toHaveBeenCalledTimes(PINGS_BEFORE_RELOAD + 1)
        for (const [message, targetOrigin] of post.mock.calls) {
          expect(message).toEqual(PING)
          expect(targetOrigin).toBe(SANDBOX_ORIGIN)
        }

        // …and only now the re-request — except on the last pass, where the budget is gone and
        // this same expiry labels the wait instead.
        act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
      }

      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#${VOUCH_RETRY_LIMIT}.0`,
      )
      expect(container.textContent).toMatch(/Your app is taking longer than usual to open/i)
      expect(container.textContent).toMatch(/It will appear here the moment it loads/i)

      // …and nothing moves after that, however long the citizen leaves the tab open.
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS * 10))
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#${VOUCH_RETRY_LIMIT}.0`,
      )

      // LIVENESS, and it is the property the card's own copy promises: a beacon that lands after
      // the re-requests ran out still wins. The stall says "slow", never "dead".
      vouch(container)
      expect(card(container).className).toMatch(/opacity-100/)
      expect(container.textContent).not.toMatch(/taking longer than usual/i)
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ the budget is spent per ADDRESS: the citizen’s Reload buys another three, and so does a new url', () => {
    // A count that survived a Reload would drop the citizen straight back onto the stall card,
    // which is the one state the Reload exists to leave. Same for a new address: a different
    // document has not spent anything.
    //
    // Mutation check: drop `externalReloadNonce` from the reset effect's deps and the middle
    // stretch goes red (the second budget is refused, so the stall never lifts for a full cap);
    // drop `previewUrl` and the last stretch does.
    vi.useFakeTimers()
    try {
      const { container, rerender } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ready" reloadNonce={0} />,
      )
      exhaustTheVouchBudget()
      expect(container.textContent).toMatch(/taking longer than usual/i)

      rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" reloadNonce={1} />)
      expect(container.textContent).not.toMatch(/taking longer than usual/i)
      expect(container.textContent).toMatch(/opening your app/i) // LIVENESS: back in the honest wait
      exhaustTheVouchBudget() // three more re-requests, and only then the label again
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#${VOUCH_RETRY_LIMIT * 2}.1`,
      )
      expect(container.textContent).toMatch(/taking longer than usual/i)

      rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" reloadNonce={1} />)
      expect(container.textContent).not.toMatch(/taking longer than usual/i)
      exhaustTheVouchBudget()
      expect(container.textContent).toMatch(/taking longer than usual/i)
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL_2}#${VOUCH_RETRY_LIMIT * 3}.1`,
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ and NOT by an automatic reload: the app coming back up costs one fetch and two pings, never a fresh budget', () => {
    // A budget reset on an automatic reload would hand a document that never answers a fresh three
    // fetches every time one fired, and the bound on the counter would bound nothing. The edge is
    // still worth ONE fetch — the document that loaded before the app answered really can be
    // stale — and that is the whole of what it is worth. Stated in the component's own words: "an
    // automatic reload afterwards costs one fetch and two pings, never a fresh budget".
    //
    // Mutation check: add the auto nonce to the budget-reset effect's deps and the last stretch
    // goes red — the key climbs past `#4.0` and the stall card never comes back.
    vi.useFakeTimers()
    try {
      const { container, rerender } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ready" />,
      )
      exhaustTheVouchBudget()
      expect(container.textContent).toMatch(/taking longer than usual/i)

      rerender(<LivePreview previewUrl={SANDBOX_URL} status="building" />)
      rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)

      // ONE re-request from the edge, and the honest wait back with it: a new key, so the stall
      // verdict does not outlive the frame it was a complaint about.
      const refetched = container.querySelector('iframe')
      expect(refetched.getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#${VOUCH_RETRY_LIMIT + 1}.0`,
      )
      expect(container.textContent).toMatch(/opening your app/i)

      // …then two pings, which is all a spent budget still buys.
      const post = vi.spyOn(refetched.contentWindow, 'postMessage')
      for (let ping = 0; ping < PINGS_BEFORE_RELOAD; ping += 1) {
        act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS + 1))
        expect(container.querySelector('iframe')).toBe(refetched)
      }
      expect(post).toHaveBeenCalledTimes(PINGS_BEFORE_RELOAD)

      // …and then the label, with NO further re-request however long the tab is left open.
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS + 1))
      expect(container.textContent).toMatch(/taking longer than usual/i)
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS * 5))
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#${VOUCH_RETRY_LIMIT + 1}.0`,
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('a beacon at 4,999ms disarms the wait: the deadline right behind it neither asks nor re-requests', () => {
    // The timer re-reads the vouch at fire time instead of trusting a value it closed over. A
    // beacon that lands a paint before the deadline must not have the document that sent it torn
    // down underneath it — which, on screen, is the citizen's app disappearing at the moment it
    // arrived.
    //
    // ★ AND THE QUIET AFTERWARDS IS BOUNDED BY THE HEARTBEAT, deliberately rather than
    // generously. A vouched frame IS asked again — every HEARTBEAT_MS, for ever, pinned in its
    // own block below — so "nothing more happens" is only ever a claim about the interval before
    // the first beat, and a test that advanced past one would be asserting a silence the contract
    // does not promise. The beat itself is then this test's liveness: it says the silence at the
    // deadline was a wait DECLINING to ask, not a pane left with no timers at all.
    //
    // Mutation check: make the wait indifferent to the vouch — drop `frameVouched` from the
    // effect's deps so the deadline's own timer survives the beacon, and with it the
    // `vouchedKeyRef.current === frameKey` guard at the top of the timer callback — and this goes
    // red at 5,000ms: a ping into a document that has already answered, and a re-request behind
    // it.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      // Spied BEFORE the load, so the load's own ping proves the spy is on the window the pane
      // actually pings — without that, every "no further ping" below would be green over a spy
      // attached to nothing.
      const post = vi.spyOn(container.querySelector('iframe').contentWindow, 'postMessage')
      loadTheFrame(container)
      expect(post).toHaveBeenCalledTimes(1)

      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS - 1))
      vouch(container) // …with one millisecond to spare
      expect(card(container).getAttribute('data-revealed')).toBe('true') // LIVENESS: it landed

      act(() => vi.advanceTimersByTime(1)) // the deadline the beacon just beat
      expect(post).toHaveBeenCalledTimes(1) // still only the load's ping: the wait asked nothing
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#0.0`,
      )

      // …and it stays that way for the whole of the heartbeat's interval bar its last
      // millisecond: the beat was armed by the beacon at 4,999ms, so at 19,998ms it is still one
      // tick away. Nothing is asked in that window, and nothing is fetched again.
      act(() => vi.advanceTimersByTime(HEARTBEAT_MS - 2))
      expect(post).toHaveBeenCalledTimes(1)
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#0.0`,
      )
      expect(card(container).getAttribute('data-revealed')).toBe('true')

      // LIVENESS: one tick later the beat lands — ONE ping, the same element, still revealed.
      act(() => vi.advanceTimersByTime(2))
      expect(post).toHaveBeenCalledTimes(2)
      expect(post).toHaveBeenLastCalledWith(PING, SANDBOX_ORIGIN)
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#0.0`,
      )
      expect(card(container).getAttribute('data-revealed')).toBe('true')
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ under the cover NOTHING is re-requested, however long the wait runs', () => {
    // Under the cover we know exactly why the document has not vouched — a turn is rewriting the
    // app as the citizen watches — so re-requesting would only fetch the same half-written page
    // again. Worse, a timer left running lands the instant the cover clears, so the pane would
    // answer a recovery with "taking longer than usual".
    //
    // Mutation check: delete the `showCover` early return and this goes red twice over — a frame
    // key climbing under the cover, and the stall card waiting on the other side of it.
    vi.useFakeTimers()
    try {
      const { container, rerender } = render(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ready"
          serving
          compileState="unknown"
          turnRunning
        />,
      )
      loadTheFrame(container)
      // Spied after the load, so the load's own ping is out of the count: under the cover the
      // document is not even ASKED, let alone fetched again.
      const post = vi.spyOn(container.querySelector('iframe').contentWindow, 'postMessage')
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS * 30))

      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#0.0`,
      )
      expect(post).not.toHaveBeenCalled()
      expect(container.textContent).not.toMatch(/taking longer than usual to open/i)
      // LIVENESS: the cover is what is holding this screen, so the absences above are a wait being
      // told rather than a pane that stopped rendering.
      expect(coverEl(container)).toBeTruthy()

      // …and the wait starts from the UNCOVER rather than from the remains of the covered stretch:
      // one short wait later, not instantly.
      rerender(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ready"
          serving
          compileState="clean"
          turnRunning
        />,
      )
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS - 1))
      expect(post).not.toHaveBeenCalled()
      act(() => vi.advanceTimersByTime(2))
      // The first ASK, counted from the uncover — and the element is untouched, exactly as on any
      // other key: the wait restarts whole rather than resuming mid-sequence.
      expect(post).toHaveBeenCalledWith(PING, SANDBOX_ORIGIN)
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#0.0`,
      )
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1)) // the second ask…
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1)) // …and then the re-request
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#1.0`,
      )
    } finally {
      vi.useRealTimers()
    }
  })
})

// --- the document that is ALIVE and has nothing to show yet --------------------------------
//
// REVISION 3'S WHOLE SUBJECT, and the case the wait above could not tell apart from the 502 it was
// built for. A bodyless 502 and a Next app three seconds into its first Turbopack route look
// identical from this side: loaded, silent, no beacon. Fetching the address again is the right
// answer for the first — nothing in that response can ever answer a ping, and the ingress catches
// up on its own — and the worst possible answer for the second, because the fetch tears down the
// very hydration that was about to produce the beacon. So the template answers a ping it cannot
// yet satisfy with `bial:app-painting`, and a document that says that is ASKED again, twelve times
// over, and NEVER fetched again — its own beacon still reveals it whenever it finally paints, and
// the label at the end of the asking says "slow", never "dead".
const ALIVE_PINGS_BEFORE_STALL = 12 // asks an ALIVE document gets before the wait is LABELLED

// The painting reply, written out as a literal for the same reason the beacon above is: this pair
// is a CONTRACT WITH ANOTHER REPOSITORY'S FILE (`sandbox/template/instrumentation-client.ts`) and
// the value drifting is exactly the failure, so a test that imported the constant it pins would
// assert only that the code equals itself. It carries the same REQUIRED `path` the beacon does —
// one hostname for the whole fleet, so the path is the only field that says which app answered.
const PAINTING_REPLY = { type: 'bial:app-painting', path: '/' }

/**
 * The framed document answering a ping with "alive, nothing to show yet".
 *
 * Built through `fromSandbox` so it carries BOTH halves of the provenance gate (the origin of the
 * url currently framed AND this pane's own frame window), exactly as `vouch` does — this message
 * buys its sender ten more asks and immunity from ever being fetched again, so it is gated on the
 * same two facts as the reveal. The tests that need a half missing build their own by hand.
 */
function answersPainting(container, origin = SANDBOX_ORIGIN, path = '/') {
  const iframe = container.querySelector('iframe')
  act(() => {
    window.dispatchEvent(fromSandbox({ ...PAINTING_REPLY, path }, iframe.contentWindow, origin))
  })
  return iframe
}

describe('LivePreview — a document that says it is painting is asked again, never fetched again', () => {
  it('★ a document that answers EVERY ask is asked twelve times and never remounted, then labelled — and its own beacon still reveals it', () => {
    // The asking is bounded but the ELEMENT is untouchable: twelve asks on one document, at one
    // key, and then the honest label. A page that takes half a minute to paint gets the whole of
    // that minute to do it, and the only thing that ends the wait early is the page itself.
    //
    // ★ AND THE MARK IS SPENT BY THE ASK THAT READS IT, which is why this document answers every
    // one of them. "Alive" is a claim about the moment it is made: the expiry that reads the mark
    // clears it before it pings, so staying immune from being fetched again costs a fresh
    // `bial:app-painting` per ask. One reply from a page that then died buys it nothing past the
    // next expiry — that half is the sibling below.
    //
    // Mutation check: delete the `aliveKeyRef.current === frameKey` arm and this goes red at the
    // third expiry — the element is replaced and the key climbs to `#1.0`; raise
    // ALIVE_PINGS_BEFORE_STALL past the loop and the label never appears; lower it and the loop
    // finds the stall card while it is still counting asks.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      const first = container.querySelector('iframe')
      loadTheFrame(container)
      // Spied AFTER the load, so the load's own ping is out of the count and every call below is
      // one the WAIT made.
      const post = vi.spyOn(first.contentWindow, 'postMessage')
      answersPainting(container) // the answer to the load's ping: alive, nothing painted yet

      for (let ask = 1; ask <= ALIVE_PINGS_BEFORE_STALL; ask += 1) {
        act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
        // ASKED, NEVER REPLACED — the same element and the same key, twelve times over, with the
        // ask count the only thing that moves. This is the assertion the revision exists for.
        expect(container.querySelector('iframe')).toBe(first)
        expect(first.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#0.0`)
        expect(post).toHaveBeenCalledTimes(ask)
        expect(container.textContent).toMatch(/opening your app/i)
        answersPainting(container) // …and it answers THIS ask too: the mark is spent per ask
      }
      // Every one of them a `bial:ping`, at the preview origin and never at `'*'`.
      for (const [message, targetOrigin] of post.mock.calls) {
        expect(message).toEqual(PING)
        expect(targetOrigin).toBe(SANDBOX_ORIGIN)
      }

      // …and the asking IS bounded: the next expiry labels the wait instead of asking a
      // thirteenth time — and still does not fetch the address again.
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
      expect(container.textContent).toMatch(/Your app is taking longer than usual to open/i)
      expect(post).toHaveBeenCalledTimes(ALIVE_PINGS_BEFORE_STALL)
      expect(container.querySelector('iframe')).toBe(first)
      expect(first.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#0.0`)

      // …and nothing moves after that, however long the citizen leaves the tab open.
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS * 10))
      expect(container.querySelector('iframe')).toBe(first)
      expect(post).toHaveBeenCalledTimes(ALIVE_PINGS_BEFORE_STALL)

      // LIVENESS, and the promise the card's own copy makes: the document's beacon still wins
      // after the label, so the absences above are a wait being told rather than a dead pane.
      vouch(container)
      expect(card(container).getAttribute('data-revealed')).toBe('true')
      expect(container.textContent).not.toMatch(/taking longer than usual/i)
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ a document that answered ONCE and then went silent is asked twice more, and then fetched again', () => {
    // THE OTHER HALF OF "SPENT PER ASK", and the one that keeps the immunity bounded. A page that
    // answered a single ping and then died — a hydration that threw, a dev server that went away
    // mid-render — is a SILENT document by the next expiry, and a silent document is asked twice
    // and then fetched again, exactly as one that never answered at all. Without that, one reply
    // ever bought a dead page twelve asks and permanent immunity from the one thing that could
    // have recovered it.
    //
    // Mutation check: drop `aliveKeyRef.current = null` from the alive arm of the timer and this
    // goes red at the fourth expiry — the mark never expires, so the document is asked a fourth
    // time instead of being re-requested and the key never leaves `#0.0`.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      const first = container.querySelector('iframe')
      loadTheFrame(container)
      // Spied AFTER the load, so the load's own ping is out of the count and every call below is
      // one the WAIT made.
      const post = vi.spyOn(first.contentWindow, 'postMessage')
      answersPainting(container) // …and this is the last thing it ever says

      // The ask the mark buys: asked, not fetched.
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
      expect(post).toHaveBeenCalledTimes(1)
      expect(container.querySelector('iframe')).toBe(first)
      expect(first.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#0.0`)

      // …and then the ORDINARY budget, because the silence has made it an ordinary document
      // again: two asks, the element untouched through both.
      for (let ping = 1; ping <= PINGS_BEFORE_RELOAD; ping += 1) {
        act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
        expect(post).toHaveBeenCalledTimes(ping + 1)
        expect(container.querySelector('iframe')).toBe(first)
        expect(first.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#0.0`)
      }

      // …and then the re-request, which is the whole difference between this document and the one
      // above that kept answering. LIVENESS for every "still `#0.0`" assertion before it: the key
      // does move, so those were a document being spared rather than a wait that had stopped.
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
      expect(container.querySelector('iframe')).not.toBe(first)
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#1.0`,
      )
      expect(container.querySelector('iframe').getAttribute('src')).toBe(SANDBOX_URL)
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ a painting reply from a WRONG ORIGIN, or from another window, buys the document nothing', () => {
    // The painting reply is read INSIDE the same provenance gate the beacon is, and it has to be.
    // It reveals nothing, but it does buy its sender ten more asks and permanent immunity from
    // being fetched again — so if any frame on the shared apps host could send it, any of them
    // could hold this pane on a document that is never coming back.
    //
    // Mutation check: hoist the PAINTING_TYPE check above the origin/source checks and the first
    // half goes red — the re-request never comes.
    //
    // ASSERT-ABSENCE, PAIRED WITH LIVENESS: jsdom swallows a throw inside a window listener, so
    // "it remounted anyway" is equally green over a listener that died on the first message. The
    // second half sends the byte-identical payload from the pane's OWN frame at the preview origin
    // and watches the re-requesting stop.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      const first = container.querySelector('iframe')
      loadTheFrame(container)
      const post = vi.spyOn(first.contentWindow, 'postMessage')
      const impostor = document.body.appendChild(document.createElement('iframe'))
      try {
        act(() => {
          // Right window, wrong origin…
          window.dispatchEvent(
            fromSandbox(PAINTING_REPLY, first.contentWindow, 'https://evil.example'),
          )
          // …and right origin, wrong window — a sibling app frame inside this same portal
          // document, which is the only reachable impostor now every app shares one hostname.
          window.dispatchEvent(fromSandbox(PAINTING_REPLY, impostor.contentWindow))
        })
      } finally {
        impostor.remove()
      }

      // Two asks, exactly as for a document that said nothing at all…
      for (let ping = 1; ping <= PINGS_BEFORE_RELOAD; ping += 1) {
        act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
        expect(container.querySelector('iframe')).toBe(first)
        expect(post).toHaveBeenCalledTimes(ping)
      }
      // …and then the re-request neither of those two messages bought any immunity from.
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
      const refetched = container.querySelector('iframe')
      expect(refetched).not.toBe(first)
      expect(refetched.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#1.0`)

      // LIVENESS: the SAME payload, from this pane's own frame at the preview origin, does mark
      // the document alive — three expiries later it has been asked three times and the element
      // has not been touched, where the rejected pair had it replaced on the third.
      loadTheFrame(container)
      const postAgain = vi.spyOn(refetched.contentWindow, 'postMessage')
      answersPainting(container)
      for (let ask = 1; ask <= PINGS_BEFORE_RELOAD + 1; ask += 1) {
        act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
        expect(container.querySelector('iframe')).toBe(refetched)
        expect(postAgain).toHaveBeenCalledTimes(ask)
      }
      expect(refetched.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#1.0`)
    } finally {
      vi.useRealTimers()
    }
  })

  it('the painting reply is CONSUMED like the beacon — it reveals nothing and reaches no receiver', () => {
    // It is this pane's own evidence about its own wait, not a report about the app: forwarded, it
    // would reach the client-error relay as an envelope that means nothing there, and the harness
    // would have to learn a wire message it has no use for.
    //
    // Mutation check: drop the `return` after `aliveKeyRef.current = ...` and the receiver below
    // sees it. LIVENESS: an ordinary message from the same frame still gets through, so the
    // silence is this message being eaten rather than the seam being unwired.
    const onFrameMessage = vi.fn()
    const { container, iframe } = setup({ onFrameMessage })
    answersPainting(container)
    expect(onFrameMessage).not.toHaveBeenCalled()
    expect(card(container).getAttribute('data-revealed')).toBe('false') // alive is not painted
    window.dispatchEvent(fromSandbox({ kind: 'client_error' }, iframe.contentWindow))
    expect(onFrameMessage).toHaveBeenCalledTimes(1)
  })

  it('★ a SECOND load at the same key hands the new document its own asking budget', () => {
    // A page that replaces itself from the inside — a dev-server restart does exactly this — is a
    // NEW document in an old element. It has not been asked anything, and the asks the last one
    // ignored are not its debt: inheriting a spent budget has it fetched again on its very first
    // expiry, tearing down the document that had only just arrived.
    //
    // Mutation check: delete `pingsRef.current = 0` from the reloaded arm of `onFrameLoad` and the
    // element is replaced at the first expiry after the second load, so `#0.0` never survives it.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      const first = container.querySelector('iframe')
      // Spied BEFORE the load, so the load's own ping proves the spy is on the window this pane
      // actually pings — without it every count below would be green over a spy on nothing.
      const post = vi.spyOn(first.contentWindow, 'postMessage')
      loadTheFrame(container)
      expect(post).toHaveBeenCalledTimes(1)

      // The whole asking budget, spent: one more expiry and this address would be fetched again.
      for (let ping = 1; ping <= PINGS_BEFORE_RELOAD; ping += 1) {
        act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
        expect(container.querySelector('iframe')).toBe(first)
      }
      expect(post).toHaveBeenCalledTimes(PINGS_BEFORE_RELOAD + 1)

      // …and then the document replaces itself, which asks the new one once on the spot.
      loadTheFrame(container)
      expect(post).toHaveBeenCalledTimes(PINGS_BEFORE_RELOAD + 2)

      // TWO MORE ASKS before anything is fetched again — the new document's own budget, and the
      // element untouched through both of them.
      for (let ping = 1; ping <= PINGS_BEFORE_RELOAD; ping += 1) {
        act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
        expect(container.querySelector('iframe')).toBe(first)
        expect(first.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#0.0`)
      }
      expect(post).toHaveBeenCalledTimes(PINGS_BEFORE_RELOAD * 2 + 2)

      // LIVENESS: a FRESH budget, not an exemption — the expiry behind it still re-requests, so
      // the two asks above are a budget being spent rather than a wait that stopped counting.
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
      expect(container.querySelector('iframe')).not.toBe(first)
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#1.0`,
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ the cover hands back a FULL asking budget when it comes down', () => {
    // Any progress the wait made under the cover is dropped, and that now includes the asking
    // budget. Under the cover we know exactly why the document has not vouched, so the asks it did
    // not get to make were never questions it refused to answer — and restarting the wait while
    // keeping the spent count would have the first expiry after a recovery FETCH the app again,
    // which is the one thing this wait is careful never to do first.
    //
    // Mutation check: delete `pingsRef.current = 0` from the `showCover` arm of the wait and the
    // expiry after the uncover replaces the element instead of asking it.
    vi.useFakeTimers()
    try {
      const { container, rerender } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ready" serving turnRunning={false} />,
      )
      const first = container.querySelector('iframe')
      loadTheFrame(container)
      const post = vi.spyOn(first.contentWindow, 'postMessage')

      // The budget, spent in the open: no cover, no verdict, nobody to blame for the silence.
      for (let ping = 1; ping <= PINGS_BEFORE_RELOAD; ping += 1) {
        act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
      }
      expect(post).toHaveBeenCalledTimes(PINGS_BEFORE_RELOAD)
      expect(container.querySelector('iframe')).toBe(first)

      // The cover goes up — a route is compiling — and the wait stops counting entirely…
      rerender(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ready"
          serving
          compileState="building"
          turnRunning={false}
        />,
      )
      expect(coverEl(container)).toBeTruthy() // LIVENESS: it really is covered
      act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS * 3))
      expect(post).toHaveBeenCalledTimes(PINGS_BEFORE_RELOAD)
      expect(container.querySelector('iframe')).toBe(first)

      // …and then it comes down again.
      rerender(
        <LivePreview
          previewUrl={SANDBOX_URL}
          status="ready"
          serving
          compileState="clean"
          turnRunning={false}
        />,
      )
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
      // ASKED, not replaced: the first expiry on the far side of the cover spends a budget that
      // starts again from zero, even though this exact document had already spent one.
      expect(container.querySelector('iframe')).toBe(first)
      expect(first.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#0.0`)
      expect(post).toHaveBeenCalledTimes(PINGS_BEFORE_RELOAD + 1)

      // LIVENESS: a fresh budget rather than an exemption — the second ask lands, and only the
      // expiry after THAT fetches the address again.
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
      expect(post).toHaveBeenCalledTimes(PINGS_BEFORE_RELOAD + 2)
      expect(container.querySelector('iframe')).toBe(first)
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#1.0`,
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ an ask that never LEFT is charged all the same — a throwing postMessage still ends at the label', () => {
    // THE BUDGET PAYS FOR THE ATTEMPT, NOT THE DELIVERY, and the reversal is the whole point of
    // this test. Charged only for questions that were received, a frame nothing can reach is
    // asked for ever and never once called slow: the pane waits politely on a window that cannot
    // answer, with no fetch, no card and no end — a wait that runs out of nothing is not a wait,
    // it is the white rectangle again with a spinner over it. So an un-delivered ask still moves
    // this document toward being fetched again, and then toward the label.
    //
    // jsdom will not detach a contentWindow underneath us, so the failure is forced at the only
    // seam that produces it: the postMessage the ping goes out through.
    //
    // Mutation check: charge only for delivery (`if (pingFrame()) pingsRef.current += 1`) and
    // this goes red at the third expiry — the element is never replaced, the key never leaves
    // `#0.0`, and the label at the end never arrives at all.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      const first = container.querySelector('iframe')
      const throwOnAsk = () => {
        throw new Error('the window went away mid-navigation')
      }
      const post = vi.spyOn(first.contentWindow, 'postMessage').mockImplementation(throwOnAsk)
      loadTheFrame(container)
      // LIVENESS for every count below: the spy is on the window this pane actually pings, and a
      // throwing ask is swallowed rather than taking the pane down with it.
      expect(post).toHaveBeenCalledTimes(1)
      expect(container.textContent).toMatch(/opening your app/i)

      // TWO attempts, neither of which left, and the element untouched through both…
      for (let attempt = 1; attempt <= PINGS_BEFORE_RELOAD; attempt += 1) {
        act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
        expect(post).toHaveBeenCalledTimes(attempt + 1)
        expect(container.querySelector('iframe')).toBe(first)
        expect(first.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#0.0`)
      }

      // …and then the re-request those undelivered asks paid for.
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
      expect(container.querySelector('iframe')).not.toBe(first)
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#1.0`,
      )
      expect(container.textContent).not.toMatch(/taking longer than usual/i) // a wait, not a label

      // The rest of the budget, spent the same way. Each fetched document throws on every ask it
      // is given and costs three expiries — two asks and a fetch — and the LAST of them is
      // labelled instead of fetched a fourth time. No `load` fires for these, so every expiry
      // here is the full cap rather than the short wait.
      for (let key = 1; key <= VOUCH_RETRY_LIMIT; key += 1) {
        vi.spyOn(
          container.querySelector('iframe').contentWindow,
          'postMessage',
        ).mockImplementation(throwOnAsk)
        for (let expiry = 1; expiry <= PINGS_BEFORE_RELOAD + 1; expiry += 1) {
          act(() => vi.advanceTimersByTime(FRAME_LOAD_CAP_MS + 1))
        }
      }
      expect(container.textContent).toMatch(/Your app is taking longer than usual to open/i)
      expect(container.querySelector('iframe').getAttribute('data-frame-key')).toBe(
        `${SANDBOX_URL}#${VOUCH_RETRY_LIMIT}.0`,
      )

      // LIVENESS, and the promise the card's own copy makes: the frame is still mounted, so the
      // document's own beacon reveals it the moment it can send one — the label is a wait being
      // named, never a pane that gave up.
      vouch(container)
      expect(card(container).getAttribute('data-revealed')).toBe('true')
      expect(container.textContent).not.toMatch(/taking longer than usual/i)
    } finally {
      vi.useRealTimers()
    }
  })
})

// --- the heartbeat: a page that goes blank AFTER it painted ---------------------------------
//
// The reveal rests on one claim — "there is something to look at in this frame" — and that claim
// can stop being true with nothing on this side to notice. A client-side route to a page that
// renders nothing fires no `load`, changes no url the pane can read, and reports no compile: the
// frame simply goes white under a pane that is still calling it revealed. So a vouched frame is
// asked again, slowly, for ever; the page answers a ping it can no longer satisfy with
// `bial:app-painting`, and that answer takes the reveal back.
//
// SLOWLY IS THE WHOLE DESIGN. Every ask costs the framed app a message-channel round trip, so the
// heartbeat runs at three times the silent document's wait — long enough to be free, short enough
// that a blank page is not left presented as somebody's app for a minute.
describe('LivePreview — the heartbeat: a revealed page that goes blank is found out', () => {
  it('★ a vouched frame is asked every fifteen seconds, and never fetched again', () => {
    // ASKED, NOT REPLACED, and the distinction is the same one the whole wait is built on: a
    // re-request tears down a working app the citizen is using — scroll position, form state and
    // the HMR socket with it — to answer a question a ping answers for free.
    //
    // Mutation check: delete the `frameVouched` heartbeat arm of the wait and the first beat
    // never lands; re-request instead of asking (fall through to the retry arm) and `#0.0` is
    // gone by the first beat. Two beats rather than one, because a heartbeat that fires once and
    // stops is exactly what a missing `setAskedAgain` re-arm looks like.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      const first = container.querySelector('iframe')
      // Spied BEFORE the load, so the load's own ping proves the spy is on the window the pane
      // actually pings — without it every count below would be green over a spy on nothing.
      const post = vi.spyOn(first.contentWindow, 'postMessage')
      loadTheFrame(container)
      expect(post).toHaveBeenCalledTimes(1)
      vouch(container)
      expect(card(container).getAttribute('data-revealed')).toBe('true') // LIVENESS: it landed

      for (let beat = 1; beat <= 2; beat += 1) {
        act(() => vi.advanceTimersByTime(HEARTBEAT_MS - 1))
        expect(post).toHaveBeenCalledTimes(beat) // the load's ping, plus the beats before this one

        act(() => vi.advanceTimersByTime(1))
        // EXACTLY ONE ping per interval, at the preview origin and never at `'*'`, into the same
        // element at the same key — and the citizen still looking at their app throughout.
        expect(post).toHaveBeenCalledTimes(beat + 1)
        expect(post).toHaveBeenLastCalledWith(PING, SANDBOX_ORIGIN)
        expect(container.querySelector('iframe')).toBe(first)
        expect(first.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#0.0`)
        expect(card(container).getAttribute('data-revealed')).toBe('true')
      }
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ a painting answer to the heartbeat RETRACTS the reveal, and the page`s next beacon gives it back', () => {
    // "Alive, nothing to show yet" from the document that is currently revealed is that document
    // telling this pane its reveal has expired — it painted once and has nothing on screen now.
    // The honest answer is to hide it and go back to waiting, on the ALIVE path: asked again
    // rather than torn down, because the page is plainly still running and its own beacon is what
    // ends the wait.
    //
    // Mutation check: drop the `vouchedKeyRef.current === sentBy` retraction from the painting
    // arm of the listener and the reveal never comes down — a blank page stays presented as the
    // citizen's app. Keep the retraction but fetch instead of asking, and the element below is
    // replaced under a page that never stopped running.
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      const first = container.querySelector('iframe')
      const post = vi.spyOn(first.contentWindow, 'postMessage')
      loadTheFrame(container)
      vouch(container)
      expect(card(container).getAttribute('data-revealed')).toBe('true')

      act(() => vi.advanceTimersByTime(HEARTBEAT_MS)) // the beat asks…
      expect(post).toHaveBeenCalledTimes(2)

      answersPainting(container) // …and the answer is that there is nothing to see any more
      expect(card(container).getAttribute('data-revealed')).toBe('false')
      // A REVEAL TAKEN BACK, NOT A DOCUMENT TORN DOWN: same element, same key, and the pane says
      // what it always says while it waits.
      expect(container.querySelector('iframe')).toBe(first)
      expect(first.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#0.0`)
      expect(container.textContent).toMatch(/opening your app/i)

      // …and the ordinary wait takes over on the alive path — asked again after the SHORT wait,
      // still never fetched again.
      act(() => vi.advanceTimersByTime(VOUCH_AFTER_LOAD_MS + 1))
      expect(post).toHaveBeenCalledTimes(3)
      expect(post).toHaveBeenLastCalledWith(PING, SANDBOX_ORIGIN)
      expect(container.querySelector('iframe')).toBe(first)
      expect(first.getAttribute('data-frame-key')).toBe(`${SANDBOX_URL}#0.0`)

      // LIVENESS, and the point of retracting at all: the page paints again, says so, and is
      // revealed again — the retraction is a reveal that can be re-earned, not a verdict.
      vouch(container)
      expect(card(container).getAttribute('data-revealed')).toBe('true')
      expect(container.textContent).not.toMatch(/opening your app/i)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('★ the reconnecting cover is NO LONGER BOUNDED here — the bound moved to the server', () => {
  // ★ THE CAP IS GONE AND ITS LANDING STATE IS WHY. A 20-second timer used to collapse this cover
  // into the "preview unavailable" card, which was one of the four WORKSPACE verdicts this file
  // has stopped authoring — so an expiry now has nowhere honest to go. Letting it expire into an
  // empty rectangle says nothing; re-mounting the frame instead frames the apps router's own
  // "This app isn't running right now" page, which is the exact defect this whole change exists
  // to end.
  //
  // WHERE THE BOUND WENT, so this is a move and not a deletion: a dev server that dies and never
  // comes back is the SERVING STAMP's business now. The turn watcher clears the stamp on the
  // debounced crash edge and the five-minute reconciler clears it out of turn, the reading stops
  // being `running`, `AppPane` unmounts this pane, and the ONE workspace author draws the ONE
  // card. What this pane owes that citizen in the meantime is not a verdict — it is to keep
  // saying, honestly, that it is still waiting.
  it('★ keeps saying "Reconnecting…" indefinitely, and never reaches for a verdict', () => {
    vi.useFakeTimers()
    try {
      const onRelaunch = vi.fn()
      const { container } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ended" serving reconnecting onRelaunch={onRelaunch} />,
      )
      expect(container.textContent).toMatch(/reconnecting/i)

      // Six times the old cap. Nothing collapses, nothing is claimed.
      act(() => vi.advanceTimersByTime(120_000))

      // LIVENESS FIRST, and it is what makes the two absences below mean anything: the cover is
      // still on screen saying the honest thing.
      expect(container.textContent).toMatch(/reconnecting to your preview/i)
      expect(container.textContent).not.toMatch(/preview unavailable/i)
      expect(container.textContent).not.toMatch(/no longer running/i)
      expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
      expect(onRelaunch).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ nor while the build is still ACTIVE — the loop owns recovery, and LIVENESS does not change that', () => {
    // ★ `serving` IS PASSED HERE ON PURPOSE, and it is the mutant this scenario has always caught.
    // Reading `reconnecting && serving` as "the build is over" is wrong: liveness is true DURING a
    // running build as well, so a cap keyed on it would fire mid-build and answer a recovery the
    // loop was about to make. There is no cap at all now, which makes that mutant unreachable
    // rather than merely guarded — and this scenario stays as the record of why.
    vi.useFakeTimers()
    try {
      const { container } = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ready" serving reconnecting />,
      )
      act(() => vi.advanceTimersByTime(60000))
      expect(container.textContent).toMatch(/reconnecting/i)
      expect(container.textContent).not.toMatch(/preview unavailable/i)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a fresh preview_ready clears the cover and re-frames, however long the wait ran', () => {
    // The recovery half, and the reason an uncapped wait is not an abandoned one: the cover is
    // self-healing by construction — a restarted dev server produces a fresh `preview_ready`.
    vi.useFakeTimers()
    try {
      const view = render(<LivePreview previewUrl={SANDBOX_URL} status="ended" serving reconnecting />)
      act(() => vi.advanceTimersByTime(300_000))
      expect(view.container.querySelector('iframe')).toBeNull()

      view.rerender(<LivePreview previewUrl={SANDBOX_URL} status="ended" serving reconnecting={false} />)

      expect(view.container.querySelector('iframe')).toBeTruthy()
      expect(view.container.textContent).not.toMatch(/reconnecting to your preview/i)
    } finally {
      vi.useRealTimers()
    }
  })
})

// THE SAVE CONTROL LEFT THIS COMPONENT — it drew in the toolbar row this pane owned, so a
// project with nothing built had no Save at all. It reads the channel's own save cell from the
// shell's row now; every one of the six scenarios that were here, including the one that matters
// most (`null` is UNKNOWN and hides the control rather than claiming the work is saved), is in
// `WorkspaceToolbar.test.tsx`. Named rather than deleted quietly, because a guard that vanishes
// with its markup is how the claim stops being checked.

describe('★ the saved-build claim left this pane entirely, along with the prop that made it', () => {
  // ★ WHAT THESE FIVE TESTS WERE, AND WHY THEY COLLAPSE INTO ONE. Each drove a different
  // `hasSavedBuild` value through a different terminal branch and pinned the sentence it selected:
  // "nothing to relaunch yet", "your saved app is still there", "start a new build". The exact bug
  // they closed was real — a fresh, never-built project opened on the terminal placeholder,
  // promised to restore a saved app and offered a Relaunch that could only 404.
  //
  // ★ THE FINDING IS NOT DROPPED; IT MOVED WITH ITS SUBJECT. "Do not promise a restore the server
  // cannot make" is now `atRest()` in `workspace/workspaceState.ts`, where `restorable`'s TRI-STATE
  // is read with `??` against the project row's own answer and only a definite `false` suppresses
  // the start control — asserted in `workspaceState.test.ts` under "the restore question, and the
  // one answer that suppresses the start control", and drawn by a board `AppPane.test.tsx` pins.
  // Both files are named here rather than left to be found, because a guard that vanishes with its
  // markup is how a claim stops being checked.
  //
  // WHAT IS LEFT TO ASSERT HERE IS THE PROP'S SILENCE: every value of it, through every branch
  // that used to read it, reaching no sentence at all.
  it('★ RETIREMENT GUARD: no value of `hasSavedBuild` selects any sentence on this pane', () => {
    const everyClaim = [
      /nothing to relaunch yet/i,
      /your saved app is still there/i,
      /start a new build/i,
      /no saved build/i,
      /relaunch it|relaunch the preview/i,
    ]

    for (const hasSavedBuild of [true, false, null]) {
      // The branch that used to be the terminal placeholder.
      const terminal = render(
        <LivePreview previewUrl={null} status="ended" onRelaunch={vi.fn()} hasSavedBuild={hasSavedBuild} />,
      )
      const where = `hasSavedBuild=${String(hasSavedBuild)}`
      for (const claim of everyClaim) {
        expect(terminal.container.textContent, where).not.toMatch(claim)
      }
      expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
      // ★ LIVENESS, STRUCTURAL, because the expected screen is empty: an absence sweep over a
      // component that threw would pass every line above. The permanent live region is the one
      // element that exists in every state of this pane.
      expect(terminal.container.querySelector('[role="status"]')?.getAttribute('aria-live'), where).toBe('polite')
      terminal.unmount()

      // And the branch that used to be the unavailable card, long past the retired cap.
      vi.useFakeTimers()
      try {
        const crashed = render(
          <LivePreview
            previewUrl={SANDBOX_URL}
            status="ended"
            serving
            reconnecting
            onRelaunch={vi.fn()}
            hasSavedBuild={hasSavedBuild}
          />,
        )
        act(() => vi.advanceTimersByTime(20001))
        for (const claim of everyClaim) {
          expect(crashed.container.textContent, where).not.toMatch(claim)
        }
        expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
        // LIVENESS, and here it can be the visible one: the reconnecting cover is what holds this
        // screen now, so the absences above are a withheld claim rather than a blank pane.
        expect(crashed.container.textContent, where).toMatch(/reconnecting to your preview/i)
        crashed.unmount()
      } finally {
        vi.useRealTimers()
      }
    }
  })
})

describe('LivePreview — the device width it is told to frame at', () => {
  // THE SWITCHER IS NOT IN THIS COMPONENT ANY MORE. It is in the shell's toolbar row, above both
  // columns, so the three `aria-pressed` scenarios live in `WorkspaceToolbar.test.tsx` — where
  // the control is. What stays here is the half this
  // component still owns: that the width it is TOLD reaches the card's inline style.
  //
  // The iframe itself is plain `w-full`: it always matches the card's width exactly, with no
  // competing inline value of its own. So the device pixel width lives on the WRAPPER's inline
  // style, and that is what these assert — that the inline style got SET, not that the framed
  // document reflows against it (jsdom has no layout engine; the real-sandbox Playwright spec
  // owns that half). Queried by data-testid rather than `iframe.parentElement`, so an element
  // inserted between the card and the iframe cannot silently retarget these at the wrong node.
  function deviceCard(container) {
    return container.querySelector('[data-testid="device-card"]')
  }

  it('defaults to full width when nobody says otherwise', () => {
    const { container } = setup()
    expect(deviceCard(container).style.width).toBe('100%')
  })

  it('frames at 390px for Mobile (iPhone class)', () => {
    expect(deviceCard(setup({ device: 'Mobile' }).container).style.width).toBe('390px')
  })

  it('no per-mode height is imposed on the wrapper — no fixed device aspect ratio', () => {
    for (const device of ['Desktop', 'Mobile']) {
      const { container } = setup({ device })
      expect(deviceCard(container).style.height).toBe('')
      cleanup()
    }
  })

  it('the card keeps relative + overflow-hidden in every mode — anchors/clips the overlays', () => {
    for (const device of ['Desktop', 'Mobile']) {
      const { container } = setup({ device })
      expect(deviceCard(container).className).toMatch(/relative/)
      expect(deviceCard(container).className).toMatch(/overflow-hidden/)
      cleanup()
    }
  })
})

describe('★ the two compact cards are gone — and their geometry rule outlived them', () => {
  // ★ WHAT THEY WERE. `preview-ended-card` and `preview-unavailable-card` were the last two
  // renderers of this file's workspace verdicts, and these four tests pinned their SHAPE: bounded
  // (`max-w-xs`), centred by their parent, never the old full-pane stretch (`flex-1`). Both cards
  // are deleted with the sentences they drew, so their test hooks answer nothing.
  //
  // ★ THE RULE THEY ENCODED IS WORTH MORE THAN THE CARDS, so it is re-pointed rather than dropped:
  // a card that fills the pane reads as the app having been replaced by an error page, which is
  // the impression this whole change exists to stop giving. The two covers that survive keep it —
  // they are anchored overlays with bounded text, never stretched blocks — and that is what is
  // asserted below, on the covers a citizen can still reach.
  it('★ RETIREMENT GUARD: neither card`s test hook answers in any state that used to draw one', () => {
    const oncePinned = [
      ['terminal, saved build', { previewUrl: null, status: 'ended', hasSavedBuild: true }],
      ['terminal, nothing saved', { previewUrl: null, status: 'ended', hasSavedBuild: false }],
      ['failed build', { previewUrl: null, status: 'failed', hasSavedBuild: true }],
    ]

    for (const [where, props] of oncePinned) {
      const { container, unmount } = render(<LivePreview onRelaunch={vi.fn()} {...props} />)
      expect(container.querySelector('[data-testid="preview-ended-card"]'), where).toBeNull()
      expect(container.querySelector('[data-testid="preview-unavailable-card"]'), where).toBeNull()
      // ★ LIVENESS, STRUCTURAL: the pane rendered and had nothing to say, which is not the same
      // thing as the pane failing to render.
      expect(container.querySelector('[role="status"]')?.getAttribute('aria-live'), where).toBe('polite')
      unmount()
    }
  })

  it('★ and the covers that survive are still BOUNDED, never a full-pane block', () => {
    // The geometry finding, re-pointed at the surfaces that still exist. A cover is an anchored
    // overlay with a `max-w` on its text; the moment one of these stretches, the pane reads as a
    // replaced app rather than as an app with something in front of it.
    vi.useFakeTimers()
    try {
      const stalled = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      exhaustTheVouchBudget()
      const stallText = [...stalled.container.querySelectorAll('p')].find((el) =>
        /taking longer than usual to open/i.test(el.textContent ?? ''),
      )
      expect(stallText).toBeTruthy() // LIVENESS: the stall card really is up
      expect(stalled.container.querySelector('.absolute.inset-0.z-20')).toBeTruthy()
      expect([...stalled.container.querySelectorAll('.max-w-xs')].length).toBeGreaterThan(0)
      stalled.unmount()
    } finally {
      vi.useRealTimers()
    }

    const covered = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" serving compileState="failed" />,
    )
    const coverText = [...covered.container.querySelectorAll('p')].find((el) =>
      /this page can’t open/i.test(el.textContent ?? ''),
    )
    expect(coverText).toBeTruthy() // LIVENESS: the compile cover really is up
    expect(coverText.className).toMatch(/max-w-sm/)
    expect(coverText.className).not.toMatch(/flex-1/)
  })
})

describe('LivePreview — a live preview is left alone', () => {
  it('with no server verdict the pane keeps framing what it has — absence is not a verdict', () => {
    // The `previewState` prop defaults to null (NOT YET ASKED). The four-state rendering and
    // the reclaimed cases live in LivePreview.test.tsx, next to the wire shape that drives them.
    const { container } = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ended" serving />,
    )
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(container.textContent).not.toMatch(/preview unavailable/i)
    expect(container.textContent).not.toMatch(/asleep/i)
  })
})

// ---------------------------------------------------------------------------------------
// THE COVER
//
// A full-screen framework compile-error screen once filled this pane for over a minute in each
// of three consecutive builds, in front of a client. This is the fix, and it is a fix that
// reaches apps ALREADY BUILT: the pane covers its own frame from the outside, so nothing about
// the app — its Next version, its files, its image — is consulted or changed.
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
// work it describes. TWO sentences, because the cover's two idle causes are opposites:
// `failed` means the newest change did not come together, `building` means the app is compiling a
// route right now — which a perfectly healthy completed app does on demand.
//
// ★ BOTH WERE REWRITTEN, AND BOTH FOR THE SAME REASON: they described the WORKSPACE, which is not
// this pane's subject. `IDLE_BUSY` read "Getting your app ready…", which is word for word the
// sentence the workspace map says while nothing is serving, so one situation had two authors on
// one screen. `IDLE_BROKEN` opened "Your app isn't running right now" — the same claim the apps
// router's own error page makes, told from inside a pane that is mounted only because the app IS
// up; on 2026-09-10 the two of them contradicted each other in front of a citizen. The rule now is
// that a cover may describe the DOCUMENT in front of it and nothing else.
const IDLE_BROKEN = /The last change didn.t come together, so this page can.t open/i
const IDLE_BUSY = /Putting this page together/i

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

describe('LivePreview — the cover: the framework error screen is never seen', () => {
  it('covers the frame when the app fails to compile, and shows the holding state', () => {
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

  it('covers an app built before any of this shipped — no version is ever consulted', () => {
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
    //
    // ★ READ BETWEEN TURNS, ON PURPOSE. `covered` is `coveredByVerdict || flyingBlind`, and the
    // second arm covers ANY running turn without a `clean` verdict — so with `turnRunning` set
    // this would be asserting the OTHER half of the cover, backwards. What is pinned here is the
    // VERDICT's half: `unknown` moves nothing on its own. (`LivePreview.test.tsx` owns the
    // flying-blind arm, together with the incident that produced it.)
    const { container, rerender } = setup({ compileState: 'clean' })
    expect(coverEl(container)).toBeFalsy()

    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="unknown" />)
    expect(coverEl(container)).toBeFalsy()
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('holds the cover down when nothing has been reported at all', () => {
    // `null` is "no signal on this turn", which is how the pane behaved before the cover
    // existed. It must not raise a cover nobody asked for.
    //
    // ★ BETWEEN TURNS, for the reason the test above records: a running turn with no `clean`
    // verdict is covered by the cover's OTHER arm, which is a different claim from this one.
    const { container } = setup({ compileState: null })
    expect(coverEl(container)).toBeFalsy()
    expect(container.querySelector('iframe')).toBeTruthy()
  })

  it('does not carry a cover across to a DIFFERENT app', () => {
    // The one exception to holding, and it is not a hole in it. Holding is fail-closed because
    // an absent signal says nothing about the app being covered; a new preview url means we are
    // not covering that app any more, and keeping the card up would be a claim about code this
    // container has never seen. Reachable by switching conversations in the same pane.
    //
    // ★ BETWEEN TURNS, so the only thing that can be covering here is the VERDICT: the cover's
    // other arm covers every running turn without a `clean` verdict, which would mask exactly the
    // carry-over this test is looking for.
    const { container, rerender } = setup({ compileState: 'failed' })
    expect(coverEl(container)).toBeTruthy()

    rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" compileState={null} />)
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
    // ★ BETWEEN TURNS, for the reason the two tests above record: the cover's other arm would
    // hold this screen on its own and the verdict's hold would prove nothing.
    const { container, rerender } = setup({ compileState: 'failed' })
    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="unknown" />)
    expect(coverEl(container)).toBeTruthy() // held through the unknown, same app

    rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" compileState="unknown" />)
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

  // The restore that used to outrank the cover is gone (`relaunching` had no producer), so this
  // precedence row has no contender left. What still has to hold is the rest of the chain — a
  // terminal session and a container that is not serving both take the frame away, and the cover
  // only ever exists over a frame — and that is the row below, which is unaffected.
  it('RETIREMENT GUARD: a restore can no longer pre-empt the cover, because nothing can raise one', () => {
    const { container } = setup({ turnRunning: true, compileState: 'failed', relaunching: true })
    expect(coverEl(container)).toBeTruthy() // liveness: the cover is drawn, and now nothing takes it
    expect(container.textContent).not.toMatch(/restoring your app/i)
  })

  it('★ loses to a terminal session, and to a start the platform has not watched serve', () => {
    // The cover only exists OVER A FRAME, so anything that unmounts the frame takes it with it.
    // Two such things are left, and the second one is the change.
    //
    // ★ THE LIVENESS HALVES BOTH MOVED. This test used to prove the frame was gone by finding the
    // sentence that replaced it — the terminal placeholder in the first case, "Your workspace is
    // asleep" in the second. Both of those were workspace verdicts this file has stopped
    // authoring, so neither is a handle any more. What is left to assert is structural in the
    // first case (the pane rendered, and rendered nothing) and behavioural in the second (the
    // wait replaced the frame, which is what `starting` is for).
    const terminal = render(<LivePreview turnRunning previewUrl={SANDBOX_URL} status="ended" compileState="failed" />)
    expect(coverEl(terminal.container)).toBeFalsy()
    expect(terminal.container.querySelector('iframe')).toBeNull()
    // LIVENESS, structural: the pane rendered and had nothing to say.
    expect(terminal.container.querySelector('[role="status"]')?.getAttribute('aria-live')).toBe('polite')
    cleanup()

    // ★ `asleep` NO LONGER TAKES THE FRAME DOWN HERE, and that is not a regression — it is the
    // veto moving up a level. `AppPane` will not mount this component at all unless the workspace
    // reading is `running`, so a gone reading never reaches this file. What this pane still
    // refuses on its own is `starting`: a container being brought up answers 502 at its own edge,
    // and the apps router turns a 502 into an error page shown to the one citizen watching.
    const starting = render(
      <LivePreview turnRunning previewUrl={SANDBOX_URL} status="ready" previewState="starting" compileState="failed" />,
    )
    expect(coverEl(starting.container)).toBeFalsy()
    expect(starting.container.querySelector('iframe')).toBeNull()
    // LIVENESS: the wait is what replaced the frame, so this is a withheld frame rather than an
    // empty rectangle — the half the first version of that refusal shipped without.
    expect(screen.getAllByText(/opening your app/i).length).toBeGreaterThan(0)
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
// The holding state stops when the work does
// The frame is revealed on the verdict AND the BEACON, never on either one alone
// ---------------------------------------------------------------------------------------

/** The device card carries the reveal. `opacity-100` is the revealed state; `opacity-0` is
 *  mounted-but-hidden, which is deliberately NOT unmounted — an iframe that never mounts never
 *  loads, and a document that never loads can never vouch for itself. */
function deviceCard(container) {
  return container.querySelector('[data-testid="device-card"]')
}

/** The document ARRIVING, which is now a non-event for the reveal: it starts the short vouch wait
 *  and sends the ping, and shows the citizen nothing. Kept as a helper precisely so the tests can
 *  say "it loaded" and then go on to assert that nothing happened. */
function loadTheFrame(container) {
  act(() => {
    fireEvent.load(container.querySelector('iframe'))
  })
}

describe('LivePreview — the holding state stops when the turn does', () => {
  it('says the app is not running once no turn is in flight, and stops claiming progress', () => {
    // THE FAILURE THIS CLOSES. "Putting the latest change together…" is true for exactly as long
    // as a turn is running. Left up after one ends it becomes a progress state that never
    // resolves — the citizen's only way to learn the build was over is to wait long enough to
    // stop believing it.
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
      // Past the escalation deadline but short of the idle cover's OWN expiry (30 s, since
      // 2026-09-11 — a `building` that outlives a compile yields to the document; see
      // `LivePreview.test.tsx`). Inside that window the sentence must not change at all.
      act(() => vi.advanceTimersByTime(ESCALATE_MS + 1_000))
      expect(coverEl(container).textContent).toMatch(IDLE_BUSY)
      expect(coverEl(container).textContent).not.toMatch(HOLDING_SLOW)
    } finally {
      vi.useRealTimers()
    }
  })

  it('★ says NOTHING on an unreadable verdict — no failure sentence, and no announcement', () => {
    // ★ THE THIRD VALUE IS THE LOAD-BEARING ONE. Without it the natural implementation reads "not
    // failure" as success, and republishes a claim about a build nothing verified — on exactly the
    // reload where nothing was serving. `unknown` is what the client answers for a refusal, an
    // unreadable body, a thrown request, or a container running an image older than the signal.
    //
    // ASSERT-ABSENCE, PAIRED WITH LIVENESS: the pane has to be showing its ordinary framed content
    // in the same breath, or a component that threw would satisfy every absence here.
    const { container } = setup({ compileState: 'unknown', turnRunning: false })
    loadTheFrame(container)
    vouch(container)

    // LIVENESS: the app is framed and revealed — this is the pane's normal content, not a hole.
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(card(container).className).toMatch(/opacity-100/)
    expect(coverEl(container)).toBeFalsy()
    // ABSENCE, IN BOTH DIRECTIONS: no failure sentence, and no claim that anything succeeded.
    expect(container.textContent).not.toMatch(IDLE_BROKEN)
    expect(container.textContent).not.toMatch(/build complete/i)
    expect(container.querySelector('[role="status"]').textContent).toBe('')
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

describe('LivePreview — the reveal is earned twice over', () => {
  it('reveals when the verdict passes AND the document vouches', () => {
    const { container } = setup({ compileState: 'clean' })
    expect(deviceCard(container).className).toMatch(/opacity-0/)

    vouch(container)

    expect(deviceCard(container).className).toMatch(/opacity-100/)
  })

  it('does NOT reveal on a BEACON alone when the verdict says the app failed', () => {
    // ★ THE POINT OF THE UNIT, RESTATED FOR THE BEACON. A document can vouch for itself perfectly
    // honestly while what it rendered is the framework's error screen: the beacon says the root
    // layout rendered in this browser, never that the app is healthy. `load` is weaker still — it
    // fires for a 500 exactly as for a 200, and for the bodyless 502 the in-container proxy
    // returns when the dev server is down — so both are fired here and neither shows the app.
    //
    // Mutation check: drop `!covered` from `revealed` and this goes red at full opacity, with the
    // error screen fading in underneath the cover that is describing it.
    const { container } = setup({ compileState: 'failed', turnRunning: true })

    loadTheFrame(container)
    vouch(container)

    expect(deviceCard(container).className).toMatch(/opacity-0/)
    expect(deviceCard(container).getAttribute('data-revealed')).toBe('false')
    expect(coverEl(container)).toBeTruthy() // LIVENESS: the pane rendered and is covering
  })

  it('does not reveal on a passing verdict alone — the document still has to vouch', () => {
    const { container } = setup({ compileState: 'clean' })
    expect(deviceCard(container)).toBeTruthy() // LIVENESS: the card is mounted, just hidden
    expect(deviceCard(container).className).toMatch(/opacity-0/)

    // …and not on the load either, which is the whole of the 2026-09-10 finding: a clean compile
    // verdict plus a document arriving is still two facts measured on the wrong side of the
    // network. Mutation check: put `frameLoaded` back into `revealed` and this line goes red.
    loadTheFrame(container)
    expect(deviceCard(container).className).toMatch(/opacity-0/)
  })

  it('RETRACTS a reveal when the verdict flips to failed, and the cover explains', () => {
    const { container, rerender } = setup({ compileState: 'clean' })
    vouch(container)
    expect(deviceCard(container).className).toMatch(/opacity-100/)

    rerender(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="failed" turnRunning />,
    )

    expect(deviceCard(container).className).toMatch(/opacity-0/)
    expect(coverEl(container).textContent).toMatch(HOLDING)
  })

  it('does NOT retract a reveal on an unanswerable verdict', () => {
    // `unknown` HOLDS whatever is showing rather than moving it — the same fail-closed rule that
    // stops an absent signal uncovering a broken app stops it hiding a working one.
    const { container, rerender } = setup({ compileState: 'clean' })
    vouch(container)
    expect(deviceCard(container).className).toMatch(/opacity-100/)

    rerender(<LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="unknown" />)

    expect(deviceCard(container).className).toMatch(/opacity-100/)
  })

  it('introduces no overlay of its own — the reveal is opacity and nothing else', () => {
    // An inertness guard, paired with liveness. This unit controls the frame's transparency;
    // everything visible ABOVE the frame belongs to the cover, and a second surface here would
    // be one more thing that can contradict it.
    const revealed = setup({ compileState: 'clean' })
    vouch(revealed.container)
    const overlaysWhenRevealed = revealed.container.querySelectorAll('.absolute.inset-0').length
    cleanup()

    const hidden = render(
      <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState="failed" turnRunning />,
    )
    vouch(hidden.container)

    expect(deviceCard(hidden.container)).toBeTruthy() // LIVENESS
    // Exactly ONE more full-bleed element than the revealed case: the cover. Not two.
    expect(hidden.container.querySelectorAll('.absolute.inset-0').length).toBe(
      overlaysWhenRevealed + 1,
    )
  })

  it('documents the null/unknown concession rather than leaving it to be discovered', () => {
    // ★ WHAT THIS UNIT DOES NOT CLOSE, pinned so it cannot drift silently. `covered` moves on
    // building/failed/clean and HOLDS on `unknown` and `null`, so between turns — with no compile
    // verdict ever reported — a vouched frame is revealed on the document's own word alone. True
    // of every container on an image older than the compile endpoint.
    //
    // A deliberate compatibility concession: requiring a POSITIVE compile verdict as well would
    // leave that whole fleet's preview permanently behind a wait card, which is worse than the
    // failure being fixed. What has changed since this note was first written is which signal
    // carries the weight — the beacon comes from inside the citizen's own browser, so the
    // concession is now "no compile verdict needed", not "no evidence needed". If someone later
    // closes it, this test is what tells them they are changing a decision, not fixing an
    // oversight.
    for (const compileState of [null, 'unknown']) {
      const view = render(
        <LivePreview previewUrl={SANDBOX_URL} status="ready" compileState={compileState} />,
      )
      // The load still buys nothing, on either value…
      loadTheFrame(view.container)
      expect(deviceCard(view.container).className).toMatch(/opacity-0/)
      // …and the document's own word still buys everything.
      vouch(view.container)
      expect(deviceCard(view.container).className).toMatch(/opacity-100/)
      cleanup()
    }
  })
})
