/**
 * THE SUFFICIENCY CHECK FOR `src/test-setup.ts`.
 *
 * This file is deliberately small, and what it leaves out matters as much as what it keeps.
 * Assertions of the form "the global the setup file defines is defined" restate the setup file
 * in a second place and go green whether or not the shim actually WORKS. The tests that prove
 * these shims are the ones that cannot run without them — the activity group, the attachment
 * dialog, the copy button — and those live in their own units.
 *
 * What is worth pinning here is the thing those units cannot pin: that the `setupFiles` key
 * exists at all. It is a single line in `vitest.config.js` with no compiler and no linter behind
 * it, and there was no `setupFiles` key in this project before. Drop it in a merge and every
 * consumer three units away fails at once with a Radix stack trace naming an internal, which is
 * a long way from "the config lost a line".
 *
 * WHICH TESTS HERE ARE ACTUALLY THE CANARY — measured by deleting the key and running the file,
 * not assumed. The plan expected "render a Radix component that stubs nothing" to be the
 * sufficiency check. It is NOT: bare jsdom provides none of the eight globals this setup file
 * defines, and a Radix Dialog and an opening Radix Select both still work without them, because
 * neither reaches the pointer-capture or scroll paths on a `click`-driven open. Both of those
 * tests went green with the key removed.
 *
 * The two SHAPE tests are what go red, and they are kept for that reason as much as for the
 * shapes themselves — both of which are load-bearing and easy to get subtly wrong:
 *  - the clipboard spy must be REJECTABLE (R65 and the copy button's failure path both need it);
 *  - `matchMedia` must return `removeEventListener` as well as `addEventListener`, or every
 *    component that subscribes to reduced motion throws on UNMOUNT rather than on render.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

describe('the test environment has the shims the component libraries need', () => {
  it('OPENS a real Radix Select in a file that stubs nothing', async () => {
    // Not the canary (see the docblock), but the closest thing in the tree to a real consumer of
    // the pointer-capture shims, and the reason they are shipped: Plan F puts a Radix Select on
    // the history filter, and today three test files stub those methods by hand because there
    // was nowhere global to put them. This is the working recipe, written down once —
    // `fireEvent.click` then `findByRole('option')`. `fireEvent.change` on a `combobox` button
    // silently no-ops, which is why `MarketplacePage.test.tsx` carries the same warning.
    render(
      <Select>
        <SelectTrigger aria-label="Kind">
          <SelectValue placeholder="Any kind" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="plan">Plan</SelectItem>
          <SelectItem value="build">Build</SelectItem>
        </SelectContent>
      </Select>,
    )
    fireEvent.click(screen.getByRole('combobox', { name: 'Kind' }))
    expect(await screen.findByRole('option', { name: 'Build' })).toBeTruthy()
  })

  it('renders and unmounts a real Radix Dialog in a file that stubs nothing', () => {
    // Not a canary — a Dialog needs no shims — but U14 hosts the attachment preview in one, so
    // this pins that the component vendored in U1 mounts and tears down cleanly.
    const { unmount } = render(
      <Dialog open>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Attachment</DialogTitle>
            <DialogDescription>A preview over the conversation.</DialogDescription>
          </DialogHeader>
        </DialogContent>
      </Dialog>,
    )
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByText('Attachment')).toBeTruthy()
    expect(() => unmount()).not.toThrow()
  })

  it('gives navigator.clipboard a spy that resolves, and that a test can make reject', async () => {
    // Clipboard writes genuinely fail — insecure origins, denied permissions — and N1's copy
    // button has to announce that. A shim that can only succeed cannot test the half that
    // matters.
    await expect(navigator.clipboard.writeText('hello')).resolves.toBeUndefined()
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('hello')

    const write = vi.mocked(navigator.clipboard.writeText)
    write.mockRejectedValueOnce(new Error('NotAllowedError'))
    await expect(navigator.clipboard.writeText('nope')).rejects.toThrow('NotAllowedError')

    // And it recovers, so one test's rejection does not leak into the next.
    await expect(navigator.clipboard.writeText('again')).resolves.toBeUndefined()
  })

  it('gives matchMedia both addEventListener AND removeEventListener', () => {
    // `usePrefersReducedMotion` subscribes on mount and unsubscribes on unmount. A shim with
    // only the first throws when the component goes away, which surfaces as an unrelated test
    // failing during cleanup.
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    expect(mq.matches).toBe(false) // default is "animate", i.e. today's behaviour, unchanged
    expect(typeof mq.addEventListener).toBe('function')
    expect(typeof mq.removeEventListener).toBe('function')

    const onChange = vi.fn()
    expect(() => {
      mq.addEventListener('change', onChange)
      mq.removeEventListener('change', onChange)
    }).not.toThrow()
  })
})
