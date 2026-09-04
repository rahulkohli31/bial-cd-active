/**
 * SessionBanners (U15): the lifecycle banners, relocated above the composer from the retired
 * SessionControls. The pinned behaviors carry over verbatim:
 *  - feed-disconnected offers a manual Reconnect;
 *  - quota shows the daily-limit + IST reset copy;
 *  - both report something BLOCKED or BROKEN and are assertive: the operator must not miss one.
 *
 * TWO BANNERS AND THEIR TESTS ARE GONE, each because its PRODUCER was:
 *
 *  · the sleeping-workspace banner was raised only by the blind keep-alive loop U13 deleted, so
 *    `reclaimed` had no producer left. Its test kept passing — the strongest possible illustration
 *    of why a green suite is not coverage: it exercised a prop production could no longer set. The
 *    R17 argument it protected (a reclaimed container is a sleeping workspace, never an error) is
 *    enforced where a real producer exists, in `LivePreview`'s `asleep` state.
 *  · the block banner, with its Force-end and Dismiss, was raised by the session hook's `blocked`.
 *    That had TWO producers — `start`'s 409 and `relaunch`'s — and neither was reachable, so the
 *    same trap had been set twice. The live 409 comes off `relaunchPreview` today and is reported
 *    as a workspace sentence in the pane; `pages/__tests__/relaunch-chain-retired.test.jsx` drives
 *    BOTH arms and pins that neither can put this banner back on screen.
 *
 * What is left below is the retirement guard: no lifecycle state this component can be given
 * renders a Force-end control.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, fireEvent, screen } from '@testing-library/react'
import SessionBanners from '../SessionBanners'

afterEach(cleanup)

const noop = () => {}

function draw(props: Partial<Parameters<typeof SessionBanners>[0]> = {}) {
  return render(
    <SessionBanners feedDisconnected={false} quota={null} onReconnect={noop} {...props} />,
  )
}

describe('SessionBanners', () => {
  it('renders nothing when the session lifecycle is quiet', () => {
    const { container } = draw()
    expect(container.firstChild).toBeNull()
  })

  it('RETIREMENT GUARD: no state this component accepts puts a block banner or a Force-end back', () => {
    // Driven over every combination the props still allow, and PAIRED WITH LIVENESS in each: an
    // absence assertion on a component that rendered nothing is worth nothing.
    for (const props of [
      { feedDisconnected: true },
      { quota: { limit: 10, used: 11, resetsAt: '2026-07-24T00:00:00+05:30' } },
      { feedDisconnected: true, quota: { limit: 10, used: 11, resetsAt: '2026-07-24T00:00:00+05:30' } },
    ]) {
      const { unmount } = draw(props)
      expect(screen.getAllByRole('alert').length).toBeGreaterThan(0) // liveness: it rendered
      expect(screen.queryByText(/already have a build running/i)).toBeNull()
      expect(screen.queryByRole('button', { name: /force-end/i })).toBeNull()
      unmount()
    }
  })

  it('feed-disconnected banner offers a manual Reconnect', () => {
    const onReconnect = vi.fn()
    draw({ feedDisconnected: true, onReconnect })
    fireEvent.click(screen.getByRole('button', { name: /reconnect/i }))
    expect(onReconnect).toHaveBeenCalledTimes(1)
  })

  it('quota banner shows the daily-limit + IST reset copy, assertively', () => {
    draw({ quota: { limit: 1000000, used: 1000001, resetsAt: '2026-07-24T00:00:00+05:30' } })
    const alert = screen.getByRole('alert')
    expect(alert.getAttribute('aria-live')).toBe('assertive')
    expect(alert.textContent).toMatch(/daily limit/i)
    expect(alert.textContent).toMatch(/midnight IST/i)
  })
})
