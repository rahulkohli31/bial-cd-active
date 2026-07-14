import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import SessionControls from '../SessionControls'
import type { SessionControlsProps } from '../SessionControls'

afterEach(cleanup)

function props(over: Partial<SessionControlsProps> = {}): SessionControlsProps {
  return {
    status: null,
    stopping: false,
    blocked: null,
    reclaimed: false,
    feedDisconnected: false,
    quota: null,
    startedAt: null,
    onStart: vi.fn(),
    onStop: vi.fn(),
    onForceEnd: vi.fn(),
    onReconnect: vi.fn(),
    onStartAgain: vi.fn(),
    ...over,
  }
}

describe('SessionControls — the state matrix', () => {
  it('Start is shown only when no session is active; Stop/Force-end only while active', () => {
    const idle = render(<SessionControls {...props({ status: null })} />)
    expect(idle.getByRole('button', { name: /start build/i })).toBeTruthy()
    expect(idle.queryByRole('button', { name: /^stop$/i })).toBeNull()
    cleanup()

    render(<SessionControls {...props({ status: 'building' })} />)
    expect(screen.queryByRole('button', { name: /start build/i })).toBeNull()
    expect(screen.getByRole('button', { name: /^stop$/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /force-end/i })).toBeTruthy()
  })

  it('Stop shows a PENDING state while stopping (disabled, so no double-starts)', () => {
    render(<SessionControls {...props({ status: 'building', stopping: true })} />)
    const stop = screen.getByRole('button', { name: /stopping/i })
    expect(stop).toBeTruthy()
    expect((stop as HTMLButtonElement).disabled).toBe(true)
  })

  it('Start is disabled while a quota lock is showing', () => {
    render(<SessionControls {...props({ status: null, quota: { limit: 100, used: 100, resetsAt: 'x' } })} />)
    expect((screen.getByRole('button', { name: /start build/i }) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('SessionControls — force-end confirms first', () => {
  it('a first click asks for confirmation (surfacing elapsed time); only Confirm fires onForceEnd', () => {
    const onForceEnd = vi.fn()
    render(<SessionControls {...props({ status: 'building', startedAt: Date.now() - 65_000, onForceEnd })} />)

    fireEvent.click(screen.getByRole('button', { name: /force-end/i }))
    expect(onForceEnd).not.toHaveBeenCalled() // not yet — it kills in-progress work
    expect(screen.getByText(/kills in-progress work/i)).toBeTruthy()
    expect(screen.getByText(/1m 5s/)).toBeTruthy() // elapsed surfaced

    fireEvent.click(screen.getByRole('button', { name: /^force-end$/i }))
    expect(onForceEnd).toHaveBeenCalledTimes(1)
  })

  it('Cancel backs out without force-ending', () => {
    const onForceEnd = vi.fn()
    render(<SessionControls {...props({ status: 'building', onForceEnd })} />)
    fireEvent.click(screen.getByRole('button', { name: /force-end/i }))
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onForceEnd).not.toHaveBeenCalled()
    expect(screen.queryByText(/kills in-progress work/i)).toBeNull()
  })
})

describe('SessionControls — the four banners (all assertive)', () => {
  it('block banner: force-ending it targets the EXISTING sessionId', () => {
    const onForceEnd = vi.fn()
    render(<SessionControls {...props({ blocked: { existingSessionId: 'existing-9' }, onForceEnd })} />)
    const banner = screen.getByRole('alert')
    expect(banner.getAttribute('aria-live')).toBe('assertive')
    expect(banner.textContent).toMatch(/already have a build running/i)
    fireEvent.click(screen.getByRole('button', { name: /force-end it/i }))
    expect(onForceEnd).toHaveBeenCalledWith('existing-9')
  })

  it('reclaimed banner offers Start again', () => {
    const onStartAgain = vi.fn()
    render(<SessionControls {...props({ reclaimed: true, onStartAgain })} />)
    expect(screen.getByText(/was reclaimed/i)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /start again/i }))
    expect(onStartAgain).toHaveBeenCalledTimes(1)
  })

  it('feed-disconnected banner offers a manual Reconnect', () => {
    const onReconnect = vi.fn()
    render(<SessionControls {...props({ status: 'building', feedDisconnected: true, onReconnect })} />)
    expect(screen.getByText(/lost the build activity feed/i)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /reconnect/i }))
    expect(onReconnect).toHaveBeenCalledTimes(1)
  })

  it('quota banner shows the daily-limit + IST reset copy', () => {
    render(<SessionControls {...props({ quota: { limit: 1_000_000, used: 1_000_000, resetsAt: 'x' } })} />)
    const banners = screen.getAllByRole('alert')
    expect(banners.some((b) => /1,000,000 tokens/.test(b.textContent ?? ''))).toBe(true)
    expect(screen.getByText(/resets at midnight IST/i)).toBeTruthy()
  })
})
