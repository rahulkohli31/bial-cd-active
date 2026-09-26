/**
 * The vendored `switch` primitive — the three facts every caller of it depends on.
 *
 * THE THIRD ONE IS THE POINT. `ProjectConnectorRow` locks this control mid-write with
 * `aria-disabled` rather than the native `disabled`, because disabling a FOCUSED control throws
 * focus to `<body>` (see `dialog.tsx`'s focus-backstop docblock). That choice only works if the
 * caller ALSO guards its handler — `aria-disabled` still delivers the click. The last test here
 * pins that, so nobody "simplifies" the row's early return away on the belief that the attribute
 * was doing the work.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

import { Switch } from '../switch'

afterEach(cleanup)

describe('Switch', () => {
  it('exposes role="switch" with aria-checked, and reflects its checked prop', () => {
    const { rerender } = render(<Switch checked={false} aria-label="Read ORBIT in Bay Occupancy" />)

    const control = screen.getByRole('switch', { name: 'Read ORBIT in Bay Occupancy' })
    expect(control.getAttribute('aria-checked')).toBe('false')
    expect(control.getAttribute('data-state')).toBe('unchecked')

    rerender(<Switch checked aria-label="Read ORBIT in Bay Occupancy" />)
    expect(control.getAttribute('aria-checked')).toBe('true')
    expect(control.getAttribute('data-state')).toBe('checked')
  })

  it('reports the value the press is asking for, not the value it already had', () => {
    const onCheckedChange = vi.fn()
    render(<Switch checked={false} aria-label="Read ORBIT" onCheckedChange={onCheckedChange} />)

    fireEvent.click(screen.getByRole('switch'))

    expect(onCheckedChange).toHaveBeenCalledTimes(1)
    expect(onCheckedChange).toHaveBeenCalledWith(true)
  })

  it('still fires while aria-disabled — the lock is the caller’s handler, not the attribute', () => {
    const onCheckedChange = vi.fn()
    render(
      <Switch checked aria-disabled aria-label="Read ORBIT" onCheckedChange={onCheckedChange} />,
    )

    const control = screen.getByRole('switch')
    // The liveness half: the control really rendered in the state under test.
    expect(control.getAttribute('aria-disabled')).toBe('true')

    fireEvent.click(control)

    // NOT zero. This is the whole reason `ProjectConnectorRow.toggle` opens with `if (busy) return`.
    expect(onCheckedChange).toHaveBeenCalledTimes(1)
  })
})

const thumbOf = (control: HTMLElement): HTMLElement => {
  const thumb = control.firstElementChild
  if (!(thumb instanceof HTMLElement)) throw new Error('the switch rendered no thumb')
  return thumb
}

describe('Switch size', () => {
  it('size="lg" renders the 38×22 track with an 18px thumb and 16px of travel', () => {
    render(<Switch size="lg" checked aria-label="Active" />)
    const control = screen.getByRole('switch', { name: 'Active' })

    expect(control.className).toContain('h-[22px]')
    expect(control.className).toContain('w-[38px]')
    expect(control.className).toContain('p-px')
    expect(control.className).not.toContain('w-8')

    const thumb = thumbOf(control)
    expect(thumb.className).toContain('h-[18px]')
    expect(thumb.className).toContain('w-[18px]')
    expect(thumb.className).toContain('data-[state=checked]:translate-x-[16px]')
    expect(thumb.className).not.toContain('translate-x-[14px]')
  })

  it('the default size keeps the 32×18 track with a 14px thumb', () => {
    render(<Switch checked aria-label="Read ORBIT" />)
    const control = screen.getByRole('switch', { name: 'Read ORBIT' })

    expect(control.className).toContain('h-[18px]')
    expect(control.className).toContain('w-8')
    expect(control.className).toContain('p-0.5')
    expect(control.className).not.toContain('w-[38px]')

    const thumb = thumbOf(control)
    expect(thumb.className).toContain('h-3.5')
    expect(thumb.className).toContain('data-[state=checked]:translate-x-[14px]')
    expect(thumb.className).not.toContain('h-[18px]')
  })
})
