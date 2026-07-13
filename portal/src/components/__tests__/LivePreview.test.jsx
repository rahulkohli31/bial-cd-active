import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import LivePreview from '../LivePreview.jsx'

afterEach(cleanup)

const CODE = 'function PreviewApp(){ return null }'
// The Phase-2 preview is a genuinely CROSS-ORIGIN sandbox frame (C8). previewUrl is the
// sandbox FQDN root; previewOrigin is what the origin guard validates + what outbound posts
// target (never '*').
const SANDBOX_URL = 'https://app-xyz.example.azurecontainerapps.io/'
const SANDBOX_ORIGIN = 'https://app-xyz.example.azurecontainerapps.io'

function setup(props = {}) {
  const view = render(
    <LivePreview previewCode={CODE} generating={false} generationStage={5} previewUrl={SANDBOX_URL} {...props} />,
  )
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

  it('on previewReady from the sandbox origin, posts previewCode + config + accessToken with an EXPLICIT targetOrigin', () => {
    const config = { appId: 'a1', appKey: 'k1', baseUrl: '/api', loginRequired: true }
    const { iframe } = setup({ config, accessToken: 'TOK' })
    const post = vi.spyOn(iframe.contentWindow, 'postMessage')

    window.dispatchEvent(fromSandbox({ previewReady: true }))

    expect(post).toHaveBeenCalledWith(
      expect.objectContaining({ previewCode: expect.stringContaining('PreviewApp'), config, accessToken: 'TOK' }),
      SANDBOX_ORIGIN, // explicit targetOrigin — never '*'
    )
  })

  it('re-pushes config/token on their own when they change, with the explicit targetOrigin', () => {
    const { iframe, rerender } = setup({ config: { appId: 'a1', appKey: 'k1', baseUrl: '/api', loginRequired: false } })
    const post = vi.spyOn(iframe.contentWindow, 'postMessage')

    const newConfig = { appId: 'a1', appKey: 'k1', baseUrl: '/api', loginRequired: true }
    rerender(
      <LivePreview previewCode={CODE} generating={false} generationStage={5} previewUrl={SANDBOX_URL} config={newConfig} accessToken="TOK2" />,
    )

    expect(post).toHaveBeenCalledWith({ config: newConfig, accessToken: 'TOK2' }, SANDBOX_ORIGIN)
  })

  it('REJECTS a message from a wrong origin — never posts back (C8 §3 origin guard)', () => {
    const { iframe } = setup()
    const post = vi.spyOn(iframe.contentWindow, 'postMessage')
    // A previewReady spoofed from a hostile origin must be ignored.
    window.dispatchEvent(new MessageEvent('message', { data: { previewReady: true }, origin: 'https://evil.example' }))
    expect(post).not.toHaveBeenCalled()
  })

  it('omits config/token gracefully when none are provided (legacy callers)', () => {
    const { iframe } = setup() // no config/accessToken props
    const post = vi.spyOn(iframe.contentWindow, 'postMessage')
    window.dispatchEvent(fromSandbox({ previewReady: true }))
    expect(post).toHaveBeenCalledWith(
      expect.objectContaining({ previewCode: expect.stringContaining('PreviewApp'), config: undefined, accessToken: undefined }),
      SANDBOX_ORIGIN,
    )
  })

  it('uses the C8 sandbox token list — allow-same-origin for the cross-origin next dev app, top-nav/popups withheld', () => {
    const { iframe } = setup()
    const sandbox = iframe.getAttribute('sandbox')
    expect(sandbox).toContain('allow-scripts')
    expect(sandbox).toContain('allow-same-origin') // C8: the real next dev app runs as its own sandbox-FQDN origin
    expect(sandbox).toContain('allow-forms')
    expect(sandbox).toContain('allow-downloads')
    expect(sandbox).not.toContain('allow-top-navigation') // withheld: no top-nav hijack of the portal tab
    expect(sandbox).not.toContain('allow-popups') // withheld: no popup-phishing of the portal tab
  })

  it('renders NO preview iframe when previewUrl is unset — dark until Wave-1 PORTAL-PREVIEW', () => {
    const view = render(<LivePreview previewCode={CODE} generating={false} generationStage={5} />)
    expect(view.container.querySelector('iframe')).toBeNull()
  })
})
