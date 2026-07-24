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
    expect(second).toBe(first) // same node — the iframe is keyed on previewUrl only
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
  it('offers a Relaunch button on the terminal placeholder when onRelaunch is provided', () => {
    const onRelaunch = vi.fn()
    render(<LivePreview previewUrl={null} status="ended" onRelaunch={onRelaunch} />)
    const button = screen.getByRole('button', { name: /relaunch preview/i })
    fireEvent.click(button)
    expect(onRelaunch).toHaveBeenCalledTimes(1)
  })

  it('offers Relaunch on a FAILED build too (its snapshot may still be restorable)', () => {
    const onRelaunch = vi.fn()
    render(<LivePreview previewUrl={null} status="failed" onRelaunch={onRelaunch} />)
    expect(screen.getByRole('button', { name: /relaunch preview/i })).toBeTruthy()
  })

  it('without onRelaunch, the terminal keeps its plain "start a new build" copy (no button)', () => {
    const { container } = render(<LivePreview previewUrl={null} status="ended" />)
    expect(screen.queryByRole('button', { name: /relaunch preview/i })).toBeNull()
    expect(container.textContent).toMatch(/start a new build/i)
  })

  it('while relaunching, shows the "Restoring…" busy state and hides the button (no double-click)', () => {
    const onRelaunch = vi.fn()
    const { container } = render(<LivePreview previewUrl={null} status="ended" onRelaunch={onRelaunch} relaunching />)
    expect(container.textContent).toMatch(/restoring your app/i)
    expect(screen.queryByRole('button', { name: /relaunch preview/i })).toBeNull()
    expect(container.querySelector('[aria-busy="true"]')).toBeTruthy()
  })

  it('frames the restored preview once relaunch resolves (a fresh ready URL)', () => {
    // BuilderPage feeds the relaunched URL back with status "ready" → the pane frames it.
    const { container } = render(<LivePreview previewUrl={SANDBOX_URL_2} status="ready" onRelaunch={vi.fn()} />)
    expect(container.querySelector('iframe')?.getAttribute('src')).toBe(SANDBOX_URL_2)
  })
})

describe('LivePreview — relaunch from PROJECT state, not this transcript (finding #1)', () => {
  it('a conversation with NO build history in a project WITH an app still offers Relaunch', () => {
    // status null + no previewUrl = the empty state a fresh chat lands in.
    const onRelaunch = vi.fn()
    const { container } = render(<LivePreview projectHasApp onRelaunch={onRelaunch} />)
    expect(container.textContent).toMatch(/already has a saved build/i)
    fireEvent.click(screen.getByRole('button', { name: /relaunch preview/i }))
    expect(onRelaunch).toHaveBeenCalledTimes(1)
  })

  it('a project WITHOUT an app keeps the plain empty copy — no phantom relaunch', () => {
    const { container } = render(<LivePreview onRelaunch={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(container.textContent).toMatch(/submit a prompt to start a build/i)
  })

  it('a confirmed-absent snapshot (not_found after the click) drops the affordance back to plain copy', () => {
    // The registry row can exist while the snapshot does not (first build died before
    // finalize). The click answers definitively: 404 → the button goes away.
    const { container } = render(
      <LivePreview
        projectHasApp
        onRelaunch={vi.fn()}
        relaunchError={{ kind: 'not_found', message: 'No saved build to relaunch. Build the app first.' }}
      />,
    )
    expect(screen.queryByRole('button', { name: /relaunch/i })).toBeNull()
    expect(container.textContent).toMatch(/submit a prompt to start a build/i)
  })

  it('a retryable relaunch failure from the empty state keeps the button (U6 matrix holds here too)', () => {
    render(
      <LivePreview
        projectHasApp
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
        relaunchError={{ kind: 'failed', message: 'Failed to relaunch the preview' }}
      />,
    )
    expect(screen.getByRole('alert').textContent).toMatch(/failed to relaunch/i)
    expect(screen.getByRole('button', { name: /relaunch preview/i })).toBeTruthy()
  })

  it('labels the button "Relaunch last saved version" when the newest build failed (U6/F1)', () => {
    render(<LivePreview previewUrl={null} status="failed" onRelaunch={vi.fn()} lastBuildFailed />)
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

describe('LivePreview — grace + fade-in on the first framed src (F8/U5)', () => {
  it('mounts the iframe immediately but hides it (opacity-0) during the grace, then fades it in', () => {
    vi.useFakeTimers()
    try {
      const { container } = render(<LivePreview previewUrl={SANDBOX_URL} status="ready" />)
      const iframe = container.querySelector('iframe')
      expect(iframe).toBeTruthy() // mounted at once so it starts loading during the grace
      expect(iframe.parentElement.className).toMatch(/opacity-0/) // hidden — no port-bind flicker
      act(() => vi.advanceTimersByTime(500))
      expect(iframe.parentElement.className).toMatch(/opacity-100/) // faded in after the grace
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
        <LivePreview previewUrl={SANDBOX_URL} status="ended" completedLive reconnecting onRelaunch={onRelaunch} />,
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
