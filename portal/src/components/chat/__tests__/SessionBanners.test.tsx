/**
 * SessionBanners (U15): the four lifecycle banners, relocated above the composer from
 * the retired SessionControls. The pinned behaviors carry over verbatim:
 *  - block banner force-ends the EXISTING session id; with NO id (post-restart 409) the
 *    button disables and explains the retry (finding #24);
 *  - reclaimed offers Start again; feed-disconnected offers a manual Reconnect;
 *  - quota shows the daily-limit + IST reset copy;
 *  - the three banners that report something BLOCKED or BROKEN are assertive (the operator
 *    must not miss one); the sleeping-workspace one is not, because nothing is wrong (R17).
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
      reclaimed={false}
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

  it('reclaimed banner describes a sleeping workspace, POLITELY, and still offers Start again', () => {
    // R17. This was a red `role="alert"` / `aria-live="assertive"` "Your build session was
    // reclaimed." — the platform reporting its own housekeeping as the citizen's emergency.
    // A reclaimed container is a workspace that went to sleep with its work on durable
    // storage, so it is a `status`, announced politely, in neutral colours.
    //
    // The banner is RE-TONED, never retired: `reclaimed` latches (the only
    // `setReclaimed(false)` is inside `reset()`, whose sole caller is this very button), so
    // deleting it would leave the collapsed-panel attention dot lit forever with nothing on
    // screen to dismiss — and its source is `onKeepAliveSettled`, which no other surface
    // renders.
    const onStartAgain = vi.fn()
    draw({ reclaimed: true, onStartAgain })
    const banner = screen.getByRole('status')
    expect(banner.getAttribute('aria-live')).toBe('polite')
    expect(banner.textContent).toMatch(/went to sleep/i)
    expect(banner.textContent).not.toMatch(/reclaimed/i)
    // No danger styling anywhere in the banner — R17's "never show an error for a reclaimed
    // container" is a claim about what the citizen SEES, not only about the copy.
    expect(banner.className).not.toMatch(/danger/)
    expect(banner.querySelector('[class*="danger"]')).toBeNull()
    // …and nothing on this surface interrupts for it.
    expect(screen.queryByRole('alert')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /start again/i }))
    expect(onStartAgain).toHaveBeenCalledTimes(1)
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
