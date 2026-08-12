/**
 * SessionBanners (U15): the lifecycle banners, relocated above the composer from the retired
 * SessionControls. The pinned behaviors carry over verbatim:
 *  - block banner force-ends the EXISTING session id; with NO id (post-restart 409) the
 *    button disables and explains the retry (finding #24);
 *  - feed-disconnected offers a manual Reconnect;
 *  - quota shows the daily-limit + IST reset copy;
 *  - all of them report something BLOCKED or BROKEN and are assertive: the operator must not
 *    miss one.
 *
 * THERE WAS A FOURTH, and its test is gone with it. The sleeping-workspace banner was raised
 * only by the blind keep-alive loop U13 deleted, so `reclaimed` had no producer left and the
 * banner could not render outside this file. Its test kept passing — the strongest possible
 * illustration of why a green suite is not coverage: it exercised a prop that production could
 * no longer set. The R17 argument it protected (a reclaimed container is a sleeping workspace,
 * never an error) is enforced where a real producer exists, in `LivePreview`'s `asleep` state.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, fireEvent, screen } from '@testing-library/react'
import SessionBanners from '../SessionBanners'

afterEach(cleanup)

const noop = () => {}

function draw(props: Partial<Parameters<typeof SessionBanners>[0]> = {}) {
  return render(
    <SessionBanners
      blocked={null}
      feedDisconnected={false}
      quota={null}
      onForceEnd={noop}
      onReconnect={noop}
      onStartAgain={noop}
      {...props}
    />,
  )
}

describe('SessionBanners', () => {
  it('renders nothing when the session lifecycle is quiet', () => {
    const { container } = draw()
    expect(container.firstChild).toBeNull()
  })

  it('block banner: force-ending it targets the EXISTING sessionId', () => {
    const onForceEnd = vi.fn()
    draw({ blocked: { existingSessionId: 'sess-9' }, onForceEnd })
    expect(screen.getByRole('alert').textContent).toMatch(/already have a build running/i)
    fireEvent.click(screen.getByRole('button', { name: /force-end it/i }))
    expect(onForceEnd).toHaveBeenCalledWith('sess-9')
  })

  it('block banner with NO existing sessionId disables Force-end and explains the retry (finding #24)', () => {
    draw({ blocked: { existingSessionId: null } })
    const btn = screen.getByRole('button', { name: /force-end it/i }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(screen.getByRole('alert').textContent).toMatch(/being reclaimed — retry shortly/i)
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
