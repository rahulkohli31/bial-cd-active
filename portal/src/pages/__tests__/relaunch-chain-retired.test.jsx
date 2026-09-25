/**
 * `LivePreview`'s frame-survival characterization, pinned directly against the component — no
 * `ConversationSurface` mount, no mocked transport. The frame-survival scenarios are the
 * dangerous half: unmounting the iframe kills a container the server is still serving.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import LivePreview from '../../components/LivePreview'

const SANDBOX_URL = 'https://app-xyz.example.azurecontainerapps.io/'
const SANDBOX_URL_2 = 'https://app-abc.example.azurecontainerapps.io/'
// The origin the beacon must claim to be believed — derived, never hand-typed, so a change to
// SANDBOX_URL can't quietly drift out of step with what `vouch()` below sends as `e.origin`.
const SANDBOX_ORIGIN = new URL(SANDBOX_URL).origin

/** The device card that carries the reveal's opacity — the handle every frame assertion uses. */
const card = (container) => container.querySelector('[data-testid="device-card"]')

/** The framed document vouching for itself — `bial:app-mounted`, sent from the CURRENT iframe's
 *  own window at the sandbox origin. This is the only thing that reveals the card now: `load`
 *  fires for a bodyless 502 exactly as it does for a real page, so a frame scenario that still
 *  revealed on `load` alone would pass over the one failure this mechanism exists to catch.
 *  `path` MUST be present — `isMountedBeaconFor` rejects a beacon without one — so this always
 *  sends `'/'`; the sandbox root's framed path strips to `''`, which every reported path matches. */
function vouch(container) {
  const iframe = container.querySelector('iframe')
  act(() => {
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'bial:app-mounted', path: '/' },
      origin: SANDBOX_ORIGIN,
      source: iframe.contentWindow,
    }))
  })
}

describe('frame survival — the case where an unmount kills a live container', () => {
  // A framed, pardoned preview: `status: 'ended'` + `serving` is the state in which the
  // server is STILL SERVING the container under an idle lease. `showTerminal` must stay false,
  // `frameContext` true and `framePending` true, or the iframe comes down over a live app.
  // (`serving` is what `completedLive` was renamed to when liveness moved onto the address — same
  // state, a name that no longer also claims a build succeeded.)
  const framedAndPardoned = (props = {}) =>
    render(<LivePreview previewUrl={SANDBOX_URL} status="ended" serving {...props} />)

  it('an ended-but-live preview keeps its frame mounted, shows no terminal card, and keeps the labelled wait', () => {
    const { container } = framedAndPardoned()

    // frameContext → the iframe exists at all.
    const iframe = container.querySelector('iframe')
    expect(iframe).toBeTruthy()
    expect(iframe.getAttribute('src')).toBe(SANDBOX_URL)
    // ★ NO ENDED CARD over a container that is still serving — and there is no ended card LEFT to
    // draw. That placeholder was a verdict about the citizen's WORKSPACE, and the workspace map
    // owns every one of those now; what survives here is the frame and the covers over it.
    expect(screen.queryByTestId('preview-ended-card')).toBeNull()
    expect(container.textContent).not.toMatch(/no longer running/i)
    // framePending → true: mounted but not yet loaded, so the wait is up and LABELLED. The label
    // says "Opening" rather than "Starting" now: this pane is mounted only once the platform has
    // watched the app ANSWER a request, so the starting is over and this frame's own document is
    // the only thing still pending.
    expect(card(container).className).toMatch(/opacity-0/)
    expect(container.textContent).toMatch(/opening your app/i)
  })

  it('…and the framed document’s own beacon resolves that wait — its `load` alone does not', () => {
    const { container } = framedAndPardoned()
    const iframe = container.querySelector('iframe')

    // `load` fires for a bodyless 502 exactly as it does for a real page — the whole reason this
    // pane stopped trusting it. Mutant that puts `frameLoaded` back into `revealed` goes red here:
    // the card would flip to opacity-100 on this `load` alone, with no beacon in sight.
    fireEvent.load(iframe)
    expect(card(container).className).toMatch(/opacity-0/)
    expect(container.textContent).toMatch(/opening your app/i)

    vouch(container)
    expect(card(container).className).toMatch(/opacity-100/)
    expect(container.textContent).not.toMatch(/opening your app/i)
  })
})

describe('the ordinary states the pane still has to render', () => {
  it('running: a live URL frames the app behind its labelled wait', () => {
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    expect(container.querySelector('iframe')).toBeTruthy()
    expect(container.textContent).toMatch(/opening your app/i)
    // The ended card is not merely withheld here — it no longer exists anywhere in the pane.
    expect(screen.queryByTestId('preview-ended-card')).toBeNull()
  })

  it('nothing to frame yet: provisioning with no URL is the labelled build wait, not a blank pane', () => {
    const { container } = render(<LivePreview previewUrl={null} status="provisioning" />)
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.textContent).toMatch(/setting up your sandbox/i)
  })

  it('a new URL mid-session re-gates the reveal on the new frame’s own beacon', () => {
    const { container, rerender } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
    vouch(container)
    expect(card(container).className).toMatch(/opacity-100/)
    // The old key's vouch must not carry over to the new one — mutant that keys `vouchedKey` on
    // the bare URL rather than the full frame key goes red here, staying revealed across the swap.
    rerender(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" />)
    expect(card(container).className).toMatch(/opacity-0/)
  })
})
