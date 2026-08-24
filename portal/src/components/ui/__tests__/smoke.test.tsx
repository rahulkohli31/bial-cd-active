// Smoke surface for the shadcn/ui infrastructure: Button mounts under the portal's real
// resolver chain (`@/` alias through vitest.config.js) and carries its variant classes.
// The Ask/Plan/Write mode switcher (`ModeSwitcher`) shipped on a dropdown pill, never on the
// vendored Radix Toggle/ToggleGroup this file used to also smoke-test — U27 removed those two
// zero-reference components (and their Radix deps) as dead weight, so this stays Button-only.
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Button } from '@/components/ui/button'

describe('shadcn/ui smoke surface', () => {
  it('mounts Button with the brand-token variant classes', () => {
    render(<Button>Build it</Button>)
    const button = screen.getByRole('button', { name: 'Build it' })
    expect(button.className).toContain('bg-primary')
    expect(button.className).toContain('text-primary-foreground')
  })

  it('mounts Button asChild onto an anchor (Slot path)', () => {
    render(
      <Button asChild variant="outline">
        <a href="/somewhere">Go</a>
      </Button>
    )
    const link = screen.getByRole('link', { name: 'Go' })
    expect(link.className).toContain('border-input')
  })
})
