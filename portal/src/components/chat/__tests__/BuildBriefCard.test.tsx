/**
 * The build proposal card (003-U3).
 *
 * The card is the ONLY build trigger on a project thread, so the tests that matter are the ones
 * asserting the action is PRESENT and usable in the states where it would be tempting to hide
 * it — degraded parses and failed starts, i.e. exactly the moments a non-technical user would
 * otherwise be stranded with no way forward.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import BuildBriefCard from '../BuildBriefCard'

const brief = 'Build an application for BIAL that tracks visitor passes across T1 and T2.'

afterEach(cleanup)

describe('BuildBriefCard', () => {
  it('shows the brief the build will actually use', () => {
    render(<BuildBriefCard brief={brief} onBuild={vi.fn()} />)

    // The whole point of user confirmation: what is on screen IS what gets built.
    expect(screen.getByText(brief)).toBeTruthy()
  })

  it('fires the build once on confirm', () => {
    const onBuild = vi.fn()
    render(<BuildBriefCard brief={brief} onBuild={onBuild} />)

    fireEvent.click(screen.getByRole('button', { name: /build this/i }))

    expect(onBuild).toHaveBeenCalledTimes(1)
  })

  it('does not fire anything on render — a brief is a proposal, never an action', () => {
    const onBuild = vi.fn()
    render(<BuildBriefCard brief={brief} onBuild={onBuild} />)

    expect(onBuild).not.toHaveBeenCalled()
  })

  describe('degraded', () => {
    it('keeps a working build action', () => {
      const onBuild = vi.fn()
      render(<BuildBriefCard brief={brief} degraded onBuild={onBuild} />)

      const action = screen.getByRole<HTMLButtonElement>('button', { name: /build this/i })
      expect(action.disabled).toBe(false)
      fireEvent.click(action)
      expect(onBuild).toHaveBeenCalledTimes(1)
    })

    it('warns the user to read it, without exposing parser internals', () => {
      render(<BuildBriefCard brief={brief} degraded onBuild={vi.fn()} />)

      expect(screen.getByText(/give this a quick read/i)).toBeTruthy()
      expect(screen.queryByText(/parse|fence|malformed/i)).toBeNull()
    })

    it('renders no warning when the parse was clean', () => {
      render(<BuildBriefCard brief={brief} onBuild={vi.fn()} />)

      expect(screen.queryByText(/give this a quick read/i)).toBeNull()
    })
  })

  describe('gates', () => {
    it('disables the action while a start is in flight', () => {
      render(<BuildBriefCard brief={brief} busy onBuild={vi.fn()} />)

      expect(screen.getByRole<HTMLButtonElement>('button').disabled).toBe(true)
    })

    it('disables the action once this brief has been built, so a card cannot re-fire', () => {
      const onBuild = vi.fn()
      render(<BuildBriefCard brief={brief} started onBuild={onBuild} />)

      const action = screen.getByRole<HTMLButtonElement>('button', { name: /building/i })
      expect(action.disabled).toBe(true)
      fireEvent.click(action)
      expect(onBuild).not.toHaveBeenCalled()
    })
  })

  describe('failed start', () => {
    it('surfaces the error and offers the retry on the card itself', () => {
      const onBuild = vi.fn()
      render(<BuildBriefCard brief={brief} error="Could not start the build." onBuild={onBuild} />)

      expect(screen.getByRole('alert').textContent).toContain('Could not start the build.')
      // The retry lives where the failure is — not in a toast the user has to chase.
      fireEvent.click(screen.getByRole('button', { name: /try again/i }))
      expect(onBuild).toHaveBeenCalledTimes(1)
    })
  })

  describe('iteration', () => {
    it('reads as a rebuild when the thread already has an app', () => {
      render(<BuildBriefCard brief={brief} refine onBuild={vi.fn()} />)

      expect(screen.getByRole('button', { name: /rebuild with these changes/i })).toBeTruthy()
      expect(screen.getByText(/here's the updated app/i)).toBeTruthy()
    })
  })

  // The one state where hiding the action IS the right call, and the exception that proves the
  // rule above: a superseded brief no longer describes the app the thread is about, so building it
  // would revert the user's own newer work with no undo. Stranding them here is not a risk — the
  // live card is further down the same transcript.
  describe('superseded', () => {
    it('is dead, and says why', () => {
      const onBuild = vi.fn()
      render(<BuildBriefCard brief={brief} superseded onBuild={onBuild} />)

      const button = screen.getByRole<HTMLButtonElement>('button')
      expect(button.disabled).toBe(true)
      expect(button.textContent).toMatch(/replaced by a newer brief/i)
      fireEvent.click(button)
      expect(onBuild).not.toHaveBeenCalled()
    })

    it('points at the live brief instead of inviting a change to this one', () => {
      render(<BuildBriefCard brief={brief} superseded onBuild={vi.fn()} />)

      expect(screen.getByText(/scroll down for the brief this thread is building now/i)).toBeTruthy()
      expect(screen.queryByText(/keep chatting to change anything first/i)).toBeNull()
    })

    it('outranks a stale error — retrying a brief the thread moved past is never the action', () => {
      render(<BuildBriefCard brief={brief} superseded error="Could not start the build." onBuild={vi.fn()} />)

      expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
      expect(screen.getByRole<HTMLButtonElement>('button').disabled).toBe(true)
    })

    it('still reads as building when a card is superseded mid-build', () => {
      // Confirm a brief, keep chatting, and the card you are watching build IS superseded. The
      // build is real and running: saying anything else would be a lie about live state.
      render(<BuildBriefCard brief={brief} started superseded onBuild={vi.fn()} />)

      expect(screen.getByRole('button').textContent).toMatch(/building/i)
    })
  })
})
